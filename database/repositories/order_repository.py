import logging
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Order

logger = logging.getLogger(__name__)


class OrderRepository:
    # Client order ids are batched into `IN (...)` clauses of at most this size so a
    # connector with a very large book does not build an unbounded bind-parameter list.
    CLIENT_ID_CHUNK_SIZE = 500

    # Default cap for `get_active_orders`. Callers that need the complete book
    # (connector startup, for instance) must pass `limit=None`.
    DEFAULT_ACTIVE_ORDERS_LIMIT = 1000

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_order(self, order_data: Dict) -> Order:
        """Create a new order record."""
        order = Order(**order_data)
        self.session.add(order)
        await self.session.flush()  # Get the ID
        return order

    async def get_order_by_client_id(self, client_order_id: str) -> Optional[Order]:
        """Get an order by its client order ID."""
        result = await self.session.execute(
            select(Order).where(Order.client_order_id == client_order_id)
        )
        return result.scalar_one_or_none()

    async def get_orders_by_client_ids(self, client_order_ids: List[str]) -> List[Order]:
        """Get the orders matching a batch of client order IDs.

        Batched sibling of `get_order_by_client_id`: one query per `CLIENT_ID_CHUNK_SIZE`
        ids instead of one round trip per id. Ids with no row are simply absent from the
        result; no status filter is applied, so rows already in a terminal state come back
        too and can still be corrected.
        """
        if not client_order_ids:
            return []

        orders: List[Order] = []
        ids = list(client_order_ids)
        for start in range(0, len(ids), self.CLIENT_ID_CHUNK_SIZE):
            chunk = ids[start:start + self.CLIENT_ID_CHUNK_SIZE]
            result = await self.session.execute(
                select(Order).where(Order.client_order_id.in_(chunk))
            )
            orders.extend(result.scalars().all())
        return orders

    async def update_order_status(self, client_order_id: str, status: str,
                                  error_message: Optional[str] = None) -> Optional[Order]:
        """Update order status and optional error message."""
        order = await self.get_order_by_client_id(client_order_id)
        if order:
            order.status = status
            if error_message:
                order.error_message = error_message
            await self.session.flush()
        return order

    async def update_order_fill(self, client_order_id: str, filled_amount: Decimal,
                                average_fill_price: Decimal, fee_paid: Decimal = None,
                                fee_currency: str = None, exchange_order_id: str = None) -> Optional[Order]:
        """Update order with fill information."""
        result = await self.session.execute(
            select(Order).where(Order.client_order_id == client_order_id)
        )
        order = result.scalar_one_or_none()
        if order:
            # Add to existing filled amount instead of replacing
            previous_filled = Decimal(str(order.filled_amount or 0))
            order.filled_amount = float(previous_filled + filled_amount)

            # Update average price (simplified - use latest fill price)
            order.average_fill_price = float(average_fill_price)

            # Add to existing fees
            if fee_paid is not None:
                previous_fee = Decimal(str(order.fee_paid or 0))
                order.fee_paid = float(previous_fee + fee_paid)
            if fee_currency:
                order.fee_currency = fee_currency
            if exchange_order_id:
                order.exchange_order_id = exchange_order_id

            # Update status based on total filled amount
            total_filled = Decimal(str(order.filled_amount))
            if total_filled >= Decimal(str(order.amount)):
                order.status = "FILLED"
            elif total_filled > 0:
                order.status = "PARTIALLY_FILLED"

            await self.session.flush()
        return order

    async def get_orders(self, account_name: Optional[str] = None,
                         connector_name: Optional[str] = None,
                         trading_pair: Optional[str] = None,
                         status: Optional[str] = None,
                         start_time: Optional[int] = None,
                         end_time: Optional[int] = None,
                         limit: int = 100, offset: int = 0) -> List[Order]:
        """Get orders with filtering and pagination."""
        query = select(Order)

        # Apply filters
        if account_name:
            query = query.where(Order.account_name == account_name)
        if connector_name:
            query = query.where(Order.connector_name == connector_name)
        if trading_pair:
            query = query.where(Order.trading_pair == trading_pair)
        if status:
            query = query.where(Order.status == status)
        if start_time:
            start_dt = datetime.fromtimestamp(start_time / 1000)
            query = query.where(Order.created_at >= start_dt)
        if end_time:
            end_dt = datetime.fromtimestamp(end_time / 1000)
            query = query.where(Order.created_at <= end_dt)

        # Apply ordering and pagination
        query = query.order_by(Order.created_at.desc())
        query = query.limit(limit).offset(offset)

        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_active_orders(self, account_name: Optional[str] = None,
                                connector_name: Optional[str] = None,
                                trading_pair: Optional[str] = None,
                                limit: Optional[int] = DEFAULT_ACTIVE_ORDERS_LIMIT) -> List[Order]:
        """Get active orders (SUBMITTED, OPEN, PARTIALLY_FILLED, PENDING_CANCEL).

        `limit` caps how many rows come back, newest first; pass `limit=None` to get the
        whole book with no cap. A query that reaches the cap logs a warning, so a
        truncated result is never silent.
        """
        query = select(Order).where(
            Order.status.in_(["SUBMITTED", "OPEN", "PARTIALLY_FILLED", "PENDING_CANCEL"])
        )

        # Apply filters
        if account_name:
            query = query.where(Order.account_name == account_name)
        if connector_name:
            query = query.where(Order.connector_name == connector_name)
        if trading_pair:
            query = query.where(Order.trading_pair == trading_pair)

        query = query.order_by(Order.created_at.desc())
        if limit is not None:
            query = query.limit(limit)

        result = await self.session.execute(query)
        orders = result.scalars().all()

        if limit is not None and len(orders) >= limit:
            logger.warning(
                f"get_active_orders returned {len(orders)} orders, reaching its limit of {limit}; "
                f"older active orders may have been truncated "
                f"(account={account_name}, connector={connector_name}, trading_pair={trading_pair})"
            )

        return orders

    async def get_orders_summary(self, account_name: Optional[str] = None,
                                 start_time: Optional[int] = None,
                                 end_time: Optional[int] = None) -> Dict:
        """Get order summary statistics using a single DB-level aggregate query."""
        query = select(Order.status, func.count()).group_by(Order.status)

        # Apply the same filters as get_orders
        if account_name:
            query = query.where(Order.account_name == account_name)
        if start_time:
            start_dt = datetime.fromtimestamp(start_time / 1000)
            query = query.where(Order.created_at >= start_dt)
        if end_time:
            end_dt = datetime.fromtimestamp(end_time / 1000)
            query = query.where(Order.created_at <= end_dt)

        result = await self.session.execute(query)
        counts = {status: count for status, count in result.all()}

        total_orders = sum(counts.values())
        filled_orders = counts.get("FILLED", 0)
        cancelled_orders = counts.get("CANCELLED", 0)
        failed_orders = counts.get("FAILED", 0)
        active_orders = (
            counts.get("SUBMITTED", 0) + counts.get("OPEN", 0) + counts.get("PARTIALLY_FILLED", 0)
        )

        return {
            "total_orders": total_orders,
            "filled_orders": filled_orders,
            "cancelled_orders": cancelled_orders,
            "failed_orders": failed_orders,
            "active_orders": active_orders,
            "fill_rate": filled_orders / total_orders if total_orders > 0 else 0,
        }

    def to_dict(self, order: Order) -> Dict:
        """Convert Order model to dictionary format."""
        return {
            "order_id": order.client_order_id,
            "account_name": order.account_name,
            "connector_name": order.connector_name,
            "trading_pair": order.trading_pair,
            "trade_type": order.trade_type,
            "order_type": order.order_type,
            "amount": float(order.amount),
            "price": float(order.price) if order.price else None,
            "status": order.status,
            "filled_amount": float(order.filled_amount),
            "average_fill_price": float(order.average_fill_price) if order.average_fill_price else None,
            "fee_paid": float(order.fee_paid) if order.fee_paid else None,
            "fee_currency": order.fee_currency,
            "created_at": order.created_at.isoformat(),
            "updated_at": order.updated_at.isoformat(),
            "exchange_order_id": order.exchange_order_id,
            "error_message": order.error_message,
        }
