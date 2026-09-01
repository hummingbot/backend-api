"""
Gateway Swap Router - Handles DEX swap operations via Hummingbot Gateway.
Dispatches to Gateway's /trading/{router,clmm,amm}/*-swap routes by the connector's
trading type, so any swap provider works:
router connectors (jupiter, 0x, uniswap, pancakeswap) and amm/clmm connectors
(raydium/amm, meteora/clmm, ...).
"""
import logging
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from database import AsyncDatabaseManager
from database.repositories import GatewaySwapRepository
from deps import get_accounts_service, get_database_manager, require_gateway_online
from models import SwapExecuteQuoteRequest, SwapExecuteRequest, SwapExecuteResponse, SwapQuoteRequest, SwapQuoteResponse
from routers.gateway_extras import ExtraParamsSpec, get_transaction_status_from_response, validate_extra_params
from services.accounts_service import AccountsService
from services.gateway_client import GatewayError, check_gateway_error, get_native_gas_token
from utils.trading_pair import split_trading_pair

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Gateway Swaps"], prefix="/gateway")


# Gateway's swap routes pass approximateIfNoExactOut only to the
# Solana router connectors' quote path; every other provider silently ignores it.
SWAP_EXTRA_PARAMS_SPEC: ExtraParamsSpec = {
    "approximateIfNoExactOut": ((bool,), {"jupiter", "dflow", "okx", "titan"}),
}


@router.post("/swap/quote", response_model=SwapQuoteResponse, dependencies=[Depends(require_gateway_online)])
async def get_swap_quote(
    request: SwapQuoteRequest,
    accounts_service: AccountsService = Depends(get_accounts_service)
):
    """
    Get a price quote for a swap.

    Example:
        connector: 'jupiter' (or typed: 'jupiter/router', 'meteora/clmm', 'raydium/amm')
        network: 'solana-mainnet-beta'
        trading_pair: 'SOL-USDC'
        side: 'BUY'
        amount: 1
        slippage_pct: 1  (optional; omit to use the connector's configured slippagePct)
        extra_params: {"approximateIfNoExactOut": false}  # Solana routers

    Returns:
        Quote with price, expected output amount, and execution-safety fields
    """
    try:
        validate_extra_params(request.extra_params, SWAP_EXTRA_PARAMS_SPEC,
                              request.connector, "/trading/{router,clmm,amm}/quote-swap")

        # Parse trading pair
        base, quote = split_trading_pair(request.trading_pair)

        # Get quote from Gateway
        result = check_gateway_error(await accounts_service.gateway_client.quote_swap(
            connector=request.connector,
            chain_network=request.network,
            base_asset=base,
            quote_asset=quote,
            amount=float(request.amount),
            side=request.side,
            slippage_pct=float(request.slippage_pct) if request.slippage_pct is not None else None,
            extra_params=request.extra_params
        ))

        # Re-frame Gateway's token-flow response (tokenIn/tokenOut) into pair terms,
        # passing its execution-safety fields through in snake_case.
        def _dec(key):
            value = result.get(key)
            return Decimal(str(value)) if value is not None else None

        return SwapQuoteResponse(
            base=base,
            quote=quote,
            price=Decimal(str(result.get("price", 0))),
            amount=request.amount,
            amount_in=_dec("amountIn"),
            amount_out=_dec("amountOut"),
            min_amount_out=_dec("minAmountOut"),
            max_amount_in=_dec("maxAmountIn"),
            price_impact_pct=_dec("priceImpactPct"),
            pool_address=result.get("poolAddress"),
            route_path=result.get("routePath"),
            # The handle for /swap/execute-quote. Routers that hold a price return one;
            # pool-scoped connectors do not, because they price at execution.
            quote_id=result.get("quoteId"),
            # Gateway flags an approximated BUY and this dropped it, so a quote whose
            # amount_out was ~2.5% short of the request looked identical to an exact one.
            # The input half of the feature was already wired end to end —
            # approximateIfNoExactOut is accepted through extra_params — so a caller
            # could switch the behaviour off but not find out whether it had happened.
            approximation=result.get("approximation"),
            slippage_pct=(_dec("slippagePct") if result.get("slippagePct") is not None
                          else request.slippage_pct),
        )

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error getting swap quote: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting swap quote: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting swap quote: {str(e)}")


async def _record_and_report_swap(
    *,
    result: dict,
    db_manager: AsyncDatabaseManager,
    accounts_service: AccountsService,
    connector: str,
    network: str,
    wallet_address: str,
    trading_pair: str,
    base: str,
    quote: str,
    side: str,
    requested_amount: Decimal,
    requested_slippage_pct: Optional[Decimal],
) -> SwapExecuteResponse:
    """Book a settled swap and answer with what it did.

    Shared by /swap/execute and /swap/execute-quote: those two differ only in how the
    transaction was produced — priced at execution, or committed from a quote the caller
    already saw — and not at all in what has to be recorded afterwards. One copy is what
    stops the two-step flow from growing its own subtly different accounting.
    """
    transaction_hash = result.get("signature") or result.get("txHash") or result.get("hash")
    if not transaction_hash:
        raise HTTPException(status_code=500, detail="No transaction hash returned from Gateway")

    # Gateway's `data` (present only when it confirmed the tx) speaks token flow:
    # amountIn is the tokenIn amount — quote for BUY, base for SELL — and the DB
    # columns keep that tokenIn/tokenOut denomination.
    data = result.get("data", {})
    amount_in_raw = data.get("amountIn")
    amount_out_raw = data.get("amountOut")

    side = side.upper()
    # Whether the fill is known. A submitted-not-confirmed swap has placeholders
    # below, and publishing those as the fill would restate the request as the
    # result — the defect these fields were added to end.
    fill_known = amount_in_raw is not None and amount_out_raw is not None
    if fill_known:
        input_amount = Decimal(str(amount_in_raw))
        output_amount = Decimal(str(amount_out_raw))
        # Price in quote-per-base for both sides: SELL flows base->quote (out/in),
        # BUY flows quote->base (in/out).
        if side == "SELL":
            price = output_amount / input_amount if input_amount > 0 else Decimal("0")
        else:
            price = input_amount / output_amount if output_amount > 0 else Decimal("0")
    else:
        # Submitted-not-confirmed: only the request leg of the flow is known —
        # requested_amount is base-denominated (SELL: base in, BUY: base out).
        # The unknown leg and price stay 0 placeholders.
        input_amount = requested_amount if side == "SELL" else Decimal("0")
        output_amount = requested_amount if side == "BUY" else Decimal("0")
        price = Decimal("0")

    # Gateway reports the gas it actually paid in the same confirmed `data` block.
    # Record it here rather than leaving the columns null for the poller to fill —
    # the poller only revisits swaps that were still pending, so a swap confirmed
    # on the execute call was never getting its gas recorded at all.
    fee_raw = data.get("fee")
    gas_fee = Decimal(str(fee_raw)) if fee_raw is not None else None
    chain, _ = accounts_service.gateway_client.parse_network_id(network)
    gas_token = get_native_gas_token(chain) if gas_fee is not None else None

    # Prefer the slippage Gateway reports it actually applied over the one the
    # caller asked for: omitting slippage_pct means "use the connector's configured
    # value", and recording the request's None there loses what was really enforced.
    applied_slippage = data.get("slippagePct")
    slippage_pct = (
        Decimal(str(applied_slippage)) if applied_slippage is not None
        else requested_slippage_pct
    )

    # Get transaction status from Gateway response
    tx_status = get_transaction_status_from_response(result)

    # Store swap in database
    try:
        async with db_manager.get_session_context() as session:
            swap_repo = GatewaySwapRepository(session)

            swap_data = {
                "transaction_hash": transaction_hash,
                "network": network,
                # Store the base venue name: a swap on "jupiter" and one on
                # "jupiter/router" are the same venue and must file together.
                "connector": connector.split("/")[0],
                "wallet_address": wallet_address,
                "trading_pair": trading_pair,
                "base_token": base,
                "quote_token": quote,
                "side": side,
                "input_amount": float(input_amount),
                "output_amount": float(output_amount),
                "price": float(price),
                "slippage_pct": float(slippage_pct) if slippage_pct is not None else None,
                "gas_fee": float(gas_fee) if gas_fee is not None else None,
                "gas_token": gas_token,
                "status": tx_status,
                # Set by the pool-scoped routes, which resolve exactly one pool; a
                # router picks its own path across pools and leaves it unset.
                "pool_address": data.get("poolAddress")
            }

            await swap_repo.create_swap(swap_data)
            logger.info(f"Recorded swap in database: {transaction_hash} (status: {tx_status})")
    except Exception as db_error:
        # Log but don't fail the swap - it was submitted successfully
        logger.error(f"Error recording swap in database: {db_error}", exc_info=True)

    return SwapExecuteResponse(
        transaction_hash=transaction_hash,
        trading_pair=trading_pair,
        side=side,
        amount=requested_amount,
        # What actually moved, alongside what was asked for. Same values the swap
        # history row above records, so a caller no longer has to go and read it.
        input_amount=input_amount if fill_known else None,
        output_amount=output_amount if fill_known else None,
        price=price if fill_known else None,
        # "confirmed" / "submitted" / "failed" — a failed EVM swap comes back as
        # status -1 with zeroed amounts, which must not read as in-flight.
        status=tx_status
    )


@router.post("/swap/execute", response_model=SwapExecuteResponse, dependencies=[Depends(require_gateway_online)])
async def execute_swap(
    request: SwapExecuteRequest,
    accounts_service: AccountsService = Depends(get_accounts_service),
    db_manager: AsyncDatabaseManager = Depends(get_database_manager)
):
    """
    Execute a swap transaction via router (Jupiter, 0x).

    Example:
        connector: 'jupiter'
        network: 'solana-mainnet-beta'
        trading_pair: 'SOL-USDC'
        side: 'BUY'
        amount: 1
        slippage_pct: 1  (optional; omit to use the connector's configured slippagePct)
        wallet_address: (optional, uses default if not provided)
        extra_params: {"approximateIfNoExactOut": false}  # Solana routers

    Returns:
        Transaction hash and swap details
    """
    try:
        validate_extra_params(request.extra_params, SWAP_EXTRA_PARAMS_SPEC,
                              request.connector, "/trading/{router,clmm,amm}/execute-swap")

        # Parse network_id (chain needed for wallet lookup)
        chain, network = accounts_service.gateway_client.parse_network_id(request.network)

        # Get wallet address
        wallet_address = await accounts_service.gateway_client.get_wallet_address_or_default(
            chain=chain,
            wallet_address=request.wallet_address
        )

        # Parse trading pair
        base, quote = split_trading_pair(request.trading_pair)

        # Execute swap
        result = check_gateway_error(await accounts_service.gateway_client.execute_swap(
            connector=request.connector,
            chain_network=request.network,
            wallet_address=wallet_address,
            base_asset=base,
            quote_asset=quote,
            amount=float(request.amount),
            side=request.side,
            slippage_pct=float(request.slippage_pct) if request.slippage_pct is not None else None,
            extra_params=request.extra_params
        ))
        return await _record_and_report_swap(
            result=result,
            db_manager=db_manager,
            accounts_service=accounts_service,
            connector=request.connector,
            network=request.network,
            wallet_address=wallet_address,
            trading_pair=request.trading_pair,
            base=base,
            quote=quote,
            side=request.side,
            requested_amount=request.amount,
            requested_slippage_pct=request.slippage_pct,
        )

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error executing swap: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error executing swap: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error executing swap: {str(e)}")


@router.get("/swaps/{transaction_hash}/status")
async def get_swap_status(
    transaction_hash: str,
    db_manager: AsyncDatabaseManager = Depends(get_database_manager)
):
    """
    Get status of a specific swap by transaction hash.

    Args:
        transaction_hash: Transaction hash of the swap

    Returns:
        Swap details including current status
    """
    try:
        async with db_manager.get_session_context() as session:
            swap_repo = GatewaySwapRepository(session)
            swap = await swap_repo.get_swap_by_tx_hash(transaction_hash)

            if not swap:
                raise HTTPException(status_code=404, detail=f"Swap not found: {transaction_hash}")

            return swap_repo.to_dict(swap)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting swap status: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting swap status: {str(e)}")


@router.post("/swap/execute-quote", response_model=SwapExecuteResponse, dependencies=[Depends(require_gateway_online)])
async def execute_swap_quote(
    request: SwapExecuteQuoteRequest,
    accounts_service: AccountsService = Depends(get_accounts_service),
    db_manager: AsyncDatabaseManager = Depends(get_database_manager)
):
    """
    Execute a quote returned by /swap/quote, by its quote_id.

    The two-step flow: quote, decide, then commit to THAT quote. /swap/execute prices
    again at execution, which discards the price the caller saw — the whole reason dflow,
    titan and 0x return a held quote. Router connectors only; a pool-scoped connector has
    no cached quote to execute and is rejected rather than quietly re-priced.

    Example:
        connector: 'jupiter'
        network: 'solana-mainnet-beta'
        quote_id: '<quote_id from /swap/quote>'
        trading_pair: 'SOL-USDC'
        side: 'SELL'
        amount: 0.01

    Returns:
        Transaction hash and what the swap actually moved
    """
    try:
        chain, network = accounts_service.gateway_client.parse_network_id(request.network)
        wallet_address = await accounts_service.gateway_client.get_wallet_address_or_default(
            chain=chain,
            wallet_address=request.wallet_address
        )
        base, quote = split_trading_pair(request.trading_pair)

        try:
            result = check_gateway_error(await accounts_service.gateway_client.execute_quote(
                connector=request.connector,
                chain_network=request.network,
                wallet_address=wallet_address,
                quote_id=request.quote_id,
            ))
        except ValueError as e:
            # A non-router connector, or an unusable pair — the caller's mistake, not a
            # server fault.
            raise HTTPException(status_code=400, detail=str(e))

        return await _record_and_report_swap(
            result=result,
            db_manager=db_manager,
            accounts_service=accounts_service,
            connector=request.connector,
            network=request.network,
            wallet_address=wallet_address,
            trading_pair=request.trading_pair,
            base=base,
            quote=quote,
            side=request.side,
            requested_amount=request.amount,
            # The quote fixed the tolerance when it was taken; Gateway reports the value
            # it applied in the response, which _record_and_report_swap prefers anyway.
            requested_slippage_pct=None,
        )

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error executing quote: {e}")
    except Exception as e:
        logger.error(f"Error executing quote: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error executing quote: {str(e)}")


@router.post("/swaps/search")
async def search_swaps(
    network: Optional[str] = None,
    connector: Optional[str] = None,
    wallet_address: Optional[str] = None,
    trading_pair: Optional[str] = None,
    status: Optional[str] = None,
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
    db_manager: AsyncDatabaseManager = Depends(get_database_manager)
):
    """
    Search swap history with filters.

    Args:
        network: Filter by network (e.g., 'solana-mainnet-beta')
        connector: Filter by connector (e.g., 'jupiter')
        wallet_address: Filter by wallet address
        trading_pair: Filter by trading pair (e.g., 'SOL-USDC')
        status: Filter by status (SUBMITTED, CONFIRMED, FAILED)
        start_time: Start timestamp (unix seconds)
        end_time: End timestamp (unix seconds)
        limit: Max results (default 50, max 1000)
        offset: Pagination offset

    Returns:
        Paginated list of swaps
    """
    try:
        # Validate limit
        if limit > 1000:
            limit = 1000

        async with db_manager.get_session_context() as session:
            swap_repo = GatewaySwapRepository(session)
            swaps = await swap_repo.get_swaps(
                network=network,
                connector=connector,
                wallet_address=wallet_address,
                trading_pair=trading_pair,
                status=status,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                offset=offset
            )

            # Get total count for pagination (simplified - actual count would need separate query)
            has_more = len(swaps) == limit

            return {
                "data": [swap_repo.to_dict(swap) for swap in swaps],
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "has_more": has_more,
                    "total_count": len(swaps) + offset if not has_more else None
                }
            }

    except Exception as e:
        logger.error(f"Error searching swaps: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error searching swaps: {str(e)}")


@router.get("/swaps/summary")
async def get_swaps_summary(
    network: Optional[str] = None,
    wallet_address: Optional[str] = None,
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
    db_manager: AsyncDatabaseManager = Depends(get_database_manager)
):
    """
    Get swap summary statistics.

    Args:
        network: Filter by network
        wallet_address: Filter by wallet address
        start_time: Start timestamp (unix seconds)
        end_time: End timestamp (unix seconds)

    Returns:
        Summary statistics including volume, fees, success rate
    """
    try:
        async with db_manager.get_session_context() as session:
            swap_repo = GatewaySwapRepository(session)
            summary = await swap_repo.get_swaps_summary(
                network=network,
                wallet_address=wallet_address,
                start_time=start_time,
                end_time=end_time
            )
            return summary

    except Exception as e:
        logger.error(f"Error getting swaps summary: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting swaps summary: {str(e)}")
