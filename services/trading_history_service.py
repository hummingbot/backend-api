"""
TradingHistoryService provides read-only access to persisted trading history
(orders, trades and funding payments).

This concern was extracted out of the AccountsService god-class: AccountsService
stays focused on account/credential/balance state, while the database read
wrappers for orders/trades/funding live here behind a single session+error
helper (``_run_in_repo``).
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from database import AsyncDatabaseManager, FundingRepository, OrderRepository, TradeRepository

logger = logging.getLogger(__name__)


class TradingHistoryService:
    """Read-only queries over persisted orders, trades and funding payments."""

    def __init__(self, db_manager: AsyncDatabaseManager):
        """
        Initialize the TradingHistoryService.

        Args:
            db_manager: AsyncDatabaseManager for persistence (shared, created once at startup)
        """
        self.db_manager = db_manager

    async def _run_in_repo(self, repo_cls, fn, default, error_message):
        """Run ``fn`` against a freshly constructed repository inside a session.

        Collapses the repeated ``get_session_context + try/except`` scaffold: a
        new session is opened, ``repo_cls(session)`` is built and passed to
        ``fn`` (which performs the read and any to_dict conversion). On any
        exception the error is logged and ``default`` is returned.

        Args:
            repo_cls: Repository class to instantiate with the session.
            fn: Async callable receiving the repository instance.
            default: Value returned (defaults-on-error) if ``fn`` raises. May be
                a callable that receives the raised exception and returns the
                default value (used when the default embeds the error).
            error_message: Prefix used when logging the exception.

        Returns:
            The result of ``fn`` or ``default`` on error.
        """
        try:
            async with self.db_manager.get_session_context() as session:
                return await fn(repo_cls(session))
        except Exception as e:
            logger.error(f"{error_message}: {e}")
            return default(e) if callable(default) else default

    async def search_orders(self, account_names: Optional[List[str]] = None,
                            connector_names: Optional[List[str]] = None,
                            trading_pairs: Optional[List[str]] = None, status: Optional[str] = None,
                            start_time: Optional[int] = None, end_time: Optional[int] = None,
                            limit: int = 100, before: Optional[Tuple[datetime, str]] = None) -> Dict:
        """One page of order history plus the count of every order matching the filters.

        Returns ``{"orders": [...], "total_count": n}``. See ``OrderRepository.get_orders``
        for the ``before`` keyset cursor.
        """
        filters = dict(
            account_names=account_names,
            connector_names=connector_names,
            trading_pairs=trading_pairs,
            status=status,
            start_time=start_time,
            end_time=end_time,
        )

        async def _fn(order_repo):
            orders = await order_repo.get_orders(**filters, limit=limit, before=before)
            return {
                "orders": [order_repo.to_dict(order) for order in orders],
                "total_count": await order_repo.count_orders(**filters),
            }

        return await self._run_in_repo(
            OrderRepository, _fn, {"orders": [], "total_count": 0}, "Error getting orders"
        )

    async def get_active_orders_history(self, account_name: Optional[str] = None, connector_name: Optional[str] = None,
                                        trading_pair: Optional[str] = None) -> List[Dict]:
        """Get active orders from database using OrderRepository."""
        async def _fn(order_repo):
            orders = await order_repo.get_active_orders(
                account_name=account_name,
                connector_name=connector_name,
                trading_pair=trading_pair
            )
            return [order_repo.to_dict(order) for order in orders]

        return await self._run_in_repo(OrderRepository, _fn, [], "Error getting active orders")

    async def get_orders_summary(self, account_name: Optional[str] = None, start_time: Optional[int] = None,
                                 end_time: Optional[int] = None) -> Dict:
        """Get order summary statistics using OrderRepository."""
        async def _fn(order_repo):
            return await order_repo.get_orders_summary(
                account_name=account_name,
                start_time=start_time,
                end_time=end_time
            )

        return await self._run_in_repo(
            OrderRepository,
            _fn,
            {
                "total_orders": 0,
                "filled_orders": 0,
                "cancelled_orders": 0,
                "failed_orders": 0,
                "active_orders": 0,
                "fill_rate": 0,
            },
            "Error getting orders summary",
        )

    async def get_trades(self, account_name: Optional[str] = None, connector_name: Optional[str] = None,
                         trading_pair: Optional[str] = None, trade_type: Optional[str] = None,
                         start_time: Optional[int] = None, end_time: Optional[int] = None,
                         limit: int = 100, offset: int = 0) -> List[Dict]:
        """Get trade history using TradeRepository."""
        async def _fn(trade_repo):
            trade_order_pairs = await trade_repo.get_trades_with_orders(
                account_name=account_name,
                connector_name=connector_name,
                trading_pair=trading_pair,
                trade_type=trade_type,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                offset=offset
            )
            return [trade_repo.to_dict(trade, order) for trade, order in trade_order_pairs]

        return await self._run_in_repo(TradeRepository, _fn, [], "Error getting trades")

    async def get_funding_payments(self, account_name: str, connector_name: str = None,
                                   trading_pair: str = None, limit: int = 100) -> List[Dict]:
        """
        Get funding payment history for an account.

        Args:
            account_name: Name of the account
            connector_name: Optional connector name filter
            trading_pair: Optional trading pair filter
            limit: Maximum number of records to return

        Returns:
            List of funding payment dictionaries
        """
        async def _fn(funding_repo):
            funding_payments = await funding_repo.get_funding_payments(
                account_name=account_name,
                connector_name=connector_name,
                trading_pair=trading_pair,
                limit=limit
            )
            return [funding_repo.to_dict(payment) for payment in funding_payments]

        return await self._run_in_repo(FundingRepository, _fn, [], "Error getting funding payments")

    async def get_total_funding_fees(self, account_name: str, connector_name: str,
                                     trading_pair: str) -> Dict:
        """
        Get total funding fees for a specific trading pair.

        Args:
            account_name: Name of the account
            connector_name: Name of the connector
            trading_pair: Trading pair to get fees for

        Returns:
            Dictionary with total funding fees information
        """
        async def _fn(funding_repo):
            return await funding_repo.get_total_funding_fees(
                account_name=account_name,
                connector_name=connector_name,
                trading_pair=trading_pair
            )

        return await self._run_in_repo(
            FundingRepository,
            _fn,
            lambda e: {
                "total_funding_fees": 0,
                "payment_count": 0,
                "fee_currency": None,
                "error": str(e),
            },
            "Error getting total funding fees",
        )
