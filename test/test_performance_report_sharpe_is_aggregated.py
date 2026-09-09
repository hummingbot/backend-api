"""The Sharpe ratio is computed by the database, not by shipping every PnL row to Python.

`get_performance_report` used to run a second, unfiltered `SELECT net_pnl_quote` over every
completed executor and hand the list to `ExecutorService`, which took mean and variance in
Python. The list had exactly one consumer -- the Sharpe ratio -- and no LIMIT, so the report
scanned and transferred the whole executors table. That report is polled by the
`/ws/executors` performance push loop every `update_interval` seconds *per subscriber*, so
the cost of a client watching a chart grew without bound as the table grew.

It is now one more aggregate in the query that already computes sum/avg/count/win-rate over
the same filter: the count, the sum and the sum of squares give the sample standard
deviation directly. What is pinned here is that the row count is back to O(1), and that the
number the API reports is still the number the per-row computation produced.
"""

import inspect
import math
import re
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from database.models import ExecutorRecord
from database.repositories.executor_repository import ExecutorRepository
from services.executor_service import ExecutorService


class _AsyncSessionAdapter:
    """The async surface the repository uses, over a real synchronous Session.

    aiosqlite is not installed here and the report only ever awaits `execute`, so this
    runs the real SQL against the real schema. Mocking the session would prove nothing:
    the whole point is which statements the database is asked to run.
    """

    def __init__(self, session: Session):
        self._session = session
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self._session.execute(statement)

    async def commit(self):
        self._session.commit()

    async def rollback(self):
        self._session.rollback()

    async def close(self):
        self._session.close()


@pytest.fixture
def db():
    """An in-memory executors table, plus a session factory that records its SQL."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    ExecutorRecord.__table__.create(engine)

    sessions = []

    @asynccontextmanager
    async def session_context():
        session = Session(engine)
        adapter = _AsyncSessionAdapter(session)
        sessions.append(adapter)
        try:
            yield adapter
            await adapter.commit()
        except Exception:
            await adapter.rollback()
            raise
        finally:
            await adapter.close()

    def insert(pnls, controller_id="main"):
        with Session(engine) as session:
            for i, pnl in enumerate(pnls):
                session.add(ExecutorRecord(
                    executor_id=f"e-{controller_id}-{i}",
                    executor_type="position_executor",
                    account_name="master",
                    connector_name="binance_perpetual",
                    trading_pair="BTC-USDT",
                    controller_id=controller_id,
                    status="TERMINATED",
                    close_type="TAKE_PROFIT",
                    net_pnl_quote=None if pnl is None else Decimal(str(pnl)),
                    net_pnl_pct=Decimal("0"),
                    cum_fees_quote=Decimal("0"),
                    filled_amount_quote=Decimal("100"),
                ))
            session.commit()

    try:
        yield SimpleNamespace(session_context=session_context, insert=insert,
                              sessions=sessions, engine=engine)
    finally:
        engine.dispose()


def _sharpe_the_old_way(pnls):
    """The exact Python the service used to run over the fetched per-executor rows."""
    values = [float(p or 0) for p in pnls]
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    std = math.sqrt(variance)
    return round(mean / std, 4) if std > 0 else None


def _service(db):
    service = ExecutorService.__new__(ExecutorService)
    service.db_manager = MagicMock(get_session_context=db.session_context)
    service._executor_metadata = {}
    service._active_executors = {}
    service._positions_held = {}
    return service


async def _report(db, controller_id=None):
    return await _service(db).get_performance_report(controller_id=controller_id)


# --------------------------------------------------------------------------------------
# The row count no longer follows the table size
# --------------------------------------------------------------------------------------

def test_the_report_never_selects_a_bare_pnl_column():
    """A `select(ExecutorRecord.net_pnl_quote)` here is a full scan with no LIMIT."""
    source = inspect.getsource(ExecutorRepository.get_performance_report)
    selected = re.findall(r"select\(\s*ExecutorRecord\.(\w+)\b", source)

    assert "net_pnl_quote" not in selected, (
        "the report is fetching one PnL row per completed executor again; "
        "the Sharpe inputs belong in the aggregate query"
    )


@pytest.mark.asyncio
async def test_the_report_costs_the_same_number_of_rows_at_any_table_size(db):
    """Ten executors and a thousand must return the same number of rows to Python."""
    db.insert([1.0, -2.0, 3.0, -0.5, 4.25, -1.75, 0.5, 2.0, -3.0, 1.25])
    async with db.session_context() as session:
        small = await ExecutorRepository(session).get_performance_report()

    db.insert([i * 0.01 - 5 for i in range(1000)], controller_id="bulk")
    async with db.session_context() as session:
        big = await ExecutorRepository(session).get_performance_report()

    # The payload is aggregates only: no per-executor sequence of any kind.
    for report in (small, big):
        assert not any(isinstance(v, (list, tuple)) and v and isinstance(v[0], float)
                       for v in report.values()), f"a per-row list leaked back in: {report}"

    def db_rows(report):
        """Rows crossing the wire: the aggregate row, plus one per group."""
        return 1 + len(report["status_counts"]) + len(report["by_type"])

    assert db_rows(big) == db_rows(small), (
        "the report got more expensive purely because the table got bigger"
    )


# --------------------------------------------------------------------------------------
# The number itself did not change
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("pnls", [
    [1.0, -2.0, 3.0, -0.5, 4.25, -1.75, 0.5, 2.0, -3.0, 1.25],  # mixed
    [10.0, 12.0],                                               # the two-row minimum
    [-4.0, -1.0, -9.0, -2.5],                                   # a losing controller
    [1.0, None, 3.0, -2.0],                                     # NULL PnL counts as zero
    [0.001, 0.002, 0.0015, 0.0011],                             # tiny, tightly clustered
    [1_000_000.5, 1_000_001.5, 1_000_000.0],                    # large mean, small spread
])
async def test_the_sharpe_ratio_matches_the_per_row_computation(db, pnls):
    db.insert(pnls)
    report = await _report(db)

    assert report["sharpe_ratio"] == _sharpe_the_old_way(pnls)
    assert report["sharpe_ratio"] is not None


@pytest.mark.asyncio
async def test_a_single_executor_has_no_sharpe_ratio(db):
    """stddev of one sample is undefined -- the old guard was `len(pnl_values) >= 2`."""
    db.insert([7.5])
    report = await _report(db)

    assert report["sharpe_ratio"] is None
    assert report["total_executors"] == 1


@pytest.mark.asyncio
async def test_no_executors_at_all_has_no_sharpe_ratio(db):
    report = await _report(db)

    assert report["sharpe_ratio"] is None
    assert report["pnl_total_quote"] == 0.0


@pytest.mark.asyncio
async def test_identical_pnls_have_no_sharpe_ratio_rather_than_dividing_by_zero(db):
    """Zero variance: float error around the subtraction must not become a real number."""
    db.insert([2.5, 2.5, 2.5, 2.5])
    report = await _report(db)

    assert report["sharpe_ratio"] is None


@pytest.mark.asyncio
async def test_a_position_hold_is_left_out_of_the_sharpe_ratio_too(db):
    """The dispersion aggregate rides the same filter as the PnL total it belongs to."""
    db.insert([1.0, -2.0, 3.0, -0.5])
    with Session(db.engine) as session:
        session.add(ExecutorRecord(
            executor_id="e-hold", executor_type="position_executor", account_name="master",
            connector_name="binance_perpetual", trading_pair="BTC-USDT", controller_id="main",
            status="TERMINATED", close_type="POSITION_HOLD",
            net_pnl_quote=Decimal("500"), net_pnl_pct=Decimal("0"),
            cum_fees_quote=Decimal("0"), filled_amount_quote=Decimal("100"),
        ))
        session.commit()

    report = await _report(db)

    assert report["sharpe_ratio"] == _sharpe_the_old_way([1.0, -2.0, 3.0, -0.5])


@pytest.mark.asyncio
async def test_the_controller_filter_still_narrows_the_sharpe_ratio(db):
    mine = [1.0, -2.0, 3.0, -0.5]
    db.insert(mine, controller_id="mine")
    db.insert([100.0, -100.0, 250.0], controller_id="theirs")

    report = await _report(db, controller_id="mine")

    assert report["sharpe_ratio"] == _sharpe_the_old_way(mine)
