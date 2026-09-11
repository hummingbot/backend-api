import logging
import math
from datetime import datetime
from typing import Dict, Tuple

from fastapi import APIRouter, Depends, HTTPException
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, TradeType
from starlette import status

from deps import get_accounts_service, get_connector_service, get_trading_history_service
from models import (
    ActiveOrderFilterRequest,
    FundingPaymentFilterRequest,
    OrderFilterRequest,
    PaginatedResponse,
    PositionFilterRequest,
    TradeFilterRequest,
    TradeRequest,
    TradeResponse,
)
from models.accounts import LeverageRequest, PositionModeRequest
from models.pagination import paginate_by_cursor
from services.accounts_service import AccountsService
from services.trading_history_service import TradingHistoryService
from services.unified_connector_service import UnifiedConnectorService

# Create module-specific logger
logger = logging.getLogger(__name__)

router = APIRouter(tags=["Trading"], prefix="/trading")


# Trade Execution
@router.post("/orders", response_model=TradeResponse, status_code=status.HTTP_201_CREATED)
async def place_trade(
    trade_request: TradeRequest,
    accounts_service: AccountsService = Depends(get_accounts_service),
):
    """
    Place a buy or sell order using a specific account and connector.

    Args:
        trade_request: Trading request with account, connector, trading pair, type, amount, etc.
        accounts_service: Injected accounts service

    Returns:
        TradeResponse with order ID and trading details

    Raises:
        HTTPException: 400 for invalid parameters, 404 for account/connector not found, 500 for trade execution errors
    """
    try:
        # Convert string names to enum instances
        trade_type_enum = TradeType[trade_request.trade_type]
        order_type_enum = OrderType[trade_request.order_type]
        position_action_enum = PositionAction[trade_request.position_action]

        order_id = await accounts_service.place_trade(
            account_name=trade_request.account_name,
            connector_name=trade_request.connector_name,
            trading_pair=trade_request.trading_pair,
            trade_type=trade_type_enum,
            amount=trade_request.amount,
            order_type=order_type_enum,
            price=trade_request.price,
            position_action=position_action_enum,
        )

        return TradeResponse(
            order_id=order_id,
            account_name=trade_request.account_name,
            connector_name=trade_request.connector_name,
            trading_pair=trade_request.trading_pair,
            trade_type=trade_request.trade_type,
            amount=trade_request.amount,
            order_type=trade_request.order_type,
            price=trade_request.price,
            status="submitted",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error placing trade: {str(e)}")


@router.post("/{account_name}/{connector_name}/orders/{client_order_id}/cancel")
async def cancel_order(
    account_name: str,
    connector_name: str,
    client_order_id: str,
    accounts_service: AccountsService = Depends(get_accounts_service),
):
    """
    Cancel a specific order by its client order ID.

    Args:
        account_name: Name of the account
        connector_name: Name of the connector
        client_order_id: Client order ID to cancel
        trading_pair: Trading pair for the order
        accounts_service: Injected accounts service

    Returns:
        Success message with cancelled order ID

    Raises:
        HTTPException: 404 if account/connector not found, 500 for cancellation errors
    """
    try:
        cancelled_order_id = await accounts_service.cancel_order(
            account_name=account_name, connector_name=connector_name, client_order_id=client_order_id
        )
        return {"message": f"Order cancellation initiated for {cancelled_order_id}"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error cancelling order: {str(e)}")


@router.post("/positions", response_model=PaginatedResponse)
async def get_positions(
    filter_request: PositionFilterRequest,
    accounts_service: AccountsService = Depends(get_accounts_service),
    connector_service: UnifiedConnectorService = Depends(get_connector_service)
):
    """
    Get current positions across all or filtered perpetual connectors.

    This endpoint fetches real-time position data directly from the connectors,
    including unrealized PnL, leverage, funding fees, and margin information.

    Args:
        filter_request: JSON payload with filtering criteria

    Returns:
        Paginated response with position data and pagination metadata

    Raises:
        HTTPException: 500 if there's an error fetching positions
    """
    try:
        all_positions = []
        all_connectors = connector_service.get_all_trading_connectors()

        # Filter accounts
        accounts_to_check = filter_request.account_names if filter_request.account_names else list(all_connectors.keys())

        for account_name in accounts_to_check:
            if account_name in all_connectors:
                # Filter connectors
                connectors_to_check = (
                    filter_request.connector_names
                    if filter_request.connector_names
                    else list(all_connectors[account_name].keys())
                )

                for connector_name in connectors_to_check:
                    # Only fetch positions from perpetual connectors
                    if connector_name in all_connectors[account_name] and "_perpetual" in connector_name:
                        try:
                            positions = await accounts_service.get_account_positions(account_name, connector_name)
                            # Add cursor-friendly identifier to each position
                            for position in positions:
                                position["_cursor_id"] = f"{account_name}:{connector_name}:{position.get('trading_pair', '')}"
                            all_positions.extend(positions)
                        except Exception as e:
                            # Log error but continue with other connectors
                            logger.warning(f"Failed to get positions for {account_name}/{connector_name}: {e}")

        # Sort by cursor_id and apply cursor-based pagination
        return paginate_by_cursor(all_positions, filter_request.cursor, filter_request.limit)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching positions: {str(e)}")


# Active Orders Management - Real-time from connectors
@router.post("/orders/active", response_model=PaginatedResponse)
async def get_active_orders(
    filter_request: ActiveOrderFilterRequest,
    connector_service: UnifiedConnectorService = Depends(get_connector_service)
):
    """
    Get active (in-flight) orders across all or filtered accounts and connectors.

    This endpoint fetches real-time active orders directly from the connectors' in_flight_orders property,
    providing current order status, fill amounts, and other live order data.

    Args:
        filter_request: JSON payload with filtering criteria

    Returns:
        Paginated response with active order data and pagination metadata

    Raises:
        HTTPException: 500 if there's an error fetching orders
    """
    try:
        all_active_orders = []
        all_connectors = connector_service.get_all_trading_connectors()

        # Use filter request values
        accounts_to_check = filter_request.account_names if filter_request.account_names else list(all_connectors.keys())

        for account_name in accounts_to_check:
            if account_name in all_connectors:
                # Filter connectors
                connectors_to_check = (
                    filter_request.connector_names
                    if filter_request.connector_names
                    else list(all_connectors[account_name].keys())
                )

                for connector_name in connectors_to_check:
                    if connector_name in all_connectors[account_name]:
                        try:
                            connector = all_connectors[account_name][connector_name]
                            # Get in-flight orders directly from connector
                            in_flight_orders = connector.in_flight_orders

                            for client_order_id, order in in_flight_orders.items():
                                # Apply trading pair filter if specified
                                if filter_request.trading_pairs and order.trading_pair not in filter_request.trading_pairs:
                                    continue

                                # Convert to standardized format to match orders search response
                                standardized_order = _standardize_in_flight_order_response(order, account_name, connector_name)
                                standardized_order["_cursor_id"] = client_order_id  # Use client_order_id as cursor
                                all_active_orders.append(standardized_order)

                        except Exception as e:
                            # Log error but continue with other connectors
                            logger.warning(f"Failed to get active orders for {account_name}/{connector_name}: {e}")

        # Sort by cursor_id and apply cursor-based pagination
        return paginate_by_cursor(all_active_orders, filter_request.cursor, filter_request.limit)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching active orders: {str(e)}")


# Historical Order Management - From registry/database
@router.post("/orders/search", response_model=PaginatedResponse)
async def get_orders(
    filter_request: OrderFilterRequest,
    trading_history_service: TradingHistoryService = Depends(get_trading_history_service),
    connector_service: UnifiedConnectorService = Depends(get_connector_service)
):
    """
    Get historical order data across all or filtered accounts from the database/registry.

    Orders come newest first. `pagination.next_cursor` is the cursor of the page's last
    order; pass it back as `cursor` to get the orders older than it. It is `null` on the
    last page. A cursor this route did not hand out is refused with a 400.

    Args:
        filter_request: JSON payload with filtering criteria

    Returns:
        Paginated response with historical order data and pagination metadata
    """
    before = _parse_order_cursor(filter_request.cursor) if filter_request.cursor else None

    try:
        if filter_request.account_names:
            accounts_to_check = filter_request.account_names
        else:
            accounts_to_check = list(connector_service.get_all_trading_connectors().keys())

        # One query over every account, cut at the cursor inside the database, so the page
        # is exactly the `limit` newest orders older than the cursor. The extra row fetched
        # past `limit` is how has_more is known without a second query.
        result = await trading_history_service.search_orders(
            account_names=accounts_to_check,
            connector_names=filter_request.connector_names,
            trading_pairs=filter_request.trading_pairs,
            status=filter_request.status,
            start_time=filter_request.start_time,
            end_time=filter_request.end_time,
            limit=filter_request.limit + 1,
            before=before,
        )
        orders = result["orders"]
        page = orders[: filter_request.limit]
        has_more = len(orders) > filter_request.limit

        return PaginatedResponse(
            data=page,
            pagination={
                "limit": filter_request.limit,
                "has_more": has_more,
                "next_cursor": _order_cursor(page[-1]) if has_more else None,
                "total_count": result["total_count"],
            },
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching orders: {str(e)}")


# An order's cursor is "<created_at ISO timestamp>|<client order id>". An ISO timestamp
# never contains "|", so the first "|" splits it back whatever the order id holds.
_ORDER_CURSOR_SEPARATOR = "|"


def _order_cursor(order: Dict) -> str:
    """The keyset cursor after `order`, built from the fields an order row really carries."""
    return f"{order['created_at']}{_ORDER_CURSOR_SEPARATOR}{order['order_id']}"


def _parse_order_cursor(cursor: str) -> Tuple[datetime, str]:
    """Split a cursor from `_order_cursor` back into (created_at, client_order_id).

    Anything else is refused rather than read as "start over": an unrecognised cursor
    used to serve page one again, which a client walking the history cannot tell from
    genuinely older orders.
    """
    created_at, _, client_order_id = cursor.partition(_ORDER_CURSOR_SEPARATOR)
    try:
        if not client_order_id:
            raise ValueError("no client order id")
        return datetime.fromisoformat(created_at), client_order_id
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid cursor {cursor!r}: pass back the next_cursor of a previous /trading/orders/search page",
        )


# Trade History
@router.post("/trades", response_model=PaginatedResponse)
async def get_trades(
    filter_request: TradeFilterRequest,
    trading_history_service: TradingHistoryService = Depends(get_trading_history_service),
    connector_service: UnifiedConnectorService = Depends(get_connector_service)
):
    """
    Get trade history across all or filtered accounts with complex filtering.

    Args:
        filter_request: JSON payload with filtering criteria

    Returns:
        Paginated response with trade data and pagination metadata
    """
    try:
        all_trades = []

        # Determine which accounts to query
        if filter_request.account_names:
            accounts_to_check = filter_request.account_names
        else:
            # Get all accounts
            all_connectors = connector_service.get_all_trading_connectors()
            accounts_to_check = list(all_connectors.keys())

        # Collect trades from all specified accounts
        for account_name in accounts_to_check:
            try:
                trades = await trading_history_service.get_trades(
                    account_name=account_name,
                    connector_name=(
                        filter_request.connector_names[0]
                        if filter_request.connector_names and len(filter_request.connector_names) == 1
                        else None
                    ),
                    trading_pair=(
                        filter_request.trading_pairs[0]
                        if filter_request.trading_pairs and len(filter_request.trading_pairs) == 1
                        else None
                    ),
                    trade_type=(
                        filter_request.trade_types[0]
                        if filter_request.trade_types and len(filter_request.trade_types) == 1
                        else None
                    ),
                    start_time=filter_request.start_time,
                    end_time=filter_request.end_time,
                    limit=filter_request.limit * 2,  # Get more for filtering
                    offset=0,
                )
                # Add cursor-friendly identifier to each trade
                for trade in trades:
                    trade["_cursor_id"] = f"{trade.get('timestamp', 0)}:{trade.get('trade_id', '')}"
                all_trades.extend(trades)
            except Exception as e:
                # Log error but continue with other accounts
                logger.warning(f"Failed to get trades for {account_name}: {e}")

        # Apply filters for multiple values
        if filter_request.connector_names and len(filter_request.connector_names) > 1:
            all_trades = [trade for trade in all_trades if trade.get("connector_name") in filter_request.connector_names]
        if filter_request.trading_pairs and len(filter_request.trading_pairs) > 1:
            all_trades = [trade for trade in all_trades if trade.get("trading_pair") in filter_request.trading_pairs]
        if filter_request.trade_types and len(filter_request.trade_types) > 1:
            all_trades = [trade for trade in all_trades if trade.get("trade_type") in filter_request.trade_types]

        # Sort by timestamp (most recent first) then cursor_id, and apply cursor-based pagination
        return paginate_by_cursor(
            all_trades,
            filter_request.cursor,
            filter_request.limit,
            sort_key=lambda x: (x.get("timestamp", 0), x.get("_cursor_id", "")),
            reverse=True,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching trades: {str(e)}")


@router.post("/{account_name}/{connector_name}/position-mode")
async def set_position_mode(
    account_name: str,
    connector_name: str,
    request: PositionModeRequest,
    accounts_service: AccountsService = Depends(get_accounts_service),
):
    """
    Set position mode for a perpetual connector.

    Args:
        account_name: Name of the account
        connector_name: Name of the perpetual connector
        position_mode: Position mode to set (HEDGE or ONEWAY)

    Returns:
        Success message with status

    Raises:
        HTTPException: 400 if not a perpetual connector or invalid position mode
    """
    try:
        # Convert string to PositionMode enum
        mode = PositionMode[request.position_mode.upper()]
        result = await accounts_service.set_position_mode(account_name, connector_name, mode)
        return result
    except KeyError:
        raise HTTPException(
            status_code=400, detail=f"Invalid position mode '{request.position_mode}'. Must be 'HEDGE' or 'ONEWAY'"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{account_name}/{connector_name}/position-mode")
async def get_position_mode(
    account_name: str, connector_name: str, accounts_service: AccountsService = Depends(get_accounts_service)
):
    """
    Get current position mode for a perpetual connector.

    Args:
        account_name: Name of the account
        connector_name: Name of the perpetual connector

    Returns:
        Dictionary with current position mode, connector name, and account name

    Raises:
        HTTPException: 400 if not a perpetual connector
    """
    try:
        result = await accounts_service.get_position_mode(account_name, connector_name)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{account_name}/{connector_name}/leverage")
async def set_leverage(
    account_name: str,
    connector_name: str,
    request: LeverageRequest,
    accounts_service: AccountsService = Depends(get_accounts_service),
):
    """
    Set leverage for a specific trading pair on a perpetual connector.

    Args:
        account_name: Name of the account
        connector_name: Name of the perpetual connector
        request: Leverage request with trading pair and leverage value
        accounts_service: Injected accounts service

    Returns:
        Dictionary with success status and message

    Raises:
        HTTPException: 400 for invalid parameters or non-perpetual connector, 404 for account/connector
            not found, 500 for execution errors
    """
    try:
        result = await accounts_service.set_leverage(
            account_name=account_name, connector_name=connector_name, trading_pair=request.trading_pair, leverage=request.leverage
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error setting leverage: {str(e)}")


@router.post("/funding-payments", response_model=PaginatedResponse)
async def get_funding_payments(
    filter_request: FundingPaymentFilterRequest,
    trading_history_service: TradingHistoryService = Depends(get_trading_history_service),
    connector_service: UnifiedConnectorService = Depends(get_connector_service)
):
    """
    Get funding payment history across all or filtered perpetual connectors.

    This endpoint retrieves historical funding payment records including
    funding rates, payment amounts, and position data at time of payment.

    Args:
        filter_request: JSON payload with filtering criteria

    Returns:
        Paginated response with funding payment data and pagination metadata

    Raises:
        HTTPException: 500 if there's an error fetching funding payments
    """
    try:
        all_funding_payments = []
        all_connectors = connector_service.get_all_trading_connectors()

        # Filter accounts
        accounts_to_check = filter_request.account_names if filter_request.account_names else list(all_connectors.keys())

        for account_name in accounts_to_check:
            if account_name in all_connectors:
                # Filter connectors
                connectors_to_check = (
                    filter_request.connector_names
                    if filter_request.connector_names
                    else list(all_connectors[account_name].keys())
                )

                for connector_name in connectors_to_check:
                    # Only fetch funding payments from perpetual connectors
                    if connector_name in all_connectors[account_name] and "_perpetual" in connector_name:
                        try:
                            payments = await trading_history_service.get_funding_payments(
                                account_name=account_name,
                                connector_name=connector_name,
                                trading_pair=filter_request.trading_pair,
                                limit=filter_request.limit * 2,  # Get more for pagination
                            )
                            # Add cursor-friendly identifier to each payment
                            for payment in payments:
                                payment["_cursor_id"] = (
                                    f"{account_name}:{connector_name}:"
                                    f"{payment.get('timestamp', '')}:{payment.get('trading_pair', '')}"
                                )
                            all_funding_payments.extend(payments)
                        except Exception as e:
                            # Log error but continue with other connectors
                            logger.warning(f"Failed to get funding payments for {account_name}/{connector_name}: {e}")

        # Sort by timestamp (most recent first) then cursor_id, and apply cursor-based pagination
        return paginate_by_cursor(
            all_funding_payments,
            filter_request.cursor,
            filter_request.limit,
            sort_key=lambda x: (x.get("timestamp", ""), x.get("_cursor_id", "")),
            reverse=True,
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching funding payments: {str(e)}")


def _standardize_in_flight_order_response(order, account_name: str, connector_name: str) -> dict:
    """
    Convert a Hummingbot InFlightOrder to standardized format matching the orders search response.

    Args:
        order: Hummingbot InFlightOrder instance
        account_name: Name of the account
        connector_name: Name of the connector

    Returns:
        Dictionary with standardized order format
    """
    # Map OrderState to status strings
    from hummingbot.core.data_type.in_flight_order import OrderState

    status_mapping = {
        OrderState.PENDING_CREATE: "SUBMITTED",
        OrderState.OPEN: "OPEN",
        OrderState.PENDING_CANCEL: "PENDING_CANCEL",  # Cancellation in progress
        OrderState.CANCELED: "CANCELLED",
        OrderState.PARTIALLY_FILLED: "PARTIALLY_FILLED",
        OrderState.FILLED: "FILLED",
        OrderState.FAILED: "FAILED",
        OrderState.PENDING_APPROVAL: "SUBMITTED",
        OrderState.APPROVED: "SUBMITTED",
        OrderState.CREATED: "SUBMITTED",
        OrderState.COMPLETED: "FILLED",
    }

    # Get status string
    status = status_mapping.get(order.current_state, "SUBMITTED")

    # Convert timestamps to ISO format
    from datetime import datetime, timezone

    created_at = datetime.fromtimestamp(order.creation_timestamp, tz=timezone.utc).isoformat()
    updated_at = datetime.fromtimestamp(
        getattr(order, "last_update_timestamp", order.creation_timestamp), tz=timezone.utc
    ).isoformat()

    return {
        "order_id": order.client_order_id,
        "account_name": account_name,
        "connector_name": connector_name,
        "trading_pair": order.trading_pair,
        "trade_type": order.trade_type.name,
        "order_type": order.order_type.name,
        "amount": float(order.amount) if order.amount and not math.isnan(float(order.amount)) else 0,
        "price": float(order.price) if order.price and not math.isnan(float(order.price)) else None,
        "status": status,
        "filled_amount": (
            float(getattr(order, "executed_amount_base", 0) or 0)
            if not math.isnan(float(getattr(order, "executed_amount_base", 0) or 0))
            else 0
        ),
        "average_fill_price": (
            float(getattr(order, "last_executed_price", 0))
            if getattr(order, "last_executed_price", None)
            and not math.isnan(float(getattr(order, "last_executed_price", 0)))
            else None
        ),
        "fee_paid": (
            float(getattr(order, "cumulative_fee_paid_quote", 0))
            if getattr(order, "cumulative_fee_paid_quote", None)
            and not math.isnan(float(getattr(order, "cumulative_fee_paid_quote", 0)))
            else None
        ),
        "fee_currency": None,  # InFlightOrder doesn't store fee currency directly
        "created_at": created_at,
        "updated_at": updated_at,
        "exchange_order_id": order.exchange_order_id,
        "error_message": None,  # InFlightOrder doesn't store error messages
    }
