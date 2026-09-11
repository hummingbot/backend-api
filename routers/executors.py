"""
Executor Router - REST API endpoints for dynamic executor management.

This router enables running Hummingbot executors directly via API
without Docker containers or full strategy setup.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from starlette import status

from config import settings
from deps import get_executor_service, get_market_data_service
from models.executors import (
    CreateExecutorRequest,
    CreateExecutorResponse,
    ExecutorDetailResponse,
    ExecutorFilterRequest,
    ExecutorLogsResponse,
    ExecutorsSummaryResponse,
    OrphanedPositionsResponse,
    PerformanceReportResponse,
    PositionHoldResponse,
    PositionsSummaryResponse,
    StopExecutorRequest,
    StopExecutorResponse,
)
from models.onchain_preparation import OnchainPreparationRequest
from models.pagination import PaginatedResponse
from services.executor_service import ExecutorService
from services.market_data_service import MarketDataService
from utils.trading_pair import InvalidTradingPair, split_trading_pair

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Executors"], prefix="/executors")


@router.post("/onchain/prepare")
async def onchain_preparation(request: OnchainPreparationRequest):
    """Read markets/positions or prepare unsigned instructions through the configured Aomi app."""
    from services.onchain_executor import OnchainExecutor
    from services.onchain_preparation import prepare_onchain

    try:
        async with OnchainExecutor._default_client() as client:
            return await prepare_onchain(request, client, application_id=settings.aomi.preparation_application_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        # Credential-bearing upstream URLs and bodies must not reach API clients.
        raise HTTPException(status_code=502, detail="Aomi market preparation is unavailable")


@router.post("/", response_model=CreateExecutorResponse, status_code=status.HTTP_201_CREATED)
async def create_executor(
    request: CreateExecutorRequest,
    executor_service: ExecutorService = Depends(get_executor_service)
):
    """
    Create and start a new executor.

    Supported executor types:
    - **position_executor**: Single position with triple barrier (stop loss, take profit, time limit)
    - **grid_executor**: Grid trading with multiple levels
    - **dca_executor**: Dollar-cost averaging with multiple entry points
    - **twap_executor**: Time-weighted average price execution
    - **arbitrage_executor**: Cross-exchange arbitrage
    - **xemm_executor**: Cross-exchange market making
    - **order_executor**: Simple order execution
    - **lp_executor**: Liquidity provider position on CLMM DEXs (Meteora, Raydium, etc.)
    - **onchain_executor**: One on-chain transaction bundle through the Aomi Pipeline (stage, simulate, commit)

    The `executor_config` must include:
    - `type`: One of the executor types above
    - `connector_name`: Exchange connector (e.g., "binance", "binance_perpetual")
    - `trading_pair`: Trading pair (e.g., "BTC-USDT")
    - Additional type-specific configuration (see /executors/types/{type}/config for details)

    `onchain_executor` takes no connector: it needs `chain_id` and `mode` ("operation" with `operation`/`arguments`,
    or "calls" with a list of EVM calls); `connector_name`/`trading_pair` are derived from the chain when omitted.

    Returns the created executor ID and initial status.
    """
    try:
        result = await executor_service.create_executor(
            executor_config=request.executor_config,
            account_name=request.account_name,
            controller_id=request.controller_id
        )
        return CreateExecutorResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating executor: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error creating executor: {str(e)}")


@router.post("/search", response_model=PaginatedResponse)
async def list_executors(
    filter_request: ExecutorFilterRequest,
    executor_service: ExecutorService = Depends(get_executor_service)
):
    """
    Get list of executors with optional filtering.

    Returns active executors from memory combined with completed executors from database.

    Filters:
    - `account_names`: Filter by specific accounts
    - `connector_names`: Filter by connectors
    - `trading_pairs`: Filter by trading pairs
    - `executor_types`: Filter by executor types
    - `status`: Filter by status (RUNNING, TERMINATED, etc.)

    Returns paginated list of executor summaries.
    """
    try:
        # Get filtered executors (active from memory + completed from DB)
        executors = await executor_service.get_executors(
            account_name=filter_request.account_names[0] if filter_request.account_names else None,
            connector_name=filter_request.connector_names[0] if filter_request.connector_names else None,
            trading_pair=filter_request.trading_pairs[0] if filter_request.trading_pairs else None,
            executor_type=filter_request.executor_types[0] if filter_request.executor_types else None,
            status=filter_request.status,
            controller_id=filter_request.controller_ids[0] if filter_request.controller_ids else None
        )

        # Apply additional multi-value filters
        if filter_request.account_names and len(filter_request.account_names) > 1:
            executors = [e for e in executors if e.get("account_name") in filter_request.account_names]
        if filter_request.connector_names and len(filter_request.connector_names) > 1:
            executors = [e for e in executors if e.get("connector_name") in filter_request.connector_names]
        if filter_request.trading_pairs and len(filter_request.trading_pairs) > 1:
            executors = [e for e in executors if e.get("trading_pair") in filter_request.trading_pairs]
        if filter_request.executor_types and len(filter_request.executor_types) > 1:
            executors = [e for e in executors if e.get("executor_type") in filter_request.executor_types]
        if filter_request.controller_ids and len(filter_request.controller_ids) > 1:
            executors = [e for e in executors if e.get("controller_id") in filter_request.controller_ids]

        # Add cursor-friendly identifier to each executor (matches trading.py convention)
        for ex in executors:
            ex["_cursor_id"] = f"{ex.get('created_at') or ''}:{ex.get('executor_id', '')}"

        # Sort by created_at (most recent first) and then by cursor_id for consistency
        executors.sort(key=lambda x: (x.get("created_at") or "", x["_cursor_id"]), reverse=True)

        # Apply cursor-based pagination
        start_idx = 0
        if filter_request.cursor:
            for i, ex in enumerate(executors):
                if ex.get("_cursor_id") == filter_request.cursor:
                    start_idx = i + 1
                    break

        end_idx = start_idx + filter_request.limit
        page_data = executors[start_idx:end_idx]
        has_more = end_idx < len(executors)
        next_cursor = page_data[-1]["_cursor_id"] if page_data and has_more else None

        # Clean up cursor_id from response data
        for ex in page_data:
            ex.pop("_cursor_id", None)

        return PaginatedResponse(
            data=page_data,
            pagination={
                "limit": filter_request.limit,
                "has_more": has_more,
                "next_cursor": next_cursor,
                "total_count": len(executors)
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing executors: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error listing executors: {str(e)}")


@router.get("/lending/policy")
async def get_lending_policy():
    """Read the operator-owned grant; creation revalidates it under the database lock."""
    from config import settings
    from services.lending_policy import LendingPolicy

    try:
        policy = LendingPolicy.load(settings.aomi.lending_policy_file)
        return policy.report() if policy else {"enabled": False}
    except Exception:
        raise HTTPException(status_code=503, detail="Lending policy unavailable")


@router.get("/lending/positions")
async def get_lending_positions(executor_service: ExecutorService = Depends(get_executor_service)):
    """Attributed contributions and unresolved attempts, including completed executors.

    Raw amounts are strings. These are not live balances or available withdrawals;
    receipt-token balances must be reconciled separately. A storage or history error
    returns 503, never a partial list or a zero balance.
    """
    try:
        return {"positions": await executor_service.get_lending_positions()}
    except Exception:
        logger.exception("Lending position history is unavailable")
        raise HTTPException(status_code=503, detail="Lending position history is unavailable")


@router.get("/summary", response_model=ExecutorsSummaryResponse)
async def get_executors_summary(
    executor_service: ExecutorService = Depends(get_executor_service)
):
    """
    Get summary statistics for all executors.

    Returns aggregate information including:
    - Total active/completed executor counts
    - Total PnL and volume
    - Breakdown by executor type, connector, and status
    """
    try:
        summary = executor_service.get_summary()
        return ExecutorsSummaryResponse(**summary)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting executor summary: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting summary: {str(e)}")


@router.get("/performance", response_model=PerformanceReportResponse)
async def get_performance_report(
    controller_id: Optional[str] = None,
    executor_service: ExecutorService = Depends(get_executor_service),
    market_data_service: MarketDataService = Depends(get_market_data_service)
):
    """
    Get a performance report for executors.

    Aggregates metrics from all completed executors (optionally filtered by controller_id):
    - Realized PnL (from completed executors, excluding POSITION_HOLD close type)
    - Unrealized PnL (from active executors + position holds)
    - Global PnL (realized + unrealized)
    - Fees and volume totals
    - Win rate and Sharpe ratio
    - Breakdown by executor type
    - Active position count

    Query parameters:
    - **controller_id**: Filter by controller ID (omit for all controllers)
    """
    try:
        report = await executor_service.get_performance_report(
            controller_id=controller_id,
            market_data_service=market_data_service
        )
        return PerformanceReportResponse(**report)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating performance report: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error generating performance report: {str(e)}")


@router.get("/{executor_id}/logs", response_model=ExecutorLogsResponse)
async def get_executor_logs(
    executor_id: str,
    level: Optional[str] = None,
    limit: int = 50,
    executor_service: ExecutorService = Depends(get_executor_service)
):
    """
    Get captured log entries for a specific executor.

    Returns log entries from the in-memory ring buffer. Only available for
    active executors - logs are cleared when the executor completes.

    Query parameters:
    - **level**: Filter by log level (ERROR, WARNING, INFO, DEBUG)
    - **limit**: Maximum entries to return (default 50)
    """
    try:
        all_logs = executor_service.get_executor_logs(executor_id, level=level)
        total_count = len(all_logs)
        limited_logs = all_logs[-limit:] if limit else all_logs

        return ExecutorLogsResponse(
            executor_id=executor_id,
            logs=limited_logs,
            total_count=total_count,
        )
    except Exception as e:
        logger.error(f"Error getting logs for executor {executor_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting executor logs: {str(e)}")


@router.get("/types/available")
async def get_available_executor_types():
    """
    Get list of available executor types with descriptions.

    Returns information about each supported executor type.
    """
    return {
        "executor_types": [
            {
                "type": "position_executor",
                "description": "Single position with triple barrier (stop loss, take profit, time limit)",
                "use_case": "Directional trading with risk management"
            },
            {
                "type": "grid_executor",
                "description": "Grid trading with multiple buy/sell levels",
                "use_case": "Range-bound market trading"
            },
            {
                "type": "dca_executor",
                "description": "Dollar-cost averaging with multiple entry points",
                "use_case": "Gradual position building"
            },
            {
                "type": "twap_executor",
                "description": "Time-weighted average price execution",
                "use_case": "Large order execution with minimal market impact"
            },
            {
                "type": "arbitrage_executor",
                "description": "Cross-exchange price arbitrage",
                "use_case": "Exploiting price differences between exchanges"
            },
            {
                "type": "xemm_executor",
                "description": "Cross-exchange market making",
                "use_case": "Providing liquidity across exchanges"
            },
            {
                "type": "order_executor",
                "description": "Simple order execution with retry logic",
                "use_case": "Basic order placement with reliability"
            },
            {
                "type": "lp_executor",
                "description": "LP position management for CLMM pools (Meteora, Raydium) ",
                "use_case": "Automated liquidity provision with position tracking"
            },
            {
                "type": "onchain_executor",
                "description": "One on-chain transaction bundle through the Aomi Pipeline: stage, fork-simulate, commit",
                "use_case": "Kernel-signed EVM transactions (transfers, contract calls, catalog operations) without a connector"
            }
        ]
    }


@router.get("/{executor_id}", response_model=ExecutorDetailResponse)
async def get_executor(
    executor_id: str,
    executor_service: ExecutorService = Depends(get_executor_service)
):
    """
    Get detailed information about a specific executor.

    Checks active executors in memory first, then falls back to database for completed executors.

    Returns full executor information including:
    - Current status and PnL
    - Full configuration
    - Executor-specific custom information
    """
    try:
        executor = await executor_service.get_executor(executor_id)

        if not executor:
            raise HTTPException(status_code=404, detail=f"Executor {executor_id} not found")

        return ExecutorDetailResponse(**executor)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting executor {executor_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting executor: {str(e)}")


@router.post("/{executor_id}/stop", response_model=StopExecutorResponse)
async def stop_executor(
    executor_id: str,
    request: StopExecutorRequest,
    executor_service: ExecutorService = Depends(get_executor_service)
):
    """
    Stop an active executor.

    Options:
    - `keep_position`: If true, keeps any open position (for position executors).
      If false, the executor will attempt to close all positions before stopping.

    Returns confirmation of the stop action.
    """
    try:
        result = await executor_service.stop_executor(
            executor_id=executor_id,
            keep_position=request.keep_position
        )
        return StopExecutorResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error stopping executor {executor_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error stopping executor: {str(e)}")


# ========================================
# Position Hold Endpoints
# ========================================

@router.get("/positions/orphaned", response_model=OrphanedPositionsResponse)
async def get_orphaned_positions(
    executor_service: ExecutorService = Depends(get_executor_service)
):
    """
    List terminated executors that may still own an on-chain position.

    Covers three orphan classes:
    - Involuntary holds: close_type POSITION_HOLD with hold_reason set (an LP close
      that exhausted its retries): the position is live on-chain with no automated
      owner.
    - Legacy FAILED records whose final state still reported a position_address
      (force-stop stragglers, records persisted by older executors).
    - Executors terminated by SYSTEM_CLEANUP after an API restart: their on-chain
      state was never persisted, so they need external reconciliation.

    This is a DB-side listing. Before recovering, cross-check candidates against
    on-chain reality via the gateway positions-owned endpoints
    (/trading/clmm/positions-owned, /trading/amm/positions-owned).
    """
    try:
        orphans = await executor_service.get_orphaned_positions()
        return OrphanedPositionsResponse(count=len(orphans), orphans=orphans)
    except Exception as e:
        logger.error(f"Error listing orphaned positions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error listing orphaned positions: {str(e)}")


@router.post("/{executor_id}/resolve-orphan")
async def resolve_orphaned_position(
    executor_id: str,
    executor_service: ExecutorService = Depends(get_executor_service)
):
    """
    Mark an orphaned position as recovered.

    Call after the stranded on-chain position has been closed (or adopted)
    externally. Removes the executor from /executors/positions/orphaned and from
    agent-facing orphan warnings. Only valid for terminated executors that are
    orphan candidates: an involuntary hold (POSITION_HOLD with hold_reason or the
    orphaned_position flag), FAILED, or SYSTEM_CLEANUP.
    """
    try:
        return await executor_service.resolve_orphaned_position(executor_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resolving orphaned position for {executor_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error resolving orphaned position: {str(e)}")


@router.get("/positions/summary", response_model=PositionsSummaryResponse)
async def get_positions_summary(
    controller_id: Optional[str] = None,
    executor_service: ExecutorService = Depends(get_executor_service),
    market_data_service: MarketDataService = Depends(get_market_data_service)
):
    """
    Get summary of all held positions from executors stopped with keep_position=True.

    Returns aggregate information including:
    - Total number of active position holds
    - Total realized PnL across all positions
    - Total unrealized PnL (when market rates are available)
    - List of all positions with breakeven prices and PnL

    Query parameters:
    - **controller_id**: Filter positions by controller ID
    """
    try:
        positions = executor_service.get_positions_held(controller_id=controller_id)
        total_realized_pnl = sum(float(p.realized_pnl_quote) for p in positions)
        total_unrealized_pnl = None
        position_responses = []

        for p in positions:
            unrealized_pnl = None
            # A pair whose base symbol contains a hyphen used to split into three parts
            # and fail this length check, leaving the PnL silently absent. Tolerance is
            # still right here — one unreadable pair should not fail the whole listing —
            # but it now applies only to pairs that really are unreadable.
            try:
                base, quote = split_trading_pair(p.trading_pair)
            except InvalidTradingPair:
                base = quote = None
            if base and quote:
                rate = market_data_service.get_rate(base, quote)
                if rate is not None:
                    unrealized_pnl = float(p.get_unrealized_pnl(rate))
                    if total_unrealized_pnl is None:
                        total_unrealized_pnl = 0.0
                    total_unrealized_pnl += unrealized_pnl

            position_responses.append(PositionHoldResponse(
                trading_pair=p.trading_pair,
                connector_name=p.connector_name,
                account_name=p.account_name,
                controller_id=p.controller_id,
                buy_amount_base=float(p.buy_amount_base),
                buy_amount_quote=float(p.buy_amount_quote),
                sell_amount_base=float(p.sell_amount_base),
                sell_amount_quote=float(p.sell_amount_quote),
                net_amount_base=float(p.net_amount_base),
                buy_breakeven_price=float(p.buy_breakeven_price) if p.buy_breakeven_price else None,
                sell_breakeven_price=float(p.sell_breakeven_price) if p.sell_breakeven_price else None,
                matched_amount_base=float(p.matched_amount_base),
                unmatched_amount_base=float(p.unmatched_amount_base),
                position_side=p.position_side,
                realized_pnl_quote=float(p.realized_pnl_quote),
                cum_fees_quote=float(p.cum_fees_quote),
                unrealized_pnl_quote=unrealized_pnl,
                executor_count=len(p.executor_ids),
                executor_ids=p.executor_ids,
                last_updated=p.last_updated.isoformat() if p.last_updated else None
            ))

        return PositionsSummaryResponse(
            total_positions=len(positions),
            total_realized_pnl=total_realized_pnl,
            total_unrealized_pnl=total_unrealized_pnl,
            positions=position_responses
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting positions summary: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting positions summary: {str(e)}")


@router.get("/positions/{connector_name}/{trading_pair}", response_model=PositionHoldResponse)
async def get_position_held(
    connector_name: str,
    trading_pair: str,
    account_name: str = "master_account",
    controller_id: str = "main",
    executor_service: ExecutorService = Depends(get_executor_service),
    market_data_service: MarketDataService = Depends(get_market_data_service)
):
    """
    Get held position for a specific connector/trading pair.

    Returns the aggregated position from executors stopped with keep_position=True,
    including breakeven prices, matched/unmatched volume, realized PnL, and unrealized PnL.

    Query parameters:
    - **controller_id**: Controller ID (default "main")
    """
    try:
        position = executor_service.get_position_held(
            account_name=account_name,
            connector_name=connector_name,
            trading_pair=trading_pair,
            controller_id=controller_id
        )

        if not position:
            raise HTTPException(
                status_code=404,
                detail=f"No position hold found for {connector_name}/{trading_pair}"
            )

        unrealized_pnl = None
        try:
            base, quote = split_trading_pair(trading_pair)
        except InvalidTradingPair:
            base = quote = None
        if base and quote:
            rate = market_data_service.get_rate(base, quote)
            if rate is not None:
                unrealized_pnl = float(position.get_unrealized_pnl(rate))

        return PositionHoldResponse(
            trading_pair=position.trading_pair,
            connector_name=position.connector_name,
            account_name=position.account_name,
            controller_id=position.controller_id,
            buy_amount_base=float(position.buy_amount_base),
            buy_amount_quote=float(position.buy_amount_quote),
            sell_amount_base=float(position.sell_amount_base),
            sell_amount_quote=float(position.sell_amount_quote),
            net_amount_base=float(position.net_amount_base),
            buy_breakeven_price=float(position.buy_breakeven_price) if position.buy_breakeven_price else None,
            sell_breakeven_price=float(position.sell_breakeven_price) if position.sell_breakeven_price else None,
            matched_amount_base=float(position.matched_amount_base),
            unmatched_amount_base=float(position.unmatched_amount_base),
            position_side=position.position_side,
            realized_pnl_quote=float(position.realized_pnl_quote),
            cum_fees_quote=float(position.cum_fees_quote),
            unrealized_pnl_quote=unrealized_pnl,
            executor_count=len(position.executor_ids),
            executor_ids=position.executor_ids,
            last_updated=position.last_updated.isoformat() if position.last_updated else None
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting position: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting position: {str(e)}")


@router.delete("/positions/{connector_name}/{trading_pair}")
async def clear_position_held(
    connector_name: str,
    trading_pair: str,
    account_name: str = "master_account",
    controller_id: str = "main",
    executor_service: ExecutorService = Depends(get_executor_service)
):
    """
    Clear a held position (after manual close or full exit).

    This removes the position from tracking but preserves historical data
    in completed executors.

    Query parameters:
    - **controller_id**: Controller ID (default "main")
    """
    try:
        cleared = await executor_service.clear_position_held(
            account_name=account_name,
            connector_name=connector_name,
            trading_pair=trading_pair,
            controller_id=controller_id
        )

        if not cleared:
            raise HTTPException(
                status_code=404,
                detail=f"No position hold found for {connector_name}/{trading_pair}"
            )

        return {
            "message": f"Position hold for {connector_name}/{trading_pair} cleared",
            "connector_name": connector_name,
            "trading_pair": trading_pair
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error clearing position: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error clearing position: {str(e)}")


def _extract_field_info(schema: dict, definitions: dict) -> list:
    """
    Extract field information from a JSON schema.

    Returns list of field dicts with: name, type, description, required, default, constraints
    """
    fields = []
    properties = schema.get("properties", {})
    required_fields = set(schema.get("required", []))

    for field_name, field_schema in properties.items():
        # Skip internal fields
        if field_name.startswith("_"):
            continue

        field_info = {
            "name": field_name,
            "required": field_name in required_fields,
        }

        # Resolve $ref if present
        if "$ref" in field_schema:
            ref_path = field_schema["$ref"].split("/")[-1]
            if ref_path in definitions:
                field_schema = {**definitions[ref_path], **field_schema}
                del field_schema["$ref"]

        # Handle anyOf (usually Optional types)
        if "anyOf" in field_schema:
            types = []
            for option in field_schema["anyOf"]:
                if "$ref" in option:
                    ref_name = option["$ref"].split("/")[-1]
                    types.append(ref_name)
                elif option.get("type") == "null":
                    field_info["required"] = False
                else:
                    types.append(option.get("type", "any"))
            field_info["type"] = types[0] if len(types) == 1 else f"Union[{', '.join(types)}]"
        elif "allOf" in field_schema:
            # Handle allOf (usually inheritance)
            refs = [opt["$ref"].split("/")[-1] for opt in field_schema["allOf"] if "$ref" in opt]
            field_info["type"] = refs[0] if refs else "object"
        elif "enum" in field_schema:
            field_info["type"] = "enum"
            field_info["enum_values"] = field_schema["enum"]
        elif "type" in field_schema:
            field_info["type"] = field_schema["type"]
        else:
            field_info["type"] = "any"

        # Extract description
        if "description" in field_schema:
            field_info["description"] = field_schema["description"]
        elif "title" in field_schema:
            field_info["description"] = field_schema["title"]

        # Extract default value
        if "default" in field_schema:
            field_info["default"] = field_schema["default"]

        # Extract constraints
        constraints = {}
        if "minimum" in field_schema:
            constraints["minimum"] = field_schema["minimum"]
        if "maximum" in field_schema:
            constraints["maximum"] = field_schema["maximum"]
        if "exclusiveMinimum" in field_schema:
            constraints["exclusive_minimum"] = field_schema["exclusiveMinimum"]
        if "exclusiveMaximum" in field_schema:
            constraints["exclusive_maximum"] = field_schema["exclusiveMaximum"]
        if "minLength" in field_schema:
            constraints["min_length"] = field_schema["minLength"]
        if "maxLength" in field_schema:
            constraints["max_length"] = field_schema["maxLength"]
        if "pattern" in field_schema:
            constraints["pattern"] = field_schema["pattern"]
        if "ge" in field_schema:
            constraints["ge"] = field_schema["ge"]
        if "le" in field_schema:
            constraints["le"] = field_schema["le"]
        if "gt" in field_schema:
            constraints["gt"] = field_schema["gt"]
        if "lt" in field_schema:
            constraints["lt"] = field_schema["lt"]

        if constraints:
            field_info["constraints"] = constraints

        fields.append(field_info)

    return fields


@router.get("/types/{executor_type}/config")
async def get_executor_config_schema(executor_type: str):
    """
    Get configuration schema for a specific executor type.

    Returns detailed information about each configuration field including:
    - **name**: Field name
    - **type**: Data type (str, int, Decimal, enum, etc.)
    - **description**: Field description
    - **required**: Whether the field is required
    - **default**: Default value if any
    - **constraints**: Validation constraints (min, max, pattern, etc.)
    - **enum_values**: Possible values for enum types

    Also returns nested type definitions for complex fields.
    """
    from services.executor_service import ExecutorService

    if executor_type not in ExecutorService.EXECUTOR_REGISTRY:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown executor type '{executor_type}'. Valid types: {list(ExecutorService.EXECUTOR_REGISTRY.keys())}"
        )

    _, config_class = ExecutorService.EXECUTOR_REGISTRY[executor_type]

    try:
        # Get JSON schema from pydantic model
        schema = config_class.model_json_schema()
        definitions = schema.get("$defs", {})

        # Extract field information
        fields = _extract_field_info(schema, definitions)

        # Extract nested type definitions
        nested_types = {}
        for def_name, def_schema in definitions.items():
            if "properties" in def_schema:
                nested_types[def_name] = {
                    "description": def_schema.get("description", def_schema.get("title", "")),
                    "fields": _extract_field_info(def_schema, definitions)
                }
            elif "enum" in def_schema:
                nested_types[def_name] = {
                    "type": "enum",
                    "values": def_schema["enum"],
                    "description": def_schema.get("description", def_schema.get("title", ""))
                }

        return {
            "executor_type": executor_type,
            "config_class": config_class.__name__,
            "description": schema.get("description", schema.get("title", "")),
            "fields": fields,
            "nested_types": nested_types
        }

    except Exception as e:
        logger.error(f"Error extracting config schema for {executor_type}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error extracting config schema: {str(e)}"
        )
