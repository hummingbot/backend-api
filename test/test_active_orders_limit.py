"""
Tests that `get_active_orders` bounds results explicitly and that the connector startup
path loads the whole in-flight book with no silent cap (CORR-107).

Run with: pytest test/test_active_orders_limit.py -v
"""
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class FakeSession:
    """Answers a SELECT from memory, honouring whatever LIMIT the query carries."""

    def __init__(self, rows):
        self._rows = rows
        self.limits = []

    async def execute(self, statement):
        limit_clause = getattr(statement, "_limit_clause", None)
        limit = None if limit_clause is None else limit_clause.value
        self.limits.append(limit)
        return FakeResult(self._rows if limit is None else self._rows[:limit])


def _rows(count, status="OPEN"):
    return [SimpleNamespace(client_order_id=f"OID-{i}", status=status) for i in range(count)]


class TestGetActiveOrdersLimit:
    @pytest.mark.asyncio
    async def test_default_limit_still_bounds_the_query(self):
        from database.repositories.order_repository import OrderRepository

        default_limit = OrderRepository.DEFAULT_ACTIVE_ORDERS_LIMIT
        session = FakeSession(_rows(default_limit + 500))

        orders = await OrderRepository(session).get_active_orders()

        assert session.limits == [default_limit]
        assert len(orders) == default_limit

    @pytest.mark.asyncio
    async def test_limit_none_returns_the_whole_book(self):
        from database.repositories.order_repository import OrderRepository

        session = FakeSession(_rows(OrderRepository.DEFAULT_ACTIVE_ORDERS_LIMIT + 500))

        orders = await OrderRepository(session).get_active_orders(limit=None)

        assert session.limits == [None]
        assert len(orders) == OrderRepository.DEFAULT_ACTIVE_ORDERS_LIMIT + 500

    @pytest.mark.asyncio
    async def test_a_truncated_query_warns_with_the_count_and_the_limit(self, caplog):
        from database.repositories.order_repository import OrderRepository

        session = FakeSession(_rows(30))

        with caplog.at_level(logging.WARNING, logger="database.repositories.order_repository"):
            await OrderRepository(session).get_active_orders(limit=10)

        warnings = [record.getMessage() for record in caplog.records
                    if record.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "10 orders" in warnings[0]
        assert "limit of 10" in warnings[0]

    @pytest.mark.asyncio
    async def test_an_unclipped_query_stays_quiet(self, caplog):
        from database.repositories.order_repository import OrderRepository

        session = FakeSession(_rows(3))

        with caplog.at_level(logging.WARNING, logger="database.repositories.order_repository"):
            await OrderRepository(session).get_active_orders(limit=10)

        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


class TestStartupLoadsEveryActiveOrder:
    @pytest.mark.asyncio
    async def test_more_orders_than_the_default_limit_are_all_loaded(self):
        pytest.importorskip("hummingbot")
        from database.repositories.order_repository import OrderRepository
        from services.unified_connector_service import UnifiedConnectorService

        total = OrderRepository.DEFAULT_ACTIVE_ORDERS_LIMIT + 250
        session = FakeSession(_rows(total))

        @asynccontextmanager
        async def get_session_context():
            yield session

        service = UnifiedConnectorService.__new__(UnifiedConnectorService)
        service.db_manager = MagicMock()
        service.db_manager.get_session_context = get_session_context
        service._convert_db_order_to_in_flight = lambda record: SimpleNamespace(
            client_order_id=record.client_order_id
        )

        connector = MagicMock()
        connector.in_flight_orders = {}

        await service._load_existing_orders(connector, "master", "binance")

        assert session.limits == [None]
        assert len(connector.in_flight_orders) == total
