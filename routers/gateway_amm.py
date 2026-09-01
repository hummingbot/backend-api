"""
Gateway AMM Router - Handles DEX AMM liquidity operations via Hummingbot Gateway.

Supports AMM connectors (Meteora DAMM v2, Raydium CPMM, Uniswap/Pancakeswap V2). This is a
deliberately separate surface from CLMM: AMM was previously removed from hummingbot-api and is
re-added here to expose Gateway's standardized /trading/amm/* routes.

Every liquidity WRITE is persisted to gateway_amm_events — the AMM history for all connectors.
Without it a deposit existed only on-chain and in Gateway's live view, with no record here that
it happened and no gas accounting at all.

Meteora DAMM v2 positions are NFTs with their own identity, so they additionally get tracked
rows in gateway_amm_positions, carrying deposited capital, held amounts and a base-weighted
entry price — the same treatment CLMM positions get. The routes are position-addressed for
them: remove requires position_address, add takes it optionally (omit = new position),
position-info returns a positions[] breakdown, and positions-owned lists a wallet's positions.

Fungible-LP AMMs (Raydium CPMM, Uniswap/PancakeSwap V2) have no position identity, so they get
events only; their holdings are the LP token balance, read live from Gateway. They ignore
position_address, and Gateway rejects positions-owned for them with a 400, surfaced as-is.
"""
import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException

from database import AsyncDatabaseManager
from database.repositories import GatewayAMMRepository
from database.repositories.gateway_amm_repository import has_nft_positions
from deps import get_accounts_service, get_database_manager, require_gateway_online
from models import (
    AMMAddLiquidityRequest,
    AMMCreatePoolRequest,
    AMMCreatePoolResponse,
    AMMPoolInfoResponse,
    AMMPositionInfoResponse,
    AMMPositionsOwnedRequest,
    AMMQuoteLiquidityRequest,
    AMMQuoteLiquidityResponse,
    AMMRemoveLiquidityRequest,
    AMMTransactionResponse,
)
from routers.gateway_extras import (
    ExtraParamsSpec,
    get_transaction_status_from_response,
    transaction_id_from_error,
    validate_extra_params,
)
from services.accounts_service import AccountsService
from services.gateway_client import GatewayError, check_gateway_error, get_native_gas_token

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Gateway AMM"], prefix="/gateway")

# Gateway's unified create-pool destructure, per consuming connector:
# configAddress (meteora DAMM v2 — required there), ammConfigIndex (raydium CPMM).
# EVM seeding slippage is the standard slippage_pct field, not an extra param.
AMM_CREATE_POOL_EXTRA_PARAMS_SPEC: ExtraParamsSpec = {
    "configAddress": ((str,), {"meteora"}),
    "ammConfigIndex": ((int,), {"raydium"}),
}


async def _resolve_wallet(accounts_service: AccountsService, network: str, wallet_address) -> str:
    chain, _ = accounts_service.gateway_client.parse_network_id(network)
    return await accounts_service.gateway_client.get_wallet_address_or_default(
        chain=chain, wallet_address=wallet_address
    )


async def _read_pool(
    accounts_service: AccountsService,
    connector: str,
    network: str,
    pool_address: str,
) -> Dict[str, Any]:
    """Pool state at the moment of a write — its price is the cost basis for the event.

    Read for every connector, not just the ones with position rows: a fungible-LP AMM has
    nowhere else to record the price its capital went in or out at. A failure costs the
    price, never the write — the liquidity has already moved by the time this is called.
    """
    try:
        return check_gateway_error(await accounts_service.gateway_client.amm_pool_info(
            connector=connector, chain_network=network, pool_address=pool_address,
        )) or {}
    except Exception as e:
        logger.warning(f"Could not read AMM pool {pool_address}; the write will be "
                       f"recorded without a price: {e}")
        return {}


async def _book_position_add(
    accounts_service: AccountsService,
    db_manager: AsyncDatabaseManager,
    request: AMMAddLiquidityRequest,
    wallet_address: str,
    position_address: str,
    data: Dict[str, Any],
    pool_info: Dict[str, Any],
    price: Optional[float],
) -> None:
    """Create or top up the DAMM v2 position row for a confirmed add."""
    base_added = data.get("baseTokenAmountAdded") or 0
    quote_added = data.get("quoteTokenAmountAdded") or 0
    # Rent is locked, not spent: the chain returns it when the position account closes,
    # and Gateway reports it separately for exactly that reason. Recorded here so the
    # close can be checked against it — a refund smaller than what was locked means an
    # account was left behind. Present only when this add opened the position; adding to
    # one that already exists locks no further rent.
    position_rent = data.get("positionRent")

    try:
        async with db_manager.get_session_context() as session:
            repo = GatewayAMMRepository(session)
            existing = await repo.get_position_by_address(position_address)
            if existing:
                await repo.add_to_position_amounts(
                    position_address=position_address,
                    base_delta=Decimal(str(base_added)),
                    quote_delta=Decimal(str(quote_added)),
                    entry_price=Decimal(str(price)) if price else None,
                )
                if existing.status == "CLOSED":
                    existing.status = "OPEN"
                    existing.closed_at = None
            else:
                chain, network_name = request.network.split("-", 1)
                base_symbol = await accounts_service.gateway_client.resolve_token_symbol(
                    chain, network_name, pool_info.get("baseTokenAddress", ""))
                quote_symbol = await accounts_service.gateway_client.resolve_token_symbol(
                    chain, network_name, pool_info.get("quoteTokenAddress", ""))
                await repo.create_position({
                    "position_address": position_address,
                    "pool_address": request.pool_address,
                    "connector": request.connector.split("/")[0],
                    "network": request.network,
                    "wallet_address": wallet_address,
                    "base_token": base_symbol,
                    "quote_token": quote_symbol,
                    "trading_pair": f"{base_symbol}-{quote_symbol}",
                    "initial_base_token_amount": base_added,
                    "initial_quote_token_amount": quote_added,
                    "base_token_amount": base_added,
                    "quote_token_amount": quote_added,
                    "position_rent": Decimal(str(position_rent)) if position_rent is not None else None,
                    "entry_price": price,
                    "current_price": price,
                })
        logger.info(f"Booked AMM position {position_address}: +{base_added} base, +{quote_added} quote")
    except Exception as db_error:
        logger.error(f"Error booking AMM position {position_address}: {db_error}", exc_info=True)


async def _record_event(
    db_manager: AsyncDatabaseManager,
    result: Dict[str, Any],
    *,
    event_type: str,
    connector: str,
    network: str,
    wallet_address: str,
    pool_address: str,
    position_address: Optional[str],
    base_amount_key: str,
    quote_amount_key: str,
    price: Optional[float] = None,
) -> str:
    """Persist one AMM write and return its status in hapi's vocabulary.

    Amounts come from Gateway's `data`, present only once it confirmed the tx; a
    submitted-not-confirmed write records the status with null amounts rather than
    inventing figures. Recording never fails the operation — the liquidity has already
    moved by the time we get here, so a database problem must not surface as a failed
    write to the caller.
    """
    tx_status = get_transaction_status_from_response(result)
    data = result.get("data") or {}
    chain, _ = network.split("-", 1) if "-" in network else (network, "")

    try:
        async with db_manager.get_session_context() as session:
            await GatewayAMMRepository(session).create_event({
                "transaction_hash": result.get("signature") or result.get("txHash") or "",
                "connector": connector,
                "network": network,
                "wallet_address": wallet_address,
                "pool_address": pool_address,
                "position_address": position_address,
                "event_type": event_type,
                "base_token_amount": data.get(base_amount_key),
                "quote_token_amount": data.get(quote_amount_key),
                "price": price,
                "gas_fee": data.get("fee"),
                "gas_token": get_native_gas_token(chain) if data.get("fee") is not None else None,
                "status": tx_status,
            })
        logger.info(f"Recorded AMM {event_type}: {result.get('signature')} (status: {tx_status})")
    except Exception as db_error:
        logger.error(f"Error recording AMM {event_type} event: {db_error}", exc_info=True)

    return tx_status


# ----------------------------- Reads -----------------------------


async def _record_failed_event(
    db_manager: AsyncDatabaseManager,
    error: Exception,
    *,
    event_type: str,
    connector: str,
    network: str,
    wallet_address: str,
    pool_address: str,
    position_address: Optional[str] = None,
) -> None:
    """Record a write that reached the chain and reverted, before the error is re-raised.

    `_record_event` above only runs when Gateway *returns*. A transaction that landed and
    reverted does not return: Gateway raises, the client turns it into a GatewayError, and
    control skips the whole recording block. That is why every row in both event tables
    read CONFIRMED with no error_message — not because nothing had ever failed, but
    because a failure could not be written.

    Only failures carrying a transaction id are recorded: a pre-flight simulation failure
    never got one and cost nothing, while a landed revert has one and paid gas. Recording
    never masks the original failure.
    """
    transaction_hash = transaction_id_from_error(error)
    if not transaction_hash:
        return

    chain, _ = network.split("-", 1) if "-" in network else (network, "")
    try:
        async with db_manager.get_session_context() as session:
            await GatewayAMMRepository(session).create_event({
                "transaction_hash": transaction_hash,
                "connector": connector,
                "network": network,
                "wallet_address": wallet_address,
                "pool_address": pool_address,
                "position_address": position_address,
                "event_type": event_type,
                "status": "FAILED",
                "error_message": str(error),
            })
        logger.error(
            f"AMM {event_type} {transaction_hash} landed on-chain and FAILED on {connector}/"
            f"{network}; recorded. {error}"
        )
    except Exception as db_error:
        logger.error(f"Error recording failed AMM {event_type}: {db_error}", exc_info=True)


@router.get(
    "/amm/pool-info",
    response_model=AMMPoolInfoResponse,
    response_model_by_alias=False,
    dependencies=[Depends(require_gateway_online)],
)
async def get_amm_pool_info(
    connector: str,
    network: str,
    pool_address: str,
    accounts_service: AccountsService = Depends(get_accounts_service),
):
    """Get AMM pool information (reserves, price, base fee) by pool address."""
    try:
        result = check_gateway_error(await accounts_service.gateway_client.amm_pool_info(
            connector=connector, chain_network=network, pool_address=pool_address,
        ))
        return AMMPoolInfoResponse(**result)
    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error getting AMM pool info: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting AMM pool info: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting AMM pool info: {str(e)}")


@router.get(
    "/amm/position-info",
    response_model=AMMPositionInfoResponse,
    response_model_by_alias=False,
    dependencies=[Depends(require_gateway_online)],
)
async def get_amm_position_info(
    connector: str,
    network: str,
    pool_address: str,
    wallet_address: Optional[str] = None,
    accounts_service: AccountsService = Depends(get_accounts_service),
):
    """Get a wallet's aggregate liquidity in an AMM pool plus a per-position breakdown (DAMM v2)."""
    try:
        wallet_address = await _resolve_wallet(accounts_service, network, wallet_address)
        result = check_gateway_error(await accounts_service.gateway_client.amm_position_info(
            connector=connector, chain_network=network, pool_address=pool_address,
            wallet_address=wallet_address,
        ))
        return AMMPositionInfoResponse(**result)
    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error getting AMM position info: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting AMM position info: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting AMM position info: {str(e)}")


@router.post(
    "/amm/positions-owned",
    response_model=List[AMMPositionInfoResponse],
    response_model_by_alias=False,
    dependencies=[Depends(require_gateway_online)],
)
async def get_amm_positions_owned(
    request: AMMPositionsOwnedRequest,
    accounts_service: AccountsService = Depends(get_accounts_service),
):
    """
    List all of a wallet's AMM positions across pools (Meteora DAMM v2 only).

    Fungible-LP AMMs (raydium, uniswap, pancakeswap) have no enumerable positions; Gateway rejects
    them with a 400, surfaced here unchanged. Use position-info with a specific pool address instead.
    """
    try:
        wallet_address = await _resolve_wallet(accounts_service, request.network, request.wallet_address)
        result = check_gateway_error(await accounts_service.gateway_client.amm_positions_owned(
            connector=request.connector, chain_network=request.network, wallet_address=wallet_address,
        ))
        positions = result if isinstance(result, list) else []
        return [AMMPositionInfoResponse(**pos) for pos in positions]
    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error getting AMM positions owned: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting AMM positions owned: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting AMM positions owned: {str(e)}")


@router.post(
    "/amm/quote-liquidity",
    response_model=AMMQuoteLiquidityResponse,
    response_model_by_alias=False,
    dependencies=[Depends(require_gateway_online)],
)
async def quote_amm_liquidity(
    request: AMMQuoteLiquidityRequest,
    accounts_service: AccountsService = Depends(get_accounts_service),
):
    """Quote a two-sided liquidity deposit."""
    try:
        result = check_gateway_error(await accounts_service.gateway_client.amm_quote_liquidity(
            connector=request.connector, chain_network=request.network, pool_address=request.pool_address,
            base_token_amount=float(request.base_token_amount), quote_token_amount=float(request.quote_token_amount),
            slippage_pct=float(request.slippage_pct) if request.slippage_pct is not None else None,
        ))
        return AMMQuoteLiquidityResponse(**result)
    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error quoting AMM liquidity: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error quoting AMM liquidity: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error quoting AMM liquidity: {str(e)}")


# ----------------------------- Writes -----------------------------

@router.post("/amm/add-liquidity", response_model=AMMTransactionResponse, dependencies=[Depends(require_gateway_online)])
async def add_amm_liquidity(
    request: AMMAddLiquidityRequest,
    accounts_service: AccountsService = Depends(get_accounts_service),
    db_manager: AsyncDatabaseManager = Depends(get_database_manager),
):
    """
    Add two-sided liquidity to an AMM pool.

    Meteora DAMM v2: pass position_address to add to that NFT position; omit it to open a new one.
    Fungible-LP AMMs ignore position_address.
    """
    # Bound before the try so the failure recorder in `except` cannot NameError
    # over the top of Gateway's own error when the wallet lookup is what failed.
    wallet_address = ""
    try:
        wallet_address = await _resolve_wallet(accounts_service, request.network, request.wallet_address)
        result = check_gateway_error(await accounts_service.gateway_client.amm_add_liquidity(
            connector=request.connector, chain_network=request.network, wallet_address=wallet_address,
            pool_address=request.pool_address, base_token_amount=float(request.base_token_amount),
            quote_token_amount=float(request.quote_token_amount),
            slippage_pct=float(request.slippage_pct) if request.slippage_pct is not None else None,
            position_address=request.position_address,
        ))
        data = result.get("data") or {}
        confirmed = get_transaction_status_from_response(result) == "CONFIRMED"
        position_address = request.position_address

        pool_info = await _read_pool(accounts_service, request.connector,
                                     request.network, request.pool_address)
        price = float(pool_info["price"]) if pool_info.get("price") else None

        # DAMM v2 positions are NFTs and get tracked individually; fungible-LP AMMs have
        # no position identity, so for them this block is a no-op and the event log — with
        # its price — is the entire record.
        if confirmed and has_nft_positions(request.connector):
            if position_address is None:
                # An add that opened a position names it in the response. This used to be
                # a diff of on-chain positions against tracked ones (GW-6), which could
                # not attribute an address to a transaction and gave up whenever two were
                # new — Gateway generates the NFT keypair, so it is the only thing that
                # can say which position this write created.
                position_address = data.get("positionAddress")
            if position_address:
                await _book_position_add(
                    accounts_service, db_manager, request, wallet_address, position_address,
                    data, pool_info, price)

        tx_status = await _record_event(
            db_manager, result,
            event_type="ADD_LIQUIDITY", connector=request.connector, network=request.network,
            wallet_address=wallet_address, pool_address=request.pool_address,
            position_address=position_address,
            base_amount_key="baseTokenAmountAdded", quote_amount_key="quoteTokenAmountAdded",
            price=price,
        )
        return AMMTransactionResponse(**{**result, "status": tx_status})
    except HTTPException:
        raise
    except GatewayError as e:
        await _record_failed_event(
            db_manager, e, event_type="ADD_LIQUIDITY", connector=request.connector,
            network=request.network, wallet_address=wallet_address,
            pool_address=request.pool_address, position_address=request.position_address,
        )
        raise HTTPException(status_code=e.status, detail=f"Gateway error adding AMM liquidity: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error adding AMM liquidity: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error adding AMM liquidity: {str(e)}")


@router.post("/amm/remove-liquidity", response_model=AMMTransactionResponse, dependencies=[Depends(require_gateway_online)])
async def remove_amm_liquidity(
    request: AMMRemoveLiquidityRequest,
    accounts_service: AccountsService = Depends(get_accounts_service),
    db_manager: AsyncDatabaseManager = Depends(get_database_manager),
):
    """
    Remove liquidity from an AMM pool.

    Meteora DAMM v2 requires position_address (positions are NFTs); Gateway rejects a missing one
    with a 400, surfaced here unchanged, so "remove 100%" is a true exit of the named position.
    """
    # Bound before the try so the failure recorder in `except` cannot NameError
    # over the top of Gateway's own error when the wallet lookup is what failed.
    wallet_address = ""
    try:
        wallet_address = await _resolve_wallet(accounts_service, request.network, request.wallet_address)
        result = check_gateway_error(await accounts_service.gateway_client.amm_remove_liquidity(
            connector=request.connector, chain_network=request.network, wallet_address=wallet_address,
            pool_address=request.pool_address, percentage_to_remove=float(request.percentage_to_remove),
            slippage_pct=float(request.slippage_pct) if request.slippage_pct is not None else None,
            position_address=request.position_address,
        ))
        data = result.get("data") or {}
        pool_info = await _read_pool(accounts_service, request.connector,
                                     request.network, request.pool_address)
        price = float(pool_info["price"]) if pool_info.get("price") else None

        if (get_transaction_status_from_response(result) == "CONFIRMED"
                and has_nft_positions(request.connector) and request.position_address):
            try:
                async with db_manager.get_session_context() as session:
                    repo = GatewayAMMRepository(session)
                    position = await repo.subtract_from_position_amounts(
                        position_address=request.position_address,
                        base_delta=Decimal(str(data.get("baseTokenAmountRemoved") or 0)),
                        quote_delta=Decimal(str(data.get("quoteTokenAmountRemoved") or 0)),
                    )
                    # A 100% remove is the close: Gateway closes the position account in
                    # the same transaction, which is what returns its rent. There is no
                    # separate close route, and positionRentRefunded arrives only on this
                    # path — a partial removal leaves the account open and refunds
                    # nothing, so its absence there is a fact rather than a gap.
                    if position and float(request.percentage_to_remove) >= 100:
                        rent_refunded = data.get("positionRentRefunded")
                        await repo.close_position(
                            request.position_address,
                            position_rent_refunded=(Decimal(str(rent_refunded))
                                                    if rent_refunded is not None else None),
                        )
            except Exception as db_error:
                logger.error(f"Error booking AMM removal for {request.position_address}: "
                             f"{db_error}", exc_info=True)

        tx_status = await _record_event(
            db_manager, result,
            event_type="REMOVE_LIQUIDITY", connector=request.connector, network=request.network,
            wallet_address=wallet_address, pool_address=request.pool_address,
            position_address=request.position_address,
            base_amount_key="baseTokenAmountRemoved", quote_amount_key="quoteTokenAmountRemoved",
            price=price,
        )
        return AMMTransactionResponse(**{**result, "status": tx_status})
    except HTTPException:
        raise
    except GatewayError as e:
        await _record_failed_event(
            db_manager, e, event_type="REMOVE_LIQUIDITY", connector=request.connector,
            network=request.network, wallet_address=wallet_address,
            pool_address=request.pool_address, position_address=request.position_address,
        )
        raise HTTPException(status_code=e.status, detail=f"Gateway error removing AMM liquidity: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error removing AMM liquidity: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error removing AMM liquidity: {str(e)}")


@router.post(
    "/amm/create-pool",
    response_model=AMMCreatePoolResponse,
    response_model_by_alias=False,
    dependencies=[Depends(require_gateway_online)],
)
async def create_amm_pool(
    request: AMMCreatePoolRequest,
    accounts_service: AccountsService = Depends(get_accounts_service),
    db_manager: AsyncDatabaseManager = Depends(get_database_manager),
):
    """
    Create and seed a new AMM pool.

    Seed price priority: initial_price → quote_token_amount ratio → live market price (anti-snipe).
    Connector-specific params ride extra_params under Gateway's own names (configAddress for
    meteora — required there, ammConfigIndex for raydium) — the same contract as clmm open's
    extra_params. Seeding slippage for uniswap/pancakeswap is the standard slippage_pct field.
    """
    try:
        validate_extra_params(request.extra_params, AMM_CREATE_POOL_EXTRA_PARAMS_SPEC,
                              request.connector, "unified /trading/amm/create-pool")
        extra_params = request.extra_params or {}
        if request.connector == "meteora" and not extra_params.get("configAddress"):
            raise HTTPException(
                status_code=400,
                detail="extra_params.configAddress is required for meteora create-pool "
                       "(DAMM v2 pools are created against a config account)."
            )

        wallet_address = await _resolve_wallet(accounts_service, request.network, request.wallet_address)
        result = check_gateway_error(await accounts_service.gateway_client.amm_create_pool(
            connector=request.connector, chain_network=request.network, wallet_address=wallet_address,
            base_token=request.base_token, quote_token=request.quote_token,
            base_token_amount=float(request.base_token_amount),
            quote_token_amount=float(request.quote_token_amount) if request.quote_token_amount is not None else None,
            initial_price=float(request.initial_price) if request.initial_price is not None else None,
            slippage_pct=float(request.slippage_pct) if request.slippage_pct is not None else None,
            extra_params=request.extra_params,
        ))
        # The pool address only exists in the response, so it is read from there
        # rather than the request, which names tokens.
        tx_status = await _record_event(
            db_manager, result,
            event_type="CREATE_POOL", connector=request.connector, network=request.network,
            wallet_address=wallet_address,
            pool_address=result.get("poolAddress") or result.get("pool_address") or "",
            position_address=None,
            base_amount_key="baseTokenAmountAdded", quote_amount_key="quoteTokenAmountAdded",
            # The seed price is in the create response; no pool exists to read yet.
            price=float(result["price"]) if result.get("price") else None,
        )
        return AMMCreatePoolResponse(**{**result, "status": tx_status})
    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error creating AMM pool: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating AMM pool: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error creating AMM pool: {str(e)}")


@router.post("/amm/events/search")
async def search_amm_events(
    connector: Optional[str] = None,
    network: Optional[str] = None,
    wallet_address: Optional[str] = None,
    pool_address: Optional[str] = None,
    event_type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db_manager: AsyncDatabaseManager = Depends(get_database_manager),
):
    """
    Search recorded AMM liquidity writes, newest first.

    This is the AMM history: ADD_LIQUIDITY, REMOVE_LIQUIDITY and CREATE_POOL with their
    on-chain amounts and gas. Current holdings are not here — read those live from
    /gateway/amm/position-info, which is the only authority on them.
    """
    try:
        async with db_manager.get_session_context() as session:
            repo = GatewayAMMRepository(session)
            events = await repo.search_events(
                connector=connector, network=network, wallet_address=wallet_address,
                pool_address=pool_address, event_type=event_type, status=status,
                limit=min(limit, 1000), offset=offset,
            )
            return {
                "data": [repo.event_to_dict(event) for event in events],
                "total_count": len(events),
                "limit": limit,
                "offset": offset,
            }
    except Exception as e:
        logger.error(f"Error searching AMM events: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error searching AMM events: {str(e)}")


@router.post("/amm/positions/search")
async def search_amm_positions(
    connector: Optional[str] = None,
    network: Optional[str] = None,
    wallet_address: Optional[str] = None,
    pool_address: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db_manager: AsyncDatabaseManager = Depends(get_database_manager),
):
    """
    Search tracked AMM positions (Meteora DAMM v2 NFTs), newest first.

    Fungible-LP AMMs never appear here — they have no position identity. Their holdings
    come from /gateway/amm/position-info and their history from /gateway/amm/events/search.
    """
    try:
        async with db_manager.get_session_context() as session:
            repo = GatewayAMMRepository(session)
            positions = await repo.search_positions(
                connector=connector, network=network, wallet_address=wallet_address,
                pool_address=pool_address, status=status, limit=min(limit, 1000), offset=offset,
            )
            return {
                "data": [repo.position_to_dict(position) for position in positions],
                "total_count": len(positions),
                "limit": limit,
                "offset": offset,
            }
    except Exception as e:
        logger.error(f"Error searching AMM positions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error searching AMM positions: {str(e)}")
