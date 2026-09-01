"""
Gateway CLMM Router - Handles DEX CLMM liquidity operations via Hummingbot Gateway.
Supports CLMM connectors (Meteora, Raydium, Uniswap V3) for concentrated liquidity positions.
"""
import asyncio
import logging
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from database import AsyncDatabaseManager
from database.repositories import GatewayCLMMRepository
from deps import GATEWAY_UNAVAILABLE_DETAIL, get_accounts_service, get_database_manager, require_gateway_online
from models import (
    AMMCreatePoolResponse,
    CLMMAddLiquidityRequest,
    CLMMClosePositionRequest,
    CLMMClosePositionResponse,
    CLMMCollectFeesRequest,
    CLMMCollectFeesResponse,
    CLMMCreatePoolRequest,
    CLMMOpenPositionRequest,
    CLMMOpenPositionResponse,
    CLMMPoolInfoResponse,
    CLMMPoolListItem,
    CLMMPoolListResponse,
    CLMMPositionInfo,
    CLMMPositionsOwnedRequest,
    CLMMQuotePositionRequest,
    CLMMQuotePositionResponse,
    CLMMRemoveLiquidityRequest,
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

router = APIRouter(tags=["Gateway CLMM"], prefix="/gateway")

# Gateway's unified open/add destructure ONLY strategyType, and only Meteora
# consumes it (Spot=0 / Curve=1).
CLMM_LIQUIDITY_EXTRA_PARAMS_SPEC: ExtraParamsSpec = {
    "strategyType": ((int,), {"meteora"}),
}

# Gateway's unified create-pool destructure, per consuming connector: binStep
# (meteora bin step / orca tick spacing), feeBps (meteora; required V3 fee tier
# for uniswap/pancakeswap), ammConfigIndex (raydium, pancakeswap-sol).
CLMM_CREATE_POOL_EXTRA_PARAMS_SPEC: ExtraParamsSpec = {
    "binStep": ((int,), {"meteora", "orca"}),
    "feeBps": ((int, float), {"meteora", "uniswap", "pancakeswap"}),
    "ammConfigIndex": ((int,), {"raydium", "pancakeswap-sol"}),
}


async def _refresh_position_data(position, accounts_service: AccountsService, clmm_repo: GatewayCLMMRepository):
    """
    Refresh position data from Gateway and update database.

    This updates:
    - in_range status
    - liquidity amounts
    - pending fees
    - position status (if closed externally)
    """
    try:
        # Get wallet address for the position
        wallet_address = position.wallet_address

        # Get all positions for this pool and find our specific position
        try:
            # check_gateway_error is critical here: a Gateway HTTP error must raise (and skip
            # the refresh) rather than flow onward and mark the position CLOSED below.
            positions_list = check_gateway_error(await accounts_service.gateway_client.clmm_positions_owned(
                connector=position.connector,
                chain_network=position.network,  # position.network is already in 'chain-network' format
                wallet_address=wallet_address
            ))

            # Find our specific position in the list
            result = None
            if isinstance(positions_list, list):
                for pos in positions_list:
                    if pos.get("address") == position.position_address:
                        result = pos
                        break

            # Absent from a single positions-owned read: could be closed externally,
            # could be a lagging RPC node. Closing is owned by the poller's
            # consecutive-miss gate (and the zero-liquidity check below) so one
            # refresh can never close a live position.
            if result is None:
                logger.info(f"Position {position.position_address} absent from positions-owned; "
                            "skipping update (poller's miss-gate owns close detection)")
                return

        except Exception as e:
            # If we can't fetch positions, log error but don't mark as closed
            logger.error(f"Error fetching position from Gateway: {e}")
            return

        # Extract current state
        current_price = Decimal(str(result.get("price", 0)))
        lower_price = Decimal(str(result.get("lowerPrice", 0))) if result.get("lowerPrice") else Decimal("0")
        upper_price = Decimal(str(result.get("upperPrice", 0))) if result.get("upperPrice") else Decimal("0")

        # Calculate in_range status
        in_range = "UNKNOWN"
        if current_price > 0 and lower_price > 0 and upper_price > 0:
            if lower_price <= current_price <= upper_price:
                in_range = "IN_RANGE"
            else:
                in_range = "OUT_OF_RANGE"

        # Extract token amounts
        base_token_amount = Decimal(str(result.get("baseTokenAmount", 0)))
        quote_token_amount = Decimal(str(result.get("quoteTokenAmount", 0)))

        # Check if position has been closed (zero liquidity)
        if base_token_amount == 0 and quote_token_amount == 0:
            logger.info(f"Position {position.position_address} has zero liquidity, marking as CLOSED")
            await clmm_repo.close_position(position.position_address)
            return

        # Update liquidity amounts, in_range status, and current price
        await clmm_repo.update_position_liquidity(
            position_address=position.position_address,
            base_token_amount=base_token_amount,
            quote_token_amount=quote_token_amount,
            in_range=in_range,
            current_price=current_price
        )

        # Always write pending fees — 0 is a real value (e.g. right after an
        # external collect); the old non-zero guard left stale pendings forever.
        base_fee_pending = Decimal(str(result.get("baseFeeAmount", 0)))
        quote_fee_pending = Decimal(str(result.get("quoteFeeAmount", 0)))

        await clmm_repo.update_position_fees(
            position_address=position.position_address,
            base_fee_pending=base_fee_pending,
            quote_fee_pending=quote_fee_pending
        )

        logger.debug(f"Refreshed position {position.position_address}: price={current_price}, in_range={in_range}, "
                     f"base={base_token_amount}, quote={quote_token_amount}")

    except Exception as e:
        logger.error(f"Error refreshing position {position.position_address}: {e}", exc_info=True)
        raise


async def _record_failed_write(
    db_manager: AsyncDatabaseManager,
    error: Exception,
    *,
    event_type: str,
    position_address: Optional[str],
) -> None:
    """Record a write that reached the chain and reverted, before the error is re-raised.

    The recording code below only runs when Gateway *returns*. A transaction that landed
    and reverted does not return: Gateway raises, the client turns it into a GatewayError,
    and control skips every `create_event` call to land in an `except` that persists
    nothing. So the database said every operation ever attempted had succeeded, while a
    close that reverted at slot 440494812 — costing 0.000011772 SOL — left no row at all.

    Only failures carrying a transaction id are recorded. A pre-flight simulation failure
    never got one and cost nothing, and inventing an identifier for it would put a row in
    the table that no lookup by hash could ever match.

    Recording never masks the original failure: the caller still gets Gateway's error.
    """
    transaction_hash = transaction_id_from_error(error)
    if not transaction_hash or not position_address:
        return

    try:
        async with db_manager.get_session_context() as session:
            repo = GatewayCLMMRepository(session)
            position = await repo.get_position_by_address(position_address)
            if position is None:
                logger.warning(
                    f"CLMM {event_type} {transaction_hash} reverted on-chain for position "
                    f"{position_address}, which has no database record — no event written."
                )
                return
            await repo.create_event({
                "position_id": position.id,
                "transaction_hash": transaction_hash,
                "event_type": event_type,
                "status": "FAILED",
                "error_message": str(error),
            })
        logger.error(
            f"CLMM {event_type} {transaction_hash} landed on-chain and FAILED for position "
            f"{position_address}; recorded. {error}"
        )
    except Exception as db_error:
        logger.error(f"Error recording failed CLMM {event_type}: {db_error}", exc_info=True)


@router.get(
    "/clmm/pool-info",
    response_model=CLMMPoolInfoResponse,
    response_model_by_alias=False,
    dependencies=[Depends(require_gateway_online)],
)
async def get_clmm_pool_info(
    connector: str,
    network: str,
    pool_address: str,
    bin_count: int = 0,
    accounts_service: AccountsService = Depends(get_accounts_service)
):
    """
    Get detailed information about a CLMM pool by pool address.

    Args:
        connector: CLMM connector (e.g., 'meteora', 'raydium')
        network: Network ID in 'chain-network' format (e.g., 'solana-mainnet-beta')
        pool_address: Pool contract address
        bin_count: If > 0, include the per-tick liquidity distribution (`bins`)
            around the active tick. Meteora always returns its bins and ignores
            this; orca, raydium, uniswap and pancakeswap honour it.

    Example:
        GET /gateway/clmm/pool-info?connector=meteora&network=solana-mainnet-beta
            &pool_address=2sf5NYcY4zUPXUSmG6f66mskb24t5F8S11pC1Nz5nQT3

    Returns:
        Pool information including liquidity, price, bins (for Meteora), etc.
        All field names are returned in snake_case format.

    """
    try:
        # Get pool info from Gateway's unified CLMM endpoint
        result = check_gateway_error(await accounts_service.gateway_client.clmm_pool_info(
            connector=connector,
            chain_network=network,
            pool_address=pool_address,
            bin_count=bin_count
        ))

        # Parse the camelCase Gateway response into snake_case Pydantic model
        # The model's aliases will handle the conversion
        return CLMMPoolInfoResponse(**result)

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error getting CLMM pool info: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting CLMM pool info: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting CLMM pool info: {str(e)}")


# Sort keys each connector's upstream actually accepts, mapped to the field name it wants.
# Probed against the live APIs rather than transcribed from a docstring, because the
# docstring was wrong: "volume, tvl, feetvlratio, etc." advertised two keys that 400'd.
# Meteora (dlmm.datapi.meteora.ag) takes exactly these three — bare `volume`, `fees`,
# `fees_24h`, `apr` and `liquidity` are all rejected — and wants "field:direction".
_METEORA_SORT_KEYS = {
    "tvl": "tvl",
    "volume": "volume_24h",
    "feetvlratio": "fee_tvl_ratio_24h",
}
# Orca takes the field alone plus a separate sortDirection.
_ORCA_SORT_KEYS = {"volume", "tvl", "fees", "rewards", "yieldovertvl"}


def _sort_field(connector: str, sort_key: Optional[str]) -> Optional[str]:
    """Translate a sort key to what this connector's upstream accepts, or reject it here.

    An unsupported key used to travel all the way to the DEX's API, come back a bare 400,
    and surface as an opaque hapi 500 — so `feetvlratio`, which the tool documented,
    looked like a server fault rather than a wrong field name.
    """
    if not sort_key:
        return None
    legal = _METEORA_SORT_KEYS if connector == "meteora" else {k: k for k in _ORCA_SORT_KEYS}
    if sort_key not in legal:
        raise HTTPException(
            status_code=400,
            detail=f"sort_key '{sort_key}' is not supported by {connector}. "
                   f"Supported: {', '.join(sorted(legal))}.",
        )
    return legal[sort_key]


@router.get("/clmm/pools", response_model=CLMMPoolListResponse)
async def get_clmm_pools(
    connector: str,
    network: str = Query(
        "mainnet-beta",
        description="Solana network name (bare, e.g. 'mainnet-beta'); meteora/orca are Solana-only"),
    page: int = Query(0, ge=0, description="Page number"),
    limit: int = Query(50, ge=1, le=100, description="Results per page (max 100)"),
    search_term: Optional[str] = Query(None, description="Search query to filter pools"),
    sort_key: Optional[str] = Query(
        "tvl",
        description="Sort key. Defaults to tvl: volume ranks pools by how much others "
                    "traded, while the LP question is how much depth is there — and on a "
                    "quiet pair every pool ties at zero volume, making that order "
                    "arbitrary. meteora: tvl, volume, feetvlratio. "
                    "orca: tvl, volume, fees, rewards, yieldovertvl."),
    order_by: Optional[str] = Query("desc", description="Sort order (asc, desc)"),
    include_unknown: bool = Query(True, description="Include pools with unverified tokens"),
    accounts_service: AccountsService = Depends(get_accounts_service)
):
    """
    Get list of available CLMM pools for a connector via Gateway.

    Supports: meteora, orca

    Args:
        connector: CLMM connector (meteora, orca)
        network: Solana network name (bare, default 'mainnet-beta')
        page: Page number (default: 0)
        limit: Results per page (default: 50, max: 100)
        search_term: Search query to filter pools (optional)
        sort_key: Sort by field. Defaults to tvl — see the parameter description for
            why, and for the keys each connector accepts.
        order_by: Sort order (asc, desc)
        include_unknown: Include pools with unverified tokens

    Example:
        GET /gateway/clmm/pools?connector=meteora&search_term=SOL&limit=20

    Returns:
        List of available pools with trading pairs, addresses, liquidity, volume, APR, etc.
    """
    try:
        # Both listing connectors are Solana-only, which is what makes the
        # "solana-" prefix below safe: the endpoint takes a bare network name while
        # Gateway's unified route keys on chain-network.
        supported_connectors = ["meteora", "orca"]
        if connector.lower() not in supported_connectors:
            raise HTTPException(
                status_code=400,
                detail=f"Pool listing not supported for connector '{connector}'. Supported: {', '.join(supported_connectors)}"
            )

        logger.info(f"Fetching pools from Gateway ({connector}, page={page}, limit={limit}, query={search_term})")

        # The two fetch-pools routes take different params: meteora paginates and
        # filters via page/includeUnverified with "field:direction" sortBy; orca does
        # not paginate and uses sortBy + sortDirection + verifiedOnly.
        sort_field = _sort_field(connector.lower(), sort_key)

        if connector.lower() == "meteora":
            direction = order_by if order_by else "desc"
            gateway_data = check_gateway_error(await accounts_service.gateway_client.clmm_fetch_pools(
                connector="meteora",
                chain_network=f"solana-{network}",
                limit=limit,
                query=search_term,
                sort_by=f"{sort_field}:{direction}" if sort_field else None,
                page=page,
                include_unverified=include_unknown
            ))
        else:  # orca
            if page > 0:
                raise HTTPException(
                    status_code=400,
                    detail="Orca's pool listing does not paginate; page is meteora-only. "
                           "Raise limit instead (max 100)."
                )
            gateway_data = check_gateway_error(await accounts_service.gateway_client.clmm_fetch_pools(
                connector="orca",
                chain_network=f"solana-{network}",
                limit=limit,
                query=search_term,
                sort_by=sort_field,
                sort_direction=order_by,
                verified_only=not include_unknown
            ))

        # Transform Gateway response to our format
        # Both Meteora and Orca now return same format: {pools: [...], total, page, pageSize}
        pools = []
        pool_list = gateway_data.get("pools", [])

        for pool in pool_list:
            trading_pair = pool.get("name", f"{pool.get('baseTokenSymbol', '?')}-{pool.get('quoteTokenSymbol', '?')}")
            pools.append(CLMMPoolListItem(
                address=pool.get("address", ""),
                name=pool.get("name", ""),
                trading_pair=trading_pair,
                mint_x=pool.get("baseTokenAddress", ""),
                mint_y=pool.get("quoteTokenAddress", ""),
                bin_step=pool.get("binStep", 0),
                current_price=Decimal(str(pool.get("price", 0))),
                liquidity=str(pool.get("tvl", "0")),
                apr=Decimal(str(pool.get("apr", 0))) if pool.get("apr") is not None else None,
                apy=Decimal(str(pool.get("apy", 0))) if pool.get("apy") is not None else None,
                volume_24h=Decimal(str(pool.get("volume24h", 0))) if pool.get("volume24h") is not None else None,
                fees_24h=Decimal(str(pool.get("fees24h", 0))) if pool.get("fees24h") is not None else None,
                base_fee_percentage=str(pool.get("baseFee")) if pool.get("baseFee") is not None else None,
            ))

        total = gateway_data.get("total", len(pools))

        return CLMMPoolListResponse(
            pools=pools,
            total=total,
            page=page,
            page_size=limit
        )

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error getting CLMM pools: {e}")
    except Exception as e:
        logger.error(f"Error getting CLMM pools: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting CLMM pools: {str(e)}")


@router.post("/clmm/open", response_model=CLMMOpenPositionResponse, dependencies=[Depends(require_gateway_online)])
async def open_clmm_position(
    request: CLMMOpenPositionRequest,
    accounts_service: AccountsService = Depends(get_accounts_service),
    db_manager: AsyncDatabaseManager = Depends(get_database_manager)
):
    """
    Open a NEW CLMM position with initial liquidity.

    Example:
        connector: 'meteora'
        network: 'solana-mainnet-beta'
        pool_address: '2sf5NYcY4zUPXUSmG6f66mskb24t5F8S11pC1Nz5nQT3'
        lower_price: 150
        upper_price: 250
        base_token_amount: 0.01
        quote_token_amount: 2
        slippage_pct: 1  (optional; omit to use the connector's configured slippagePct)
        wallet_address: (optional)
        extra_params: {"strategyType": 0}  # Meteora-specific

    Returns:
        Transaction hash and position address. position_address is None when the
        transaction was submitted but not yet confirmed — poll the transaction; the
        poller's discovery sweep records the position once it lands on-chain.
    """
    try:
        validate_extra_params(request.extra_params, CLMM_LIQUIDITY_EXTRA_PARAMS_SPEC,
                              request.connector, "unified /trading/clmm/open")

        # Parse network_id
        chain, _ = accounts_service.gateway_client.parse_network_id(request.network)

        # Get wallet address
        wallet_address = await accounts_service.gateway_client.get_wallet_address_or_default(
            chain=chain,
            wallet_address=request.wallet_address
        )

        # Get pool info to extract trading pair for database. Fail loud on Gateway errors —
        # opening a position without knowing its tokens would corrupt the position record.
        pool_info = check_gateway_error(await accounts_service.gateway_client.clmm_pool_info(
            connector=request.connector,
            chain_network=request.network,
            pool_address=request.pool_address
        ))

        # Extract tokens from pool info
        base_token_address = pool_info.get("baseTokenAddress", "")
        quote_token_address = pool_info.get("quoteTokenAddress", "")

        # Extract entry price from pool info (current pool price at time of opening)
        entry_price = float(pool_info.get("price", 0)) if pool_info.get("price") else None
        if entry_price:
            logger.info(f"Entry price for position: {entry_price}")

        # Store full token addresses in the database
        base = base_token_address if base_token_address else "UNKNOWN"
        quote = quote_token_address if quote_token_address else "UNKNOWN"
        trading_pair = f"{base}-{quote}"

        # Open position
        result = check_gateway_error(await accounts_service.gateway_client.clmm_open_position(
            connector=request.connector,
            chain_network=request.network,
            wallet_address=wallet_address,
            pool_address=request.pool_address,
            lower_price=float(request.lower_price),
            upper_price=float(request.upper_price),
            base_token_amount=float(request.base_token_amount) if request.base_token_amount else None,
            quote_token_amount=float(request.quote_token_amount) if request.quote_token_amount else None,
            slippage_pct=float(request.slippage_pct) if request.slippage_pct is not None else None,
            extra_params=request.extra_params
        ))

        transaction_hash = result.get("signature")
        if not transaction_hash:
            raise HTTPException(status_code=500, detail="No transaction signature returned from Gateway")

        # Gateway's OpenPositionResponse carries position details only inside `data`,
        # which is present only for CONFIRMED transactions (the response schema strips
        # any other key, so there is no top-level fallback to read).
        data = result.get("data") or {}
        position_address = data.get("positionAddress")
        tx_status = get_transaction_status_from_response(result)

        if not position_address:
            if tx_status == "CONFIRMED":
                raise HTTPException(
                    status_code=500,
                    detail="Gateway confirmed the open but returned no position address")
            # data present without a position address is the EVM revert shape
            # (uniswap/pancakeswap return the receipt with an empty address when the
            # tx landed but reverted); a negative status is a terminal failure on any
            # chain. Both are definitive failures, not pending submissions.
            if tx_status == "FAILED" or result.get("data") is not None:
                raise HTTPException(
                    status_code=500,
                    detail=f"Open position transaction failed on-chain ({transaction_hash})")
            # Submitted-not-confirmed: the position address is unknowable until the tx
            # lands. The tx IS in flight, so failing here would report a false failure
            # and orphan the position; instead return the signature for the caller to
            # poll — the transaction poller's discovery sweep records the position in
            # the database once it appears on-chain.
            logger.warning(
                f"CLMM open submitted but not confirmed ({transaction_hash}); position address "
                "unknown — the poller's discovery sweep will record the position once it lands")
            return CLMMOpenPositionResponse(
                transaction_hash=transaction_hash,
                position_address=None,
                trading_pair=trading_pair,
                pool_address=request.pool_address,
                lower_price=request.lower_price,
                upper_price=request.upper_price,
                base_token_amount_added=request.base_token_amount,
                quote_token_amount_added=request.quote_token_amount,
                position_rent=None,
                status="submitted",
            )

        # Extract position rent (SOL locked for position NFT)
        position_rent = data.get("positionRent")
        if position_rent:
            logger.info(f"Position rent: {position_rent} SOL")

        # CONFIRMED path: prefer the on-chain amounts over the requested ones —
        # slippage and rounding make them differ, and persisting the request
        # silently diverges the DB from the chain.
        base_amount_added = data.get("baseTokenAmountAdded")
        if base_amount_added is None:
            base_amount_added = float(request.base_token_amount) if request.base_token_amount else 0
        quote_amount_added = data.get("quoteTokenAmountAdded")
        if quote_amount_added is None:
            quote_amount_added = float(request.quote_token_amount) if request.quote_token_amount else 0

        # Calculate percentage: (upper_price - lower_price) / lower_price
        percentage = None
        if request.lower_price and request.upper_price and request.lower_price > 0:
            percentage = float((request.upper_price - request.lower_price) / request.lower_price)
            logger.info(f"Position price range percentage: {percentage:.4f} ({percentage*100:.2f}%)")

        # Extract gas fee from Gateway response
        gas_fee = data.get("fee")
        gas_token = get_native_gas_token(chain)

        # Store position and event in database
        try:
            async with db_manager.get_session_context() as session:
                clmm_repo = GatewayCLMMRepository(session)

                # Create position record
                position_data = {
                    "position_address": position_address,
                    "pool_address": request.pool_address,
                    "network": request.network,
                    "connector": request.connector,
                    "wallet_address": wallet_address,
                    "trading_pair": trading_pair,
                    "base_token": base,
                    "quote_token": quote,
                    "status": "OPEN",
                    "lower_price": float(request.lower_price),
                    "upper_price": float(request.upper_price),
                    "percentage": percentage,
                    "entry_price": entry_price,  # Pool price when position opened
                    "current_price": entry_price,  # Same as entry at open time, updated by poller
                    "initial_base_token_amount": float(base_amount_added),
                    "initial_quote_token_amount": float(quote_amount_added),
                    "position_rent": float(position_rent) if position_rent else None,
                    "base_token_amount": float(base_amount_added),
                    "quote_token_amount": float(quote_amount_added),
                    "in_range": "UNKNOWN"  # Will be updated by poller
                }

                position = await clmm_repo.create_position(position_data)
                logger.info(f"Recorded CLMM position in database: {position_address}")

                # Create OPEN event with polled status
                event_data = {
                    "position_id": position.id,
                    "transaction_hash": transaction_hash,
                    "event_type": "OPEN",
                    "base_token_amount": float(base_amount_added) if base_amount_added is not None else None,
                    "quote_token_amount": float(quote_amount_added) if quote_amount_added is not None else None,
                    "gas_fee": float(gas_fee) if gas_fee is not None else None,
                    "gas_token": gas_token,
                    "status": tx_status
                }

                await clmm_repo.create_event(event_data)
                logger.info(f"Recorded CLMM OPEN event in database: {transaction_hash} "
                            f"(status: {tx_status}, gas: {gas_fee} {gas_token})")
        except Exception as db_error:
            # Log but don't fail the operation - it was submitted successfully
            logger.error(f"Error recording CLMM position in database: {db_error}", exc_info=True)

        return CLMMOpenPositionResponse(
            transaction_hash=transaction_hash,
            position_address=position_address,
            trading_pair=trading_pair,
            pool_address=request.pool_address,
            lower_price=request.lower_price,
            upper_price=request.upper_price,
            base_token_amount_added=Decimal(str(base_amount_added)) if base_amount_added is not None else None,
            quote_token_amount_added=Decimal(str(quote_amount_added)) if quote_amount_added is not None else None,
            position_rent=Decimal(str(position_rent)) if position_rent else None,
            status="confirmed"
        )

    except HTTPException:
        raise
    except GatewayError as e:
        # No row for a failed OPEN: gateway_clmm_events keys every row to a position, and
        # an open that reverted created none. The transaction id is in the message and the
        # error reaches the caller; inventing a position to hang it from would be worse
        # than the gap. See GW-26.
        raise HTTPException(status_code=e.status, detail=f"Gateway error opening CLMM position: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error opening CLMM position: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error opening CLMM position: {str(e)}")


@router.post("/clmm/add", dependencies=[Depends(require_gateway_online)])
async def add_liquidity_to_clmm_position(
    request: CLMMAddLiquidityRequest,
    accounts_service: AccountsService = Depends(get_accounts_service),
    db_manager: AsyncDatabaseManager = Depends(get_database_manager)
):
    """
    Add MORE liquidity to an EXISTING CLMM position.

    Example:
        connector: 'meteora'
        network: 'solana-mainnet-beta'
        position_address: '...'
        base_token_amount: 0.5
        quote_token_amount: 50.0
        slippage_pct: 1  (optional; omit to use the connector's configured slippagePct)
        wallet_address: (optional)
        extra_params: {"strategyType": 0}  # Meteora-specific

    Returns:
        Transaction hash
    """
    try:
        validate_extra_params(request.extra_params, CLMM_LIQUIDITY_EXTRA_PARAMS_SPEC,
                              request.connector, "unified /trading/clmm/add")

        # Parse network_id
        chain, _ = accounts_service.gateway_client.parse_network_id(request.network)

        # Get wallet address
        wallet_address = await accounts_service.gateway_client.get_wallet_address_or_default(
            chain=chain,
            wallet_address=request.wallet_address
        )

        # Add liquidity to existing position
        result = check_gateway_error(await accounts_service.gateway_client.clmm_add_liquidity(
            connector=request.connector,
            chain_network=request.network,
            wallet_address=wallet_address,
            position_address=request.position_address,
            base_token_amount=float(request.base_token_amount) if request.base_token_amount else None,
            quote_token_amount=float(request.quote_token_amount) if request.quote_token_amount else None,
            slippage_pct=float(request.slippage_pct) if request.slippage_pct is not None else None,
            extra_params=request.extra_params
        ))

        transaction_hash = result.get("signature") or result.get("txHash") or result.get("hash")
        if not transaction_hash:
            raise HTTPException(status_code=500, detail="No transaction hash returned from Gateway")

        # Get transaction status from Gateway response
        tx_status = get_transaction_status_from_response(result)

        # Extract gas fee from Gateway response
        data = result.get("data", {})
        gas_fee = data.get("fee")
        gas_token = get_native_gas_token(chain)

        # Prefer the CONFIRMED on-chain amounts (data is only present when Gateway
        # confirmed the tx); the requested amounts are the submitted-not-confirmed
        # fallback, reconciled later by the poller.
        base_amount_added = data.get("baseTokenAmountAdded")
        if base_amount_added is None:
            base_amount_added = float(request.base_token_amount) if request.base_token_amount else None
        quote_amount_added = data.get("quoteTokenAmountAdded")
        if quote_amount_added is None:
            quote_amount_added = float(request.quote_token_amount) if request.quote_token_amount else None

        # Pool price at the moment of the add, used to weight the position's entry
        # price. Gateway's add-liquidity response carries no price, so read it from
        # the pool the position sits in. A failure here costs the weighting, not the
        # add — the capital is already deposited.
        add_price = None
        try:
            position_for_pool = None
            async with db_manager.get_session_context() as session:
                position_for_pool = await GatewayCLMMRepository(session).get_position_by_address(
                    request.position_address)
            if position_for_pool:
                pool_info = check_gateway_error(await accounts_service.gateway_client.clmm_pool_info(
                    connector=request.connector,
                    chain_network=request.network,
                    pool_address=position_for_pool.pool_address
                ))
                add_price = float(pool_info.get("price")) if pool_info.get("price") else None
        except Exception as price_error:
            logger.warning(f"Could not read pool price for ADD_LIQUIDITY {transaction_hash}; "
                           f"entry price will not be re-weighted: {price_error}")

        # Store ADD_LIQUIDITY event in database
        try:
            async with db_manager.get_session_context() as session:
                clmm_repo = GatewayCLMMRepository(session)

                # Get position to link event
                position = await clmm_repo.get_position_by_address(request.position_address)
                if position:
                    event_data = {
                        "position_id": position.id,
                        "transaction_hash": transaction_hash,
                        "event_type": "ADD_LIQUIDITY",
                        # `is not None`: 0 is a real amount on single-sided adds
                        "base_token_amount": float(base_amount_added) if base_amount_added is not None else None,
                        "quote_token_amount": float(quote_amount_added) if quote_amount_added is not None else None,
                        "gas_fee": float(gas_fee) if gas_fee is not None else None,
                        "gas_token": gas_token,
                        "status": tx_status
                    }
                    await clmm_repo.create_event(event_data)
                    logger.info(f"Recorded CLMM ADD_LIQUIDITY event: {transaction_hash} "
                                f"(status: {tx_status}, gas: {gas_fee} {gas_token})")

                    # Added capital raises both the PnL baseline and the held amounts.
                    # Book here only when the tx confirmed inline (the event is created
                    # CONFIRMED and the poller never re-processes it); SUBMITTED events
                    # are booked by the poller's confirm path.
                    if tx_status == "CONFIRMED":
                        await clmm_repo.add_to_position_amounts(
                            position_address=request.position_address,
                            base_delta=Decimal(str(base_amount_added or 0)),
                            quote_delta=Decimal(str(quote_amount_added or 0)),
                            entry_price=Decimal(str(add_price)) if add_price else None,
                        )
                else:
                    logger.warning(f"ADD_LIQUIDITY {transaction_hash} executed for position "
                                   f"{request.position_address} with no database record — "
                                   "no event recorded (position may be a pending open "
                                   "not yet discovered)")
        except Exception as db_error:
            logger.error(f"Error recording ADD_LIQUIDITY event: {db_error}", exc_info=True)

        return {
            "transaction_hash": transaction_hash,
            "position_address": request.position_address,
            "base_token_amount_added": base_amount_added,
            "quote_token_amount_added": quote_amount_added,
            "gas_fee": gas_fee,
            "status": tx_status.lower()
        }

    except HTTPException:
        raise
    except GatewayError as e:
        await _record_failed_write(
            db_manager, e, event_type="ADD_LIQUIDITY", position_address=request.position_address
        )
        raise HTTPException(status_code=e.status, detail=f"Gateway error adding liquidity: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error adding liquidity to CLMM position: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error adding liquidity to CLMM position: {str(e)}")


@router.post("/clmm/remove", dependencies=[Depends(require_gateway_online)])
async def remove_liquidity_from_clmm_position(
    request: CLMMRemoveLiquidityRequest,
    accounts_service: AccountsService = Depends(get_accounts_service),
    db_manager: AsyncDatabaseManager = Depends(get_database_manager)
):
    """
    Remove SOME liquidity from a CLMM position (partial removal).

    Example:
        connector: 'meteora'
        network: 'solana-mainnet-beta'
        position_address: '...'
        percentage_to_remove: 50
        slippage_pct: 1  (optional; Orca only — other connectors ignore it)
        wallet_address: (optional)

    Returns:
        Transaction hash
    """
    try:
        # Parse network_id
        chain, _ = accounts_service.gateway_client.parse_network_id(request.network)

        # Get wallet address
        wallet_address = await accounts_service.gateway_client.get_wallet_address_or_default(
            chain=chain,
            wallet_address=request.wallet_address
        )

        # Remove liquidity
        result = check_gateway_error(await accounts_service.gateway_client.clmm_remove_liquidity(
            connector=request.connector,
            chain_network=request.network,
            wallet_address=wallet_address,
            position_address=request.position_address,
            percentage_to_remove=float(request.percentage_to_remove),
            slippage_pct=float(request.slippage_pct) if request.slippage_pct is not None else None
        ))

        transaction_hash = result.get("signature") or result.get("txHash") or result.get("hash")
        if not transaction_hash:
            raise HTTPException(status_code=500, detail="No transaction hash returned from Gateway")

        # Get transaction status from Gateway response
        tx_status = get_transaction_status_from_response(result)

        # Extract gas fee from Gateway response
        data = result.get("data", {})
        gas_fee = data.get("fee")
        gas_token = get_native_gas_token(chain)

        # The CONFIRMED on-chain amounts (data is only present when Gateway confirmed
        # the tx). A percentage alone says nothing about what actually left the pool.
        base_amount_removed = data.get("baseTokenAmountRemoved")
        quote_amount_removed = data.get("quoteTokenAmountRemoved")

        # Store REMOVE_LIQUIDITY event in database
        try:
            async with db_manager.get_session_context() as session:
                clmm_repo = GatewayCLMMRepository(session)

                # Get position to link event
                position = await clmm_repo.get_position_by_address(request.position_address)
                if position:
                    # No "percentage" key: GatewayCLMMEvent has no such column, and the
                    # stray kwarg made create_event raise — silently losing every
                    # REMOVE_LIQUIDITY event to the log-and-continue handler below.
                    event_data = {
                        "position_id": position.id,
                        "transaction_hash": transaction_hash,
                        "event_type": "REMOVE_LIQUIDITY",
                        "base_token_amount": float(base_amount_removed) if base_amount_removed is not None else None,
                        "quote_token_amount": float(quote_amount_removed) if quote_amount_removed is not None else None,
                        "gas_fee": float(gas_fee) if gas_fee is not None else None,
                        "gas_token": gas_token,
                        "status": tx_status
                    }
                    await clmm_repo.create_event(event_data)
                    logger.info(f"Recorded CLMM REMOVE_LIQUIDITY event: {transaction_hash} "
                                f"(status: {tx_status}, gas: {gas_fee} {gas_token})")

                    # Withdrawn capital lowers both the held amounts and the PnL
                    # baseline. Book only on inline confirmation (the event is created
                    # CONFIRMED and the poller never re-processes it); SUBMITTED events
                    # are booked by the poller's confirm path.
                    if tx_status == "CONFIRMED":
                        await clmm_repo.subtract_from_position_amounts(
                            position_address=request.position_address,
                            base_delta=Decimal(str(base_amount_removed or 0)),
                            quote_delta=Decimal(str(quote_amount_removed or 0)),
                        )
                else:
                    logger.warning(f"REMOVE_LIQUIDITY {transaction_hash} executed for position "
                                   f"{request.position_address} with no database record — "
                                   "no event recorded (position may be a pending open "
                                   "not yet discovered)")
        except Exception as db_error:
            logger.error(f"Error recording REMOVE_LIQUIDITY event: {db_error}", exc_info=True)

        return {
            "transaction_hash": transaction_hash,
            "position_address": request.position_address,
            "percentage_to_remove": float(request.percentage_to_remove),
            "base_token_amount_removed": base_amount_removed,
            "quote_token_amount_removed": quote_amount_removed,
            "gas_fee": gas_fee,
            "status": tx_status.lower()
        }

    except HTTPException:
        raise
    except GatewayError as e:
        await _record_failed_write(
            db_manager, e, event_type="REMOVE_LIQUIDITY", position_address=request.position_address
        )
        raise HTTPException(status_code=e.status, detail=f"Gateway error removing liquidity: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error removing liquidity from CLMM position: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error removing liquidity from CLMM position: {str(e)}")


@router.post("/clmm/close", response_model=CLMMClosePositionResponse, dependencies=[Depends(require_gateway_online)])
async def close_clmm_position(
    request: CLMMClosePositionRequest,
    accounts_service: AccountsService = Depends(get_accounts_service),
    db_manager: AsyncDatabaseManager = Depends(get_database_manager)
):
    """
    CLOSE a CLMM position completely (removes all liquidity and collects pending fees).

    Example:
        connector: 'meteora'
        network: 'solana-mainnet-beta'
        position_address: '...'
        wallet_address: (optional)

    Returns:
        Transaction hash and collected fee amounts
    """
    try:
        # Parse network_id
        chain, _ = accounts_service.gateway_client.parse_network_id(request.network)

        # Wallet resolution: an explicit request value wins (same precedence as
        # open/add/remove), then the DB row's wallet, then the default wallet.
        db_wallet = None
        async with db_manager.get_session_context() as session:
            clmm_repo = GatewayCLMMRepository(session)
            db_position = await clmm_repo.get_position_by_address(request.position_address)
            if db_position:
                db_wallet = db_position.wallet_address

        wallet_address = request.wallet_address or db_wallet
        wallet_address = await accounts_service.gateway_client.get_wallet_address_or_default(
            chain=chain,
            wallet_address=wallet_address
        )
        # Note: neither Gateway's close nor the pre-close snapshot (positions_owned)
        # needs the pool — unrecorded positions (e.g. lp_executor opens) close fine
        # without one, so pool_address is informational only.

        # Fetch pending fees and current price BEFORE closing (Gateway doesn't always return these in response)
        base_fee_to_collect = Decimal("0")
        quote_fee_to_collect = Decimal("0")
        close_price = None

        try:
            positions_list = check_gateway_error(await accounts_service.gateway_client.clmm_positions_owned(
                connector=request.connector,
                chain_network=request.network,  # request.network is already in 'chain-network' format
                wallet_address=wallet_address
            ))

            # Find our specific position and get pending fees and current price
            if positions_list and isinstance(positions_list, list):
                for pos in positions_list:
                    if pos and pos.get("address") == request.position_address:
                        base_fee_to_collect = Decimal(str(pos.get("baseFeeAmount", 0)))
                        quote_fee_to_collect = Decimal(str(pos.get("quoteFeeAmount", 0)))
                        close_price = float(pos.get("price", 0)) if pos.get("price") else None
                        logger.info(f"Before closing: price={close_price}, pending fees "
                                    f"base={base_fee_to_collect}, quote={quote_fee_to_collect}")
                        break
            else:
                logger.warning(f"Could not find position {request.position_address} in positions_owned response")
        except Exception as e:
            logger.warning(f"Could not fetch position state before closing: {e}", exc_info=True)

        # Close position
        result = check_gateway_error(await accounts_service.gateway_client.clmm_close_position(
            connector=request.connector,
            chain_network=request.network,
            wallet_address=wallet_address,
            position_address=request.position_address,
            slippage_pct=float(request.slippage_pct) if request.slippage_pct is not None else None,
        ))

        transaction_hash = result.get("signature") or result.get("txHash") or result.get("hash")
        if not transaction_hash:
            raise HTTPException(status_code=500, detail="No transaction hash returned from Gateway")

        # Get transaction status from Gateway response
        tx_status = get_transaction_status_from_response(result)

        # Extract gas fee from Gateway response
        data = result.get("data", {})
        gas_fee = data.get("fee")
        gas_token = get_native_gas_token(chain)

        # Try to extract collected amounts from Gateway response, fallback to pre-fetched amounts
        base_fee_from_response = data.get("baseFeeAmountCollected")
        quote_fee_from_response = data.get("quoteFeeAmountCollected")

        # Use response values if available, otherwise use pre-fetched values
        base_fee_collected = Decimal(str(base_fee_from_response)) if base_fee_from_response is not None else base_fee_to_collect
        quote_fee_collected = (Decimal(str(quote_fee_from_response))
                               if quote_fee_from_response is not None else quote_fee_to_collect)

        # Confirmed-close accounting: what actually left the pool, and the rent the
        # chain refunded for the closed position account (tracked as locked at open
        # via position_rent). None until the transaction confirms.
        base_amount_removed = data.get("baseTokenAmountRemoved")
        quote_amount_removed = data.get("quoteTokenAmountRemoved")
        position_rent_refunded = data.get("positionRentRefunded")

        logger.info(f"Collected fees on close: base={base_fee_collected}, quote={quote_fee_collected}; "
                    f"removed base={base_amount_removed}, quote={quote_amount_removed}; "
                    f"rent refunded={position_rent_refunded}")

        # Store CLOSE event in database and update position
        try:
            async with db_manager.get_session_context() as session:
                clmm_repo = GatewayCLMMRepository(session)

                # Get position to link event
                position = await clmm_repo.get_position_by_address(request.position_address)
                if position:
                    # Create event record
                    event_data = {
                        "position_id": position.id,
                        "transaction_hash": transaction_hash,
                        "event_type": "CLOSE",
                        "base_token_amount": float(base_amount_removed) if base_amount_removed is not None else None,
                        "quote_token_amount": float(quote_amount_removed) if quote_amount_removed is not None else None,
                        "base_fee_collected": float(base_fee_collected) if base_fee_collected is not None else None,
                        "quote_fee_collected": float(quote_fee_collected) if quote_fee_collected is not None else None,
                        "gas_fee": float(gas_fee) if gas_fee is not None else None,
                        "gas_token": gas_token,
                        "status": tx_status
                    }
                    await clmm_repo.create_event(event_data)
                    logger.info(f"Recorded CLMM CLOSE event: {transaction_hash} "
                                f"(status: {tx_status}, gas: {gas_fee} {gas_token})")

                    # Position bookkeeping happens exactly once, when the tx is known
                    # good: CONFIRMED here (the event is created CONFIRMED, so the
                    # poller never touches it), or in the poller's confirm path for
                    # SUBMITTED events. A FAILED tx mutates nothing — the old
                    # unconditional booking permanently inflated *_fee_collected on
                    # failed closes.
                    if tx_status == "CONFIRMED":
                        new_base_collected = Decimal(str(position.base_fee_collected)) + base_fee_collected
                        new_quote_collected = Decimal(str(position.quote_fee_collected)) + quote_fee_collected

                        await clmm_repo.update_position_fees(
                            position_address=request.position_address,
                            base_fee_collected=new_base_collected,
                            quote_fee_collected=new_quote_collected,
                            base_fee_pending=Decimal("0"),
                            quote_fee_pending=Decimal("0")
                        )

                        # Update current_price with close price
                        if close_price:
                            await clmm_repo.update_position_liquidity(
                                position_address=request.position_address,
                                base_token_amount=Decimal(str(position.base_token_amount)),
                                quote_token_amount=Decimal(str(position.quote_token_amount)),
                                current_price=Decimal(str(close_price))
                            )

                        # Verify position is actually gone on Gateway before marking
                        # CLOSED (some connectors 500 instead of 404 for a
                        # nonexistent position — right after our own close, either
                        # means gone).
                        try:
                            await asyncio.sleep(2)  # Wait for transaction to propagate

                            verify_result = await accounts_service.gateway_client.clmm_position_info(
                                connector=request.connector,
                                chain_network=request.network,
                                position_address=request.position_address
                            )

                            if verify_result and isinstance(verify_result, dict) and "error" in verify_result:
                                status_code = verify_result.get("status")
                                if status_code in (404, 500):
                                    await clmm_repo.close_position(
                                        request.position_address,
                                        position_rent_refunded=(Decimal(str(position_rent_refunded))
                                                                if position_rent_refunded is not None else None)
                                    )
                                    logger.info(f"Position {request.position_address} verified as closed "
                                                f"(Gateway returned {status_code})")
                                else:
                                    logger.warning(f"Unexpected error verifying position close: {verify_result}")
                            elif verify_result and "address" in verify_result:
                                # Position still exists - might be a failed close or delayed propagation
                                logger.warning(f"Position {request.position_address} still exists after close "
                                               "transaction. Will be handled by poller.")
                            else:
                                logger.debug("Could not verify position close status, will be handled by poller")

                        except Exception as verify_error:
                            logger.warning(f"Error verifying position close: {verify_error}. Will be handled by poller.")

                        logger.info(f"Updated position {request.position_address}: "
                                    "collected fees updated, pending fees reset to 0.")
                else:
                    # H8 window: a close on a position hapi has no row for (e.g. a
                    # pending open awaiting the discovery sweep) leaves no event —
                    # say so loudly instead of silently skipping.
                    logger.warning(f"CLOSE {transaction_hash} executed for position "
                                   f"{request.position_address} with no database record — "
                                   "no CLOSE event recorded (position may be a pending open "
                                   "not yet discovered)")
        except Exception as db_error:
            logger.error(f"Error recording CLOSE event: {db_error}", exc_info=True)

        return CLMMClosePositionResponse(
            transaction_hash=transaction_hash,
            position_address=request.position_address,
            base_fee_collected=Decimal(str(base_fee_collected)) if base_fee_collected is not None else None,
            quote_fee_collected=Decimal(str(quote_fee_collected)) if quote_fee_collected is not None else None,
            base_token_amount_removed=Decimal(str(base_amount_removed)) if base_amount_removed is not None else None,
            quote_token_amount_removed=Decimal(str(quote_amount_removed)) if quote_amount_removed is not None else None,
            position_rent_refunded=Decimal(str(position_rent_refunded)) if position_rent_refunded is not None else None,
            status=tx_status.lower()
        )

    except HTTPException:
        raise
    except GatewayError as e:
        await _record_failed_write(
            db_manager, e, event_type="CLOSE", position_address=request.position_address
        )
        raise HTTPException(status_code=e.status, detail=f"Gateway error closing CLMM position: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error closing CLMM position: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error closing CLMM position: {str(e)}")


@router.post("/clmm/collect-fees", response_model=CLMMCollectFeesResponse, dependencies=[Depends(require_gateway_online)])
async def collect_fees_from_clmm_position(
    request: CLMMCollectFeesRequest,
    accounts_service: AccountsService = Depends(get_accounts_service),
    db_manager: AsyncDatabaseManager = Depends(get_database_manager)
):
    """
    Collect accumulated fees from a CLMM liquidity position.

    Example:
        connector: 'meteora'
        network: 'solana-mainnet-beta'
        position_address: '...'
        wallet_address: (optional)

    Returns:
        Transaction hash and collected fee amounts
    """
    try:
        # Parse network_id
        chain, _ = accounts_service.gateway_client.parse_network_id(request.network)

        # Wallet resolution: an explicit request value wins (same precedence as
        # open/add/remove), then the DB row's wallet, then the default wallet.
        db_wallet = None
        async with db_manager.get_session_context() as session:
            clmm_repo = GatewayCLMMRepository(session)
            db_position = await clmm_repo.get_position_by_address(request.position_address)
            if db_position:
                db_wallet = db_position.wallet_address

        wallet_address = request.wallet_address or db_wallet
        wallet_address = await accounts_service.gateway_client.get_wallet_address_or_default(
            chain=chain,
            wallet_address=wallet_address
        )
        # Note: neither Gateway's collect nor the fee snapshot (positions_owned) needs
        # the pool — unrecorded positions work without one; pool_address is
        # informational only.

        # Fetch pending fees BEFORE collecting (Gateway doesn't always return collected amounts in response)
        base_fee_to_collect = Decimal("0")
        quote_fee_to_collect = Decimal("0")

        try:
            positions_list = check_gateway_error(await accounts_service.gateway_client.clmm_positions_owned(
                connector=request.connector,
                chain_network=request.network,  # request.network is already in 'chain-network' format
                wallet_address=wallet_address
            ))

            # Find our specific position and get pending fees
            if positions_list and isinstance(positions_list, list):
                for pos in positions_list:
                    if pos and pos.get("address") == request.position_address:
                        base_fee_to_collect = Decimal(str(pos.get("baseFeeAmount", 0)))
                        quote_fee_to_collect = Decimal(str(pos.get("quoteFeeAmount", 0)))
                        logger.info(f"Pending fees before collection: base={base_fee_to_collect}, quote={quote_fee_to_collect}")
                        break
            else:
                logger.warning(f"Could not find position {request.position_address} in positions_owned response")
        except Exception as e:
            logger.warning(f"Could not fetch pending fees before collection: {e}", exc_info=True)

        # Collect fees
        result = check_gateway_error(await accounts_service.gateway_client.clmm_collect_fees(
            connector=request.connector,
            chain_network=request.network,
            wallet_address=wallet_address,
            position_address=request.position_address
        ))

        transaction_hash = result.get("signature") or result.get("txHash") or result.get("hash")
        if not transaction_hash:
            raise HTTPException(status_code=500, detail="No transaction hash returned from Gateway")

        # Get transaction status from Gateway response
        tx_status = get_transaction_status_from_response(result)

        # Try to extract collected amounts from Gateway response, fallback to pre-fetched amounts
        data = result.get("data", {})
        base_fee_from_response = data.get("baseFeeAmountCollected")
        quote_fee_from_response = data.get("quoteFeeAmountCollected")

        # Use response values if available, otherwise use pre-fetched values
        base_fee_collected = Decimal(str(base_fee_from_response)) if base_fee_from_response is not None else base_fee_to_collect
        quote_fee_collected = (Decimal(str(quote_fee_from_response))
                               if quote_fee_from_response is not None else quote_fee_to_collect)

        # Extract gas fee from Gateway response
        gas_fee = data.get("fee")
        gas_token = get_native_gas_token(chain)

        logger.info(f"Collected fees: base={base_fee_collected}, quote={quote_fee_collected}")

        # Store COLLECT_FEES event in database and update position
        try:
            async with db_manager.get_session_context() as session:
                clmm_repo = GatewayCLMMRepository(session)

                # Get position to link event
                position = await clmm_repo.get_position_by_address(request.position_address)
                if position:
                    # Create event record
                    event_data = {
                        "position_id": position.id,
                        "transaction_hash": transaction_hash,
                        "event_type": "COLLECT_FEES",
                        "base_fee_collected": float(base_fee_collected) if base_fee_collected is not None else None,
                        "quote_fee_collected": float(quote_fee_collected) if quote_fee_collected is not None else None,
                        "gas_fee": float(gas_fee) if gas_fee is not None else None,
                        "gas_token": gas_token,
                        "status": tx_status
                    }
                    await clmm_repo.create_event(event_data)
                    logger.info(f"Recorded CLMM COLLECT_FEES event: {transaction_hash} "
                                f"(status: {tx_status}, gas: {gas_fee} {gas_token})")

                    # Book fees exactly once: CONFIRMED here (event created CONFIRMED,
                    # never re-processed), SUBMITTED in the poller's confirm path.
                    # The old unconditional booking double-counted every pending
                    # collect (endpoint + poller) and kept phantom fees on failures.
                    if tx_status == "CONFIRMED":
                        new_base_collected = Decimal(str(position.base_fee_collected)) + base_fee_collected
                        new_quote_collected = Decimal(str(position.quote_fee_collected)) + quote_fee_collected

                        await clmm_repo.update_position_fees(
                            position_address=request.position_address,
                            base_fee_collected=new_base_collected,
                            quote_fee_collected=new_quote_collected,
                            base_fee_pending=Decimal("0"),
                            quote_fee_pending=Decimal("0")
                        )
                        logger.info(f"Updated position {request.position_address}: "
                                    "collected fees updated, pending fees reset to 0")
                else:
                    logger.warning(f"COLLECT_FEES {transaction_hash} executed for position "
                                   f"{request.position_address} with no database record — "
                                   "no event recorded (position may be a pending open "
                                   "not yet discovered)")
        except Exception as db_error:
            logger.error(f"Error recording COLLECT_FEES event: {db_error}", exc_info=True)

        return CLMMCollectFeesResponse(
            transaction_hash=transaction_hash,
            position_address=request.position_address,
            base_fee_collected=Decimal(str(base_fee_collected)) if base_fee_collected is not None else None,
            quote_fee_collected=Decimal(str(quote_fee_collected)) if quote_fee_collected is not None else None,
            status=tx_status.lower()
        )

    except HTTPException:
        raise
    except GatewayError as e:
        await _record_failed_write(
            db_manager, e, event_type="COLLECT_FEES", position_address=request.position_address
        )
        raise HTTPException(status_code=e.status, detail=f"Gateway error collecting fees: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error collecting fees: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error collecting fees: {str(e)}")


@router.post("/clmm/positions_owned", response_model=List[CLMMPositionInfo], dependencies=[Depends(require_gateway_online)])
async def get_clmm_positions_owned(
    request: CLMMPositionsOwnedRequest,
    accounts_service: AccountsService = Depends(get_accounts_service)
):
    """
    Get all CLMM liquidity positions owned by a wallet.

    Mirrors Gateway's /trading/clmm/positions-owned, which takes no pool filter:
    every CLMM position the wallet owns on the connector is returned, each row
    carrying its own pool_address. (The old pool_address request field was a
    silent no-op — Gateway never read it and the response was never filtered.)

    Example:
        connector: 'meteora'
        network: 'solana-mainnet-beta'
        wallet_address: (optional, uses default if not provided)

    Returns:
        List of CLMM position information
    """
    try:
        # Parse network_id
        chain, network = accounts_service.gateway_client.parse_network_id(request.network)

        # Get wallet address
        wallet_address = await accounts_service.gateway_client.get_wallet_address_or_default(
            chain=chain,
            wallet_address=request.wallet_address
        )

        result = check_gateway_error(await accounts_service.gateway_client.clmm_positions_owned(
            connector=request.connector,
            chain_network=request.network,  # request.network is already in 'chain-network' format
            wallet_address=wallet_address
        ))

        # Gateway returns a list directly
        positions_data = result if isinstance(result, list) else []
        positions = []

        for pos in positions_data:
            # Gateway returns token addresses; resolve them against its token list so
            # positions carry real symbols ('SOL-USDC') that trading_pair filters match.
            base_token = await accounts_service.gateway_client.resolve_token_symbol(
                chain, network, pos.get("baseTokenAddress", ""))
            quote_token = await accounts_service.gateway_client.resolve_token_symbol(
                chain, network, pos.get("quoteTokenAddress", ""))
            trading_pair = f"{base_token}-{quote_token}" if base_token and quote_token else ""

            current_price = Decimal(str(pos.get("price", 0)))
            lower_price = Decimal(str(pos.get("lowerPrice", 0))) if pos.get("lowerPrice") else Decimal("0")
            upper_price = Decimal(str(pos.get("upperPrice", 0))) if pos.get("upperPrice") else Decimal("0")

            # Determine if position is in range
            in_range = False
            if current_price > 0 and lower_price > 0 and upper_price > 0:
                in_range = lower_price <= current_price <= upper_price

            positions.append(CLMMPositionInfo(
                position_address=pos.get("address", ""),
                pool_address=pos.get("poolAddress", ""),
                trading_pair=trading_pair,
                base_token=base_token,
                quote_token=quote_token,
                base_token_amount=Decimal(str(pos.get("baseTokenAmount", 0))),
                quote_token_amount=Decimal(str(pos.get("quoteTokenAmount", 0))),
                current_price=current_price,
                lower_price=lower_price,
                upper_price=upper_price,
                # `is not None`, not truthiness: a position with nothing uncollected has
                # fees of 0, and reporting that as None says "Gateway did not tell us"
                # instead of "there are none". The single-position read below already
                # draws the distinction this way.
                base_fee_amount=Decimal(str(pos["baseFeeAmount"])) if pos.get("baseFeeAmount") is not None else None,
                quote_fee_amount=Decimal(str(pos["quoteFeeAmount"])) if pos.get("quoteFeeAmount") is not None else None,
                lower_bin_id=pos.get("lowerBinId"),
                upper_bin_id=pos.get("upperBinId"),
                in_range=in_range
            ))

        return positions

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error getting CLMM positions owned: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting CLMM positions owned: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting CLMM positions owned: {str(e)}")


@router.post(
    "/clmm/quote-position",
    response_model=CLMMQuotePositionResponse,
    response_model_by_alias=False,
    dependencies=[Depends(require_gateway_online)],
)
async def quote_clmm_position(
    request: CLMMQuotePositionRequest,
    accounts_service: AccountsService = Depends(get_accounts_service)
):
    """
    Quote a candidate CLMM position before opening or adding liquidity.

    Mirrors Gateway's GET /trading/clmm/quote-position: returns the base/quote
    split the pool would actually take for the given range and deposit amounts
    (and which side limits it), without signing or submitting anything.
    """
    try:
        result = check_gateway_error(await accounts_service.gateway_client.clmm_quote_position(
            connector=request.connector,
            chain_network=request.network,
            pool_address=request.pool_address,
            lower_price=float(request.lower_price),
            upper_price=float(request.upper_price),
            base_token_amount=float(request.base_token_amount) if request.base_token_amount is not None else None,
            quote_token_amount=float(request.quote_token_amount) if request.quote_token_amount is not None else None,
            slippage_pct=float(request.slippage_pct) if request.slippage_pct is not None else None,
        ))
        return CLMMQuotePositionResponse(**result)

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error quoting CLMM position: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error quoting CLMM position: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error quoting CLMM position: {str(e)}")


@router.post(
    "/clmm/create-pool",
    response_model=AMMCreatePoolResponse,
    response_model_by_alias=False,
    dependencies=[Depends(require_gateway_online)],
)
async def create_clmm_pool(
    request: CLMMCreatePoolRequest,
    accounts_service: AccountsService = Depends(get_accounts_service)
):
    """
    Create a new (empty) CLMM pool — liquidity is added afterwards by opening positions.

    Mirrors Gateway's POST /trading/clmm/create-pool (which shares the AMM
    create-pool response shape). Connector-specific params ride extra_params
    under Gateway's own names — the same contract as open's extra_params.
    """
    try:
        validate_extra_params(request.extra_params, CLMM_CREATE_POOL_EXTRA_PARAMS_SPEC,
                              request.connector, "unified /trading/clmm/create-pool")

        chain, _ = accounts_service.gateway_client.parse_network_id(request.network)
        wallet_address = await accounts_service.gateway_client.get_wallet_address_or_default(
            chain=chain,
            wallet_address=request.wallet_address
        )

        result = check_gateway_error(await accounts_service.gateway_client.clmm_create_pool(
            connector=request.connector,
            chain_network=request.network,
            wallet_address=wallet_address,
            base_token=request.base_token,
            quote_token=request.quote_token,
            initial_price=float(request.initial_price) if request.initial_price is not None else None,
            extra_params=request.extra_params,
        ))
        # Gateway reports status as a number; every other write path maps it to the shared
        # SUBMITTED/CONFIRMED/FAILED vocabulary. Splatting it raw made this route fail
        # validation on every successful create, since the model declares status as a string.
        return AMMCreatePoolResponse(**{**result, "status": get_transaction_status_from_response(result)})

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error creating CLMM pool: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating CLMM pool: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error creating CLMM pool: {str(e)}")


@router.get("/clmm/position-info", response_model=CLMMPositionInfo, dependencies=[Depends(require_gateway_online)])
async def get_clmm_position_info(
    connector: str,
    network: str,
    position_address: str,
    accounts_service: AccountsService = Depends(get_accounts_service)
):
    """
    Get a single CLMM position by its address.

    Mirrors Gateway's GET /trading/clmm/position-info. Gateway reports a missing
    or closed position as an error (500/404), surfaced here as 404.
    """
    try:
        pos = await accounts_service.gateway_client.clmm_position_info(
            connector=connector,
            chain_network=network,
            position_address=position_address
        )
        if pos is None or not isinstance(pos, dict):
            # Connection error: the client returns None — a 503, not a crash.
            raise HTTPException(status_code=503, detail=GATEWAY_UNAVAILABLE_DETAIL)
        if "error" in pos:
            status_code = pos.get("status")
            if status_code in (404, 500):
                raise HTTPException(status_code=404, detail=f"Position {position_address} not found or closed")
            raise HTTPException(status_code=status_code or 502, detail=str(pos.get("error")))

        chain, bare_network = accounts_service.gateway_client.parse_network_id(network)
        base_token = await accounts_service.gateway_client.resolve_token_symbol(
            chain, bare_network, pos.get("baseTokenAddress", ""))
        quote_token = await accounts_service.gateway_client.resolve_token_symbol(
            chain, bare_network, pos.get("quoteTokenAddress", ""))
        current_price = Decimal(str(pos.get("price", 0)))
        lower_price = Decimal(str(pos.get("lowerPrice", 0))) if pos.get("lowerPrice") else Decimal("0")
        upper_price = Decimal(str(pos.get("upperPrice", 0))) if pos.get("upperPrice") else Decimal("0")
        in_range = bool(current_price > 0 and lower_price > 0 and upper_price > 0
                        and lower_price <= current_price <= upper_price)

        return CLMMPositionInfo(
            position_address=pos.get("address", position_address),
            pool_address=pos.get("poolAddress", ""),
            trading_pair=f"{base_token}-{quote_token}" if base_token and quote_token else "",
            base_token=base_token,
            quote_token=quote_token,
            base_token_amount=Decimal(str(pos.get("baseTokenAmount", 0))),
            quote_token_amount=Decimal(str(pos.get("quoteTokenAmount", 0))),
            current_price=current_price,
            lower_price=lower_price,
            upper_price=upper_price,
            base_fee_amount=Decimal(str(pos["baseFeeAmount"])) if pos.get("baseFeeAmount") is not None else None,
            quote_fee_amount=Decimal(str(pos["quoteFeeAmount"])) if pos.get("quoteFeeAmount") is not None else None,
            lower_bin_id=pos.get("lowerBinId"),
            upper_bin_id=pos.get("upperBinId"),
            in_range=in_range
        )

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error getting CLMM position: {e}")
    except Exception as e:
        logger.error(f"Error getting CLMM position info: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting CLMM position info: {str(e)}")


@router.get("/clmm/positions/{position_address}/events")
async def get_clmm_position_events(
    position_address: str,
    event_type: Optional[str] = None,
    limit: int = 100,
    db_manager: AsyncDatabaseManager = Depends(get_database_manager)
):
    """
    Get event history for a CLMM position.

    Args:
        position_address: Position NFT address
        event_type: Filter by event type (OPEN, ADD_LIQUIDITY, REMOVE_LIQUIDITY, COLLECT_FEES, CLOSE,
                    DISCOVERED — written by the poller for positions it found on-chain, with a
                    synthetic discovered_<addr> transaction hash)
        limit: Max events to return

    Returns:
        List of position events
    """
    try:
        async with db_manager.get_session_context() as session:
            clmm_repo = GatewayCLMMRepository(session)
            events = await clmm_repo.get_position_events(
                position_address=position_address,
                event_type=event_type,
                limit=limit
            )

            return {
                "data": [clmm_repo.event_to_dict(event) for event in events],
                "total_count": len(events)
            }

    except Exception as e:
        logger.error(f"Error getting position events: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting position events: {str(e)}")


@router.post("/clmm/positions/search")
async def search_clmm_positions(
    network: Optional[str] = None,
    connector: Optional[str] = None,
    wallet_address: Optional[str] = None,
    trading_pair: Optional[str] = None,
    status: Optional[str] = None,
    position_addresses: Optional[List[str]] = Query(None),
    limit: int = 50,
    offset: int = 0,
    refresh: bool = False,
    db_manager: AsyncDatabaseManager = Depends(get_database_manager),
    accounts_service: AccountsService = Depends(get_accounts_service)
):
    """
    Search CLMM positions with filters.

    Args:
        network: Filter by network (e.g., 'solana-mainnet-beta')
        connector: Filter by connector (e.g., 'meteora')
        wallet_address: Filter by wallet address
        trading_pair: Filter by trading pair (e.g., 'SOL-USDC'; a token outside Gateway's
            token list is stored under its full mint address instead of a symbol)
        status: Filter by status (OPEN, CLOSED)
        position_addresses: Filter by specific position addresses (list of addresses)
        limit: Max results (default 50, max 1000)
        offset: Pagination offset
        refresh: If True, refresh position data from Gateway before returning (default False)

    Returns:
        Paginated list of positions
    """
    try:
        # Validate limit
        if limit > 1000:
            limit = 1000

        # Optionally refresh position data from Gateway first
        if refresh and await accounts_service.gateway_client.ping():
            # Get positions to refresh
            async with db_manager.get_session_context() as session:
                clmm_repo = GatewayCLMMRepository(session)
                positions_to_refresh = await clmm_repo.get_positions(
                    network=network,
                    connector=connector,
                    wallet_address=wallet_address,
                    trading_pair=trading_pair,
                    status=status,
                    position_addresses=position_addresses,
                    limit=limit,
                    offset=offset
                )

                # Extract position addresses and details before closing session
                position_details = [
                    {
                        "position_address": pos.position_address,
                        "pool_address": pos.pool_address,
                        "connector": pos.connector,
                        "network": pos.network,
                        "wallet_address": pos.wallet_address
                    }
                    for pos in positions_to_refresh
                ]

            # Refresh each position in a separate session
            logger.info(f"Refreshing {len(position_details)} positions from Gateway")
            for pos_detail in position_details:
                try:
                    async with db_manager.get_session_context() as session:
                        clmm_repo = GatewayCLMMRepository(session)
                        # Get position again in this session
                        position = await clmm_repo.get_position_by_address(pos_detail["position_address"])
                        if position:
                            await _refresh_position_data(position, accounts_service, clmm_repo)
                except Exception as e:
                    logger.warning(f"Failed to refresh position {pos_detail['position_address']}: {e}")
                    # Continue with other positions even if one fails

        # Get final results after refresh
        async with db_manager.get_session_context() as session:
            clmm_repo = GatewayCLMMRepository(session)
            positions = await clmm_repo.get_positions(
                network=network,
                connector=connector,
                wallet_address=wallet_address,
                trading_pair=trading_pair,
                status=status,
                position_addresses=position_addresses,
                limit=limit,
                offset=offset
            )

            # Get total count for pagination
            has_more = len(positions) == limit

            return {
                "data": [clmm_repo.position_to_dict(pos) for pos in positions],
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "has_more": has_more,
                    "total_count": len(positions) + offset if not has_more else None
                }
            }

    except Exception as e:
        logger.error(f"Error searching CLMM positions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error searching CLMM positions: {str(e)}")
