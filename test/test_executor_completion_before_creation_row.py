"""An executor's final state is never dropped because its creation row was not there yet.

`create_executor` registers and starts the executor synchronously, then awaits
`_persist_executor_created`. The 1s control loop runs during that await, and an executor
that failed in milliseconds is already `is_closed` by then -- so the completion write can
reach the database BEFORE the creation INSERT commits, or after it failed outright.

`ExecutorRepository.update_executor` is select-then-update and silently does nothing when
the row is missing, while the caller logged success regardless. The observable symptom was
an executor that closed instantly but sat at `status=RUNNING` / `is_active=true` in
`POST /executors/search` and in the performance report forever, with no close_type and no
PnL, until an API restart ran `cleanup_orphaned_executors`.

What is pinned here is that the completion write is self-healing: it inserts the row from
the metadata it already carries, and a creation INSERT that lands afterwards does not
resurrect the executor to RUNNING.
"""

from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from hummingbot.strategy_v2.models.executors import CloseType
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from database.models import ExecutorPerformanceSnapshot, ExecutorRecord
from database.repositories.executor_repository import ExecutorRepository
from services.executor_service import ExecutorService


class _AsyncSessionAdapter:
    """The async surface ExecutorRepository uses, over a real synchronous Session.

    aiosqlite is not installed in this environment, and the repository only ever awaits
    execute/flush/refresh/begin_nested -- so this adapter runs the real SQL against the
    real schema, including the UNIQUE index on executor_id and the SAVEPOINTs the repair
    insert and the terminal performance snapshot rely on. Mocking the session away would
    prove nothing here.
    """

    def __init__(self, session: Session):
        self._session = session

    def add(self, obj):
        self._session.add(obj)

    def add_all(self, objs):
        self._session.add_all(objs)

    async def execute(self, statement):
        return self._session.execute(statement)

    async def flush(self):
        self._session.flush()

    async def refresh(self, obj):
        self._session.refresh(obj)

    async def commit(self):
        self._session.commit()

    async def rollback(self):
        self._session.rollback()

    async def close(self):
        self._session.close()

    def begin_nested(self):
        nested = self._session.begin_nested()

        @asynccontextmanager
        async def _savepoint():
            try:
                yield nested
                if nested.is_active:
                    nested.commit()
            except Exception:
                if nested.is_active:
                    nested.rollback()
                raise

        return _savepoint()


@pytest.fixture
def db():
    """An in-memory database with the tables the completion path writes, plus a session factory."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    ExecutorRecord.__table__.create(engine)
    # Completion also writes the executor's terminal performance snapshot, in this same
    # transaction (FEAT-001).
    ExecutorPerformanceSnapshot.__table__.create(engine)

    @asynccontextmanager
    async def session_context():
        """Mirrors AsyncDatabaseManager.get_session_context: commit, else rollback."""
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

    def rows():
        with Session(engine) as session:
            return session.query(ExecutorRecord).all()

    try:
        yield SimpleNamespace(session_context=session_context, rows=rows)
    finally:
        engine.dispose()


IDENTITY = dict(
    executor_type="position_executor",
    account_name="master",
    connector_name="binance_perpetual",
    trading_pair="BTC-USDT",
    controller_id="main",
)

FINAL_STATE = dict(
    status="TERMINATED",
    close_type="STOP_LOSS",
    net_pnl_quote=Decimal("-12.5"),
    filled_amount_quote=Decimal("400"),
)


# --------------------------------------------------------------------------------------
# The repository: the completion write repairs a missing row instead of no-op'ing
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_updating_a_row_that_is_not_there_yet_still_does_nothing(db):
    """The sharp edge the upsert exists to cover -- documented, not fixed in place."""
    async with db.session_context() as session:
        assert await ExecutorRepository(session).update_executor(
            executor_id="e-1", **FINAL_STATE) is None

    assert db.rows() == []


@pytest.mark.asyncio
async def test_a_completion_with_no_creation_row_inserts_the_closed_record(db):
    async with db.session_context() as session:
        record, repaired = await ExecutorRepository(session).upsert_executor_completion(
            executor_id="e-1", **IDENTITY, **FINAL_STATE)
        assert repaired, "the caller must be told the row had to be repaired"
        assert record is not None

    row, = db.rows()
    assert row.status == "TERMINATED"
    assert row.close_type == "STOP_LOSS"
    assert row.net_pnl_quote == Decimal("-12.5")
    assert row.closed_at is not None
    assert row.trading_pair == "BTC-USDT"


@pytest.mark.asyncio
async def test_a_completion_with_a_creation_row_updates_it_in_place(db):
    """The normal path: one row, updated, and no repair reported."""
    async with db.session_context() as session:
        await ExecutorRepository(session).create_executor(executor_id="e-1", **IDENTITY)

    async with db.session_context() as session:
        _, repaired = await ExecutorRepository(session).upsert_executor_completion(
            executor_id="e-1", **IDENTITY, **FINAL_STATE)
        assert not repaired

    row, = db.rows()
    assert row.status == "TERMINATED"
    assert row.close_type == "STOP_LOSS"


# --------------------------------------------------------------------------------------
# The service: the phantom RUNNING executor, end to end
# --------------------------------------------------------------------------------------

def _service(db, metadata):
    service = ExecutorService.__new__(ExecutorService)
    service.db_manager = MagicMock(get_session_context=db.session_context)
    service._executor_metadata = {"e-1": dict(metadata)}
    service._active_executors = {}
    service._lp_position_addresses = {}
    service._lp_rent_recorded = set()
    service._lp_rent_retry_after = {}
    service._log_capture = MagicMock()
    service._log_capture.get_error_count.return_value = 0
    service._record_executor_swap = AsyncMock()
    return service


def _closed_executor():
    """An executor that failed the moment it started -- insufficient balance, say."""
    executor = MagicMock()
    executor.is_closed = True
    executor.status = SimpleNamespace(name="TERMINATED")
    executor.close_type = CloseType.STOP_LOSS
    executor.executor_info = SimpleNamespace(
        net_pnl_quote=Decimal("-12.5"),
        net_pnl_pct=Decimal("-0.03"),
        cum_fees_quote=Decimal("0.4"),
        filled_amount_quote=Decimal("400"),
    )
    executor.get_custom_info.return_value = {}
    return executor


@pytest.mark.asyncio
async def test_completion_landing_before_the_creation_insert_leaves_a_closed_record(db):
    """The race itself: the control loop completes the executor mid-_persist_created."""
    metadata = {**IDENTITY, "config": {"id": "e-1"}}
    service = _service(db, metadata)
    executor = _closed_executor()
    service._active_executors["e-1"] = executor

    # The control loop wins: it sees is_closed and persists completion first.
    await service._handle_executor_completion("e-1")

    # ...and the creation INSERT that was already in flight lands afterwards. It still
    # holds its metadata, which _handle_executor_completion has since dropped.
    service._executor_metadata["e-1"] = dict(metadata)
    await service._persist_executor_created("e-1", executor)

    row, = db.rows()
    assert row.status == "TERMINATED", "the late creation insert resurrected a phantom RUNNING executor"
    assert row.close_type == "STOP_LOSS"
    assert row.net_pnl_quote == Decimal("-12.5")
    assert row.filled_amount_quote == Decimal("400")
    assert row.final_state is not None


@pytest.mark.asyncio
async def test_a_creation_insert_that_never_landed_is_repaired_by_the_completion(db):
    """The other instance of the same root cause: _persist_executor_created swallows
    every exception, so a DB hiccup used to lose the executor from the record entirely --
    and cleanup_orphaned_executors could not repair what had no row at all."""
    service = _service(db, {**IDENTITY, "config": {"id": "e-1"}})
    service._active_executors["e-1"] = _closed_executor()

    # No _persist_executor_created call at all: it failed and was swallowed.
    await service._handle_executor_completion("e-1")

    row, = db.rows()
    assert row.status == "TERMINATED"
    assert row.close_type == "STOP_LOSS"
    assert row.account_name == "master"
    assert row.executor_type == "position_executor"
