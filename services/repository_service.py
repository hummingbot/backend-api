"""Base for services that own a database session per operation.

Routers used to open their own ``get_session_context`` blocks and construct
repositories inline, which put the persistence rules for a trade inside HTTP
handlers — and made every endpoint re-implement the "record it, but never fail the
caller over a write" policy with its own try/except and its own log message.

This base holds both halves in one place. A subclass names its repository class and
expresses each operation as a function of the repository:

- :meth:`_in_repo` for reads, where errors must reach the caller (a lookup that has
  to answer 404 cannot be handed a default),
- :meth:`_in_repo_best_effort` for the after-the-fact recording of an on-chain
  operation that already happened, where a database failure must be logged and
  swallowed.

Same shape as ``TradingHistoryService._run_in_repo``, which collapsed this scaffold
for the orders/trades/funding reads.
"""
import logging
from typing import Any, Awaitable, Callable, Optional, Type

from database import AsyncDatabaseManager

logger = logging.getLogger(__name__)


class RepositoryService:
    """Owns the session lifecycle for one repository class."""

    #: Repository constructed with the session for every operation.
    repository_class: Optional[Type] = None

    def __init__(self, db_manager: AsyncDatabaseManager):
        """
        Args:
            db_manager: AsyncDatabaseManager for persistence (shared, created once at startup)
        """
        self.db_manager = db_manager

    async def _in_repo(self, fn: Callable[[Any], Awaitable[Any]]) -> Any:
        """Run ``fn`` against a freshly constructed repository inside a session.

        Any conversion to plain dicts must happen inside ``fn``: the session closes
        when this returns, and ORM instances must not outlive it.

        Exceptions propagate — the caller decides what an unavailable database means
        for its endpoint.
        """
        async with self.db_manager.get_session_context() as session:
            return await fn(self.repository_class(session))

    async def _in_repo_best_effort(
        self,
        fn: Callable[[Any], Awaitable[Any]],
        *,
        error_message: str,
        default: Any = None,
    ) -> Any:
        """Same as :meth:`_in_repo`, but a write failure never reaches the caller.

        The one expression of the policy every gateway write endpoint used to carry
        its own copy of: the transaction is already on-chain, so failing the HTTP
        request over the bookkeeping would report a failure that did not happen.

        Args:
            fn: Async callable receiving the repository instance.
            error_message: Prefix used when logging the swallowed exception.
            default: Value returned instead, when ``fn`` raises.
        """
        try:
            return await self._in_repo(fn)
        except Exception as e:
            logger.error(f"{error_message}: {e}", exc_info=True)
            return default
