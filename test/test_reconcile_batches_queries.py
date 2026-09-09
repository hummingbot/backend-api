"""
Tests that `reconcile_active_orders` reads the tracked book with one batched SELECT and
flushes once, instead of a SELECT plus a flush per order (PERF-108).

Run with: pytest test/test_reconcile_batches_queries.py -v
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

    def __init__(self, rows, flush_error=None):
        self._rows_by_client_id = {row.client_order_id: row for row in rows}
        self._flush_error = flush_error
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
        if self._flush_error is not None:
            raise self._flush_error


def _service_with_session(session, connector, account_name="master", connector_name="binance"):
    from services.unified_connector_service import UnifiedConnectorService

    @asynccontextmanager
    async def get_session_context():
        yield session

    service = UnifiedConnectorService.__new__(UnifiedConnectorService)
    service.db_manager = MagicMock()
    service.db_manager.get_session_context = get_session_context
    service._trading_connectors = {account_name: {connector_name: connector}}
    return service


class OrderNotFound(Exception):
    """Stands in for the connector-specific "unknown order" error."""


def _connector_with_orders(exchange_states):
    """Connector whose `_request_order_status` answers with `exchange_states[client_order_id]`.

    A value that is an exception instance is raised instead of returned.
    """
    connector = MagicMock()
    connector.in_flight_orders = {
        client_order_id: SimpleNamespace(client_order_id=client_order_id)
        for client_order_id in exchange_states
    }

    async def _request_order_status(order):
        outcome = exchange_states[order.client_order_id]
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(new_state=outcome)

    connector._request_order_status = _request_order_status
    connector._is_order_not_found_during_status_update_error = (
        lambda exc: isinstance(exc, OrderNotFound)
    )
    return connector


def _db_row(client_order_id, status, error_message=None):
    return SimpleNamespace(
        client_order_id=client_order_id, status=status, error_message=error_message
    )


class TestReconcileBatchesQueries:
    @pytest.mark.asyncio
    async def test_query_count_does_not_grow_with_the_order_count(self):
        """The number of SELECTs depends on the chunk size, never on the order count."""
        from hummingbot.core.data_type.in_flight_order import OrderState

        from database.repositories.order_repository import OrderRepository

        statement_counts = []
        for order_count in (1, 8, 64):
            client_order_ids = [f"OID-{i}" for i in range(order_count)]
            session = FakeSession([_db_row(cid, "OPEN") for cid in client_order_ids])
            connector = _connector_with_orders({cid: OrderState.OPEN for cid in client_order_ids})
            service = _service_with_session(session, connector)

            summary = await service.reconcile_active_orders()

            assert summary["still_open"] == order_count
            statement_counts.append(len(session.statements))

        assert statement_counts == [1, 1, 1]
        # And the batching is what keeps it flat: a book larger than one chunk still costs
        # ceil(N / CLIENT_ID_CHUNK_SIZE) queries, not N.
        assert OrderRepository.CLIENT_ID_CHUNK_SIZE > 1

    @pytest.mark.asyncio
    async def test_large_book_costs_one_query_per_chunk(self):
        from hummingbot.core.data_type.in_flight_order import OrderState

        from database.repositories.order_repository import OrderRepository

        order_count = OrderRepository.CLIENT_ID_CHUNK_SIZE + 1
        client_order_ids = [f"OID-{i}" for i in range(order_count)]
        session = FakeSession([_db_row(cid, "OPEN") for cid in client_order_ids])
        connector = _connector_with_orders({cid: OrderState.OPEN for cid in client_order_ids})
        service = _service_with_session(session, connector)

        await service.reconcile_active_orders()

        assert len(session.statements) == 2

    @pytest.mark.asyncio
    async def test_no_flush_when_no_status_changed(self):
        from hummingbot.core.data_type.in_flight_order import OrderState

        client_order_ids = [f"OID-{i}" for i in range(5)]
        session = FakeSession([_db_row(cid, "OPEN") for cid in client_order_ids])
        connector = _connector_with_orders({cid: OrderState.OPEN for cid in client_order_ids})
        service = _service_with_session(session, connector)

        await service.reconcile_active_orders()

        assert session.flush_count == 0

    @pytest.mark.asyncio
    async def test_status_changes_are_persisted_with_a_single_flush(self):
        from hummingbot.core.data_type.in_flight_order import OrderState

        rows = [
            _db_row("OID-filled", "OPEN"),
            _db_row("OID-open", "SUBMITTED"),
            _db_row("OID-gone", "OPEN"),
        ]
        session = FakeSession(rows)
        connector = _connector_with_orders({
            "OID-filled": OrderState.FILLED,
            "OID-open": OrderState.OPEN,
            "OID-gone": OrderNotFound("unknown order"),
        })
        service = _service_with_session(session, connector)

        summary = await service.reconcile_active_orders()

        assert session.flush_count == 1
        assert rows[0].status == "FILLED"
        assert rows[1].status == "OPEN"
        assert rows[2].status == "CANCELLED"
        assert rows[2].error_message == "Reconciled on startup: order not found on exchange"
        assert summary["reconciled_terminal"] == 2
        assert summary["still_open"] == 1
        # Terminal orders stop being tracked; the open one stays cancelable.
        assert list(connector.in_flight_orders) == ["OID-open"]

    @pytest.mark.asyncio
    async def test_orders_missing_from_the_database_are_skipped(self):
        from hummingbot.core.data_type.in_flight_order import OrderState

        rows = [_db_row("OID-known", "OPEN")]
        session = FakeSession(rows)
        connector = _connector_with_orders({
            "OID-known": OrderState.PARTIALLY_FILLED,
            "OID-unknown": OrderState.FILLED,
        })
        service = _service_with_session(session, connector)

        summary = await service.reconcile_active_orders()

        assert rows[0].status == "PARTIALLY_FILLED"
        # The order with no row is still reconciled against the exchange and untracked.
        assert summary["reconciled_terminal"] == 1
        assert summary["still_open"] == 1
        assert list(connector.in_flight_orders) == ["OID-known"]

    @pytest.mark.asyncio
    async def test_unverifiable_orders_are_left_untouched(self):
        from hummingbot.core.data_type.in_flight_order import OrderState

        rows = [_db_row("OID-flaky", "OPEN"), _db_row("OID-ok", "SUBMITTED")]
        session = FakeSession(rows)
        connector = _connector_with_orders({
            "OID-flaky": TimeoutError("exchange unreachable"),
            "OID-ok": OrderState.OPEN,
        })
        service = _service_with_session(session, connector)

        summary = await service.reconcile_active_orders()

        assert summary["unverified"] == 1
        assert rows[0].status == "OPEN"
        assert rows[1].status == "OPEN"
        assert session.flush_count == 1
        # Nothing terminal was confirmed, so the whole book stays tracked.
        assert list(connector.in_flight_orders) == ["OID-flaky", "OID-ok"]

    @pytest.mark.asyncio
    async def test_a_failed_flush_leaves_tracking_untouched(self):
        from hummingbot.core.data_type.in_flight_order import OrderState

        rows = [_db_row("OID-filled", "OPEN"), _db_row("OID-open", "SUBMITTED")]
        session = FakeSession(rows, flush_error=RuntimeError("db down"))
        connector = _connector_with_orders({
            "OID-filled": OrderState.FILLED,
            "OID-open": OrderState.OPEN,
        })
        service = _service_with_session(session, connector)

        summary = await service.reconcile_active_orders()

        assert summary == {
            "reconciled_terminal": 0,
            "still_open": 0,
            "unverified": 2,
            "skipped_connectors": 0,
        }
        # Nothing was untracked, so the next startup reconciles these orders again.
        assert list(connector.in_flight_orders) == ["OID-filled", "OID-open"]
