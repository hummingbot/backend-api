"""
Tests that `_sync_orders_to_database` reads the whole in-flight book with a single
batched SELECT instead of one round trip per order (PERF-049).

Run with: pytest test/test_order_sync_batches_queries.py -v
"""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("hummingbot")


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class FakeSession:
    """Records every statement executed and answers `client_order_id IN (...)` from memory."""

    def __init__(self, rows):
        self._rows_by_client_id = {row.client_order_id: row for row in rows}
        self.statements = []
        self.flush_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        requested = []
        for value in statement.compile().params.values():
            if isinstance(value, (list, tuple)):
                requested.extend(value)
            else:
                requested.append(value)
        return FakeResult(
            [self._rows_by_client_id[cid] for cid in requested if cid in self._rows_by_client_id]
        )

    async def flush(self):
        self.flush_count += 1


def _service_with_session(session):
    from services.unified_connector_service import UnifiedConnectorService

    @asynccontextmanager
    async def get_session_context():
        yield session

    service = UnifiedConnectorService.__new__(UnifiedConnectorService)
    service.db_manager = MagicMock()
    service.db_manager.get_session_context = get_session_context
    return service


def _connector_with_orders(states):
    from hummingbot.core.data_type.in_flight_order import OrderState

    connector = MagicMock()
    connector.in_flight_orders = {
        client_order_id: SimpleNamespace(current_state=state or OrderState.OPEN)
        for client_order_id, state in states.items()
    }
    return connector


def _db_row(client_order_id, status):
    return SimpleNamespace(client_order_id=client_order_id, status=status)


class TestOrderSyncBatchesQueries:
    @pytest.mark.asyncio
    async def test_single_select_for_many_in_flight_orders(self):
        """A book of several orders is read with one SELECT, not one per order."""
        from hummingbot.core.data_type.in_flight_order import OrderState

        client_order_ids = [f"OID-{i}" for i in range(8)]
        session = FakeSession([_db_row(cid, "OPEN") for cid in client_order_ids])
        service = _service_with_session(session)
        connector = _connector_with_orders({cid: OrderState.OPEN for cid in client_order_ids})

        await service._sync_orders_to_database(connector, "master", "binance")

        assert len(session.statements) == 1
        # Nothing changed status, so nothing needed flushing either.
        assert session.flush_count == 0

    @pytest.mark.asyncio
    async def test_status_changes_are_persisted_with_one_flush(self):
        from hummingbot.core.data_type.in_flight_order import OrderState

        rows = [_db_row("OID-1", "SUBMITTED"), _db_row("OID-2", "OPEN")]
        session = FakeSession(rows)
        service = _service_with_session(session)
        connector = _connector_with_orders({
            "OID-1": OrderState.OPEN,
            "OID-2": OrderState.PARTIALLY_FILLED,
        })

        await service._sync_orders_to_database(connector, "master", "binance")

        assert rows[0].status == "OPEN"
        assert rows[1].status == "PARTIALLY_FILLED"
        assert len(session.statements) == 1
        assert session.flush_count == 1

    @pytest.mark.asyncio
    async def test_terminal_orders_are_popped_and_still_updated(self):
        from hummingbot.core.data_type.in_flight_order import OrderState

        rows = [_db_row("OID-filled", "OPEN"), _db_row("OID-open", "OPEN")]
        session = FakeSession(rows)
        service = _service_with_session(session)
        connector = _connector_with_orders({
            "OID-filled": OrderState.FILLED,
            "OID-open": OrderState.OPEN,
        })

        await service._sync_orders_to_database(connector, "master", "binance")

        assert rows[0].status == "FILLED"
        assert list(connector.in_flight_orders) == ["OID-open"]

    @pytest.mark.asyncio
    async def test_orders_missing_from_the_database_are_skipped(self):
        from hummingbot.core.data_type.in_flight_order import OrderState

        rows = [_db_row("OID-known", "OPEN")]
        session = FakeSession(rows)
        service = _service_with_session(session)
        connector = _connector_with_orders({
            "OID-known": OrderState.PARTIALLY_FILLED,
            "OID-unknown": OrderState.CANCELED,
        })

        await service._sync_orders_to_database(connector, "master", "binance")

        assert rows[0].status == "PARTIALLY_FILLED"
        # The unknown order has no row to correct but is still terminal, so it is popped.
        assert list(connector.in_flight_orders) == ["OID-known"]


class TestOrderRepositoryBatchLookup:
    @pytest.mark.asyncio
    async def test_no_query_for_an_empty_id_list(self):
        from database.repositories.order_repository import OrderRepository

        session = FakeSession([])
        assert await OrderRepository(session).get_orders_by_client_ids([]) == []
        assert session.statements == []

    @pytest.mark.asyncio
    async def test_large_id_lists_are_chunked(self):
        from database.repositories.order_repository import OrderRepository

        client_order_ids = [f"OID-{i}" for i in range(OrderRepository.CLIENT_ID_CHUNK_SIZE + 1)]
        session = FakeSession([_db_row(cid, "OPEN") for cid in client_order_ids])

        orders = await OrderRepository(session).get_orders_by_client_ids(client_order_ids)

        assert len(session.statements) == 2
        assert len(orders) == len(client_order_ids)
