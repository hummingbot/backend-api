"""A database outage is reported as a failure, not as a performance report full of zeroes.

`get_performance_report` builds a zeroed report and then fills it in from the database.
The whole database block used to sit inside a bare `except Exception` that logged and moved
on, so an unreachable database produced the untouched zeroed report: total_executors 0,
every PnL 0.0, win_rate 0.0. That is byte-identical to the report of an account that has
simply never run an executor, so no consumer could tell the two apart -- the route answered
200 with the zeroes and the `/ws/executors` performance channel pushed them to dashboards
as real numbers, with no error state and nothing marking them stale, for as long as the
outage lasted.

The failure now propagates: the route turns it into a 500 and the push loop sends an
`error` frame on the channel. The zeroed report is left to mean exactly one thing -- an
empty dataset.

Run with: pytest test/test_performance_report_reports_db_failure.py -v --asyncio-mode=auto
"""

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from database.models import ExecutorRecord
from services.executor_service import ExecutorService
from services.executor_ws_manager import ExecutorSubscription, ExecutorWebSocketManager


class DatabaseUnavailable(Exception):
    """Stands in for whatever the driver raises when the database is unreachable."""


class _AsyncSessionAdapter:
    """The async surface the repository uses, over a real synchronous Session."""

    def __init__(self, session: Session):
        self._session = session

    async def execute(self, statement):
        return self._session.execute(statement)

    async def commit(self):
        self._session.commit()

    async def rollback(self):
        self._session.rollback()

    async def close(self):
        self._session.close()


@pytest.fixture
def db():
    """An in-memory executors table whose session factory can be made to fail."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    ExecutorRecord.__table__.create(engine)

    @asynccontextmanager
    async def session_context():
        session = Session(engine)
        adapter = _AsyncSessionAdapter(session)
        try:
            yield adapter
            await adapter.commit()
        except Exception:
            await adapter.rollback()
            raise
        finally:
            await adapter.close()

    def insert(pnls):
        with Session(engine) as session:
            for i, pnl in enumerate(pnls):
                session.add(ExecutorRecord(
                    executor_id=f"e-{i}",
                    executor_type="position_executor",
                    account_name="master",
                    connector_name="binance_perpetual",
                    trading_pair="BTC-USDT",
                    controller_id="main",
                    status="TERMINATED",
                    close_type="TAKE_PROFIT",
                    net_pnl_quote=Decimal(str(pnl)),
                    net_pnl_pct=Decimal("0"),
                    cum_fees_quote=Decimal("1"),
                    filled_amount_quote=Decimal("100"),
                ))
            session.commit()

    try:
        yield SimpleNamespace(session_context=session_context, insert=insert)
    finally:
        engine.dispose()


def _service(session_context):
    service = ExecutorService.__new__(ExecutorService)
    service.db_manager = MagicMock(get_session_context=session_context)
    service._executor_metadata = {}
    service._active_executors = {}
    service._positions_held = {}
    return service


@asynccontextmanager
async def _unreachable_database():
    """A session context that fails the way a down database does."""
    raise DatabaseUnavailable("could not connect to server")
    yield  # pragma: no cover - unreachable, keeps this an async generator


# --------------------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------------------

class TestTheService:

    async def test_a_database_outage_raises_instead_of_reporting_zeroes(self):
        """The bug: the outage was swallowed and its zeroed report returned as data."""
        service = _service(lambda: _unreachable_database())

        with pytest.raises(DatabaseUnavailable):
            await service.get_performance_report()

    async def test_a_failure_midway_through_the_query_also_raises(self, db):
        """Not only the connect: a query that dies partway through must surface too."""
        service = _service(db.session_context)
        service.db_manager.get_session_context = lambda: _unreachable_database()

        with pytest.raises(DatabaseUnavailable):
            await service.get_performance_report(controller_id="main")

    async def test_an_empty_dataset_still_reports_zeroes(self, db):
        """The other half: zeroes must stay the honest answer for an empty table."""
        report = await _service(db.session_context).get_performance_report()

        assert report["total_executors"] == 0
        assert report["by_status"] == {}
        assert report["pnl_total_quote"] == 0.0
        assert report["global_pnl_quote"] == 0.0
        assert report["win_rate"] == 0.0
        assert report["sharpe_ratio"] is None
        assert report["by_type"] == []

    async def test_a_populated_dataset_is_unaffected(self, db):
        """Removing the except must not change the report the database can answer."""
        db.insert([10.0, -4.0, 6.0])

        report = await _service(db.session_context).get_performance_report()

        assert report["total_executors"] == 3
        assert report["pnl_total_quote"] == pytest.approx(12.0)
        assert report["volume_total_quote"] == pytest.approx(300.0)
        assert report["fees_total_quote"] == pytest.approx(3.0)
        assert report["win_rate"] == pytest.approx(2 / 3)


# --------------------------------------------------------------------------------------
# The route
# --------------------------------------------------------------------------------------

class TestTheRoute:

    def _client(self, report):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import routers.executors as executors_router
        from deps import get_executor_service, get_market_data_service

        executor_service = MagicMock()
        executor_service.get_performance_report = report

        app = FastAPI()
        app.include_router(executors_router.router)
        app.dependency_overrides[get_executor_service] = lambda: executor_service
        app.dependency_overrides[get_market_data_service] = lambda: MagicMock()
        return TestClient(app, raise_server_exceptions=False)

    def test_an_outage_answers_500_rather_than_200_with_zeroes(self):
        client = self._client(AsyncMock(side_effect=DatabaseUnavailable("no server")))

        response = client.get("/executors/performance")

        assert response.status_code == 500

    def test_an_empty_dataset_answers_200_with_zeroes(self):
        empty = {
            "controller_id": None, "total_executors": 0, "by_status": {},
            "pnl_total_quote": 0.0, "unrealized_pnl_quote": 0.0, "global_pnl_quote": 0.0,
            "pnl_pct_avg": 0.0, "fees_total_quote": 0.0, "volume_total_quote": 0.0,
            "win_rate": 0.0, "sharpe_ratio": None, "by_type": [], "active_positions": 0,
        }
        client = self._client(AsyncMock(return_value=empty))

        response = client.get("/executors/performance")

        assert response.status_code == 200
        assert response.json()["total_executors"] == 0


# --------------------------------------------------------------------------------------
# The WebSocket performance channel
# --------------------------------------------------------------------------------------

class RecordingWebSocket:
    """Captures pushed frames and lets a test await the Nth one."""

    def __init__(self):
        self.sent = []
        self._target = None
        self._reached = asyncio.Event()

    async def send_json(self, message):
        self.sent.append(message)
        if self._target is not None and len(self.sent) >= self._target:
            self._reached.set()

    async def wait_for_frames(self, count, timeout=2.0):
        self._target = count
        if len(self.sent) >= count:
            return
        self._reached.clear()
        await asyncio.wait_for(self._reached.wait(), timeout)


def _manager(get_performance_report):
    executor_service = MagicMock()
    executor_service.get_performance_report = get_performance_report
    return ExecutorWebSocketManager(
        executor_service=executor_service,
        market_data_service=MagicMock(),
        bots_orchestrator=MagicMock(),
    )


async def _run(manager, sub, websocket, frames):
    task = asyncio.create_task(
        manager._get_push_fn("performance")("conn-1", websocket, sub)
    )
    try:
        await websocket.wait_for_frames(frames)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _sub():
    return ExecutorSubscription(
        sub_id="sub-1", sub_type="performance", update_interval=0.01, controller_id="main"
    )


class TestTheWebSocketChannel:

    async def test_an_outage_pushes_an_error_frame_naming_the_channel(self):
        manager = _manager(AsyncMock(side_effect=DatabaseUnavailable("no server")))
        websocket = RecordingWebSocket()

        await _run(manager, _sub(), websocket, frames=1)

        frame = websocket.sent[0]
        assert frame["type"] == "error"
        assert frame["channel"] == "performance"
        assert frame["subscription_id"] == "sub-1"
        assert "no server" in frame["message"]

    async def test_a_sustained_outage_sends_one_error_frame_not_one_per_interval(self):
        """The client is told once; the loop keeps retrying quietly behind it."""
        manager = _manager(AsyncMock(side_effect=DatabaseUnavailable("no server")))
        websocket = RecordingWebSocket()
        sub = _sub()

        task = asyncio.create_task(
            manager._get_push_fn("performance")("conn-1", websocket, sub)
        )
        try:
            await websocket.wait_for_frames(1)
            await asyncio.sleep(0.1)  # ~10 more failing polls
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert len(websocket.sent) == 1

    async def test_the_channel_recovers_with_a_data_frame_after_the_error(self):
        """And the recovery frame is sent even though the payload never changed."""
        report = {"total_executors": 0, "pnl_total_quote": 0.0}
        manager = _manager(AsyncMock(side_effect=[
            report, DatabaseUnavailable("no server"), report, report,
        ]))
        websocket = RecordingWebSocket()

        await _run(manager, _sub(), websocket, frames=3)

        types = [f["type"] for f in websocket.sent[:3]]
        assert types == ["performance", "error", "performance"]
        assert websocket.sent[2]["data"] == report

    async def test_an_empty_report_is_still_pushed_as_data(self):
        """A genuinely empty dataset is not an error and must reach the client."""
        empty = {"total_executors": 0, "pnl_total_quote": 0.0, "win_rate": 0.0}
        manager = _manager(AsyncMock(return_value=empty))
        websocket = RecordingWebSocket()

        await _run(manager, _sub(), websocket, frames=1)

        assert websocket.sent[0]["type"] == "performance"
        assert websocket.sent[0]["data"] == empty
