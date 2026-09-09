"""`get_executor_stats` asks the database twice, not seven times.

The method used to issue four unfiltered scalar queries -- two COUNTs and two SUMs, none
of them narrowing anything -- followed by three separate GROUP BY queries over the same
table. Seven round-trips for a payload that is entirely aggregates: every one of them a
full pass over the executors table, and the four scalars share a single filter (none).

It is now one aggregate row plus one grouped statement over
(executor_type, status, connector_name), pivoted back into the three breakdowns in Python
-- the same shape `get_performance_report` was reduced to. What is pinned here is the
round-trip count, and that the dict handed back is the one the seven queries produced.
"""

from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from database.models import ExecutorRecord
from database.repositories.executor_repository import ExecutorRepository


class _AsyncSessionAdapter:
    """The async surface the repository uses, over a real synchronous Session.

    aiosqlite is not installed here and the method only ever awaits `execute`, so this
    runs the real SQL against the real schema, and counts the statements it is asked for.
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
        finally:
            await adapter.close()

    def insert(rows):
        """rows: dicts overriding the defaults below, one executor each."""
        with Session(engine) as session:
            for i, row in enumerate(rows):
                fields = {
                    "executor_type": "position_executor",
                    "connector_name": "binance_perpetual",
                    "status": "TERMINATED",
                    "net_pnl_quote": Decimal("1"),
                    "filled_amount_quote": Decimal("100"),
                    **row,
                }
                session.add(ExecutorRecord(
                    executor_id=f"e-{i}",
                    account_name="master",
                    trading_pair="BTC-USDT",
                    controller_id="main",
                    close_type="TAKE_PROFIT",
                    net_pnl_pct=Decimal("0"),
                    cum_fees_quote=Decimal("0"),
                    **fields,
                ))
            session.commit()

    try:
        yield SimpleNamespace(session_context=session_context, insert=insert, engine=engine)
    finally:
        engine.dispose()


def _stats_the_old_way(engine):
    """The seven queries the method used to run, verbatim, as the expected result."""
    with Session(engine) as session:
        total = session.execute(select(func.count(ExecutorRecord.id))).scalar() or 0
        active = session.execute(
            select(func.count(ExecutorRecord.id)).where(ExecutorRecord.status == "RUNNING")
        ).scalar() or 0
        pnl = session.execute(select(func.sum(ExecutorRecord.net_pnl_quote))).scalar() or Decimal("0")
        volume = session.execute(
            select(func.sum(ExecutorRecord.filled_amount_quote))
        ).scalar() or Decimal("0")

        def grouped(column):
            rows = session.execute(
                select(column, func.count(ExecutorRecord.id).label("count")).group_by(column)
            )
            return {row[0]: row.count for row in rows}

        return {
            "total_executors": total,
            "active_executors": active,
            "total_pnl_quote": float(pnl),
            "total_volume_quote": float(volume),
            "type_counts": grouped(ExecutorRecord.executor_type),
            "status_counts": grouped(ExecutorRecord.status),
            "connector_counts": grouped(ExecutorRecord.connector_name),
        }


async def _stats(db):
    """Runs the method and reports both its answer and how many statements it cost."""
    async with db.session_context() as session:
        stats = await ExecutorRepository(session).get_executor_stats()
        return stats, len(session.statements)


_A_MIXED_TABLE = [
    {"status": "RUNNING", "executor_type": "position_executor", "connector_name": "binance_perpetual"},
    {"status": "RUNNING", "executor_type": "dca_executor", "connector_name": "kucoin"},
    {"status": "TERMINATED", "executor_type": "position_executor", "connector_name": "kucoin",
     "net_pnl_quote": Decimal("-3.5"), "filled_amount_quote": Decimal("250.25")},
    {"status": "FAILED", "executor_type": "arbitrage_executor", "connector_name": "binance",
     "net_pnl_quote": None},
    {"status": "TERMINATED", "executor_type": "dca_executor", "connector_name": "binance",
     "net_pnl_quote": Decimal("12.75"), "filled_amount_quote": Decimal("0")},
]


# --------------------------------------------------------------------------------------
# The round-trip count
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_stats_cost_two_queries(db):
    db.insert(_A_MIXED_TABLE)

    _, queries = await _stats(db)

    assert queries == 2, (
        f"get_executor_stats issued {queries} statements; it is one aggregate row plus "
        "one grouped statement, and every extra one is another pass over the table"
    )


@pytest.mark.asyncio
async def test_an_empty_table_costs_the_same_two_queries(db):
    _, queries = await _stats(db)

    assert queries == 2


@pytest.mark.asyncio
async def test_the_query_count_does_not_follow_the_number_of_groups(db):
    """Ten distinct connectors, still two statements -- the breakdowns are pivoted here."""
    db.insert([{"connector_name": f"venue_{i}", "executor_type": f"type_{i % 3}"}
               for i in range(10)])

    stats, queries = await _stats(db)

    assert queries == 2
    assert len(stats["connector_counts"]) == 10


# --------------------------------------------------------------------------------------
# The dict itself did not change
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [
    pytest.param([], id="empty_table"),
    pytest.param(_A_MIXED_TABLE, id="mixed_types_statuses_and_connectors"),
    pytest.param([{"status": "RUNNING"}] * 3, id="all_running"),
    pytest.param([{"net_pnl_quote": None, "filled_amount_quote": None}], id="null_amounts"),
    pytest.param([{"net_pnl_quote": Decimal("-1.5")}, {"net_pnl_quote": Decimal("1.5")}],
                 id="pnl_cancels_to_zero"),
])
async def test_the_result_matches_the_seven_query_version(db, rows):
    db.insert(rows)

    stats, _ = await _stats(db)

    assert stats == _stats_the_old_way(db.engine)


@pytest.mark.asyncio
async def test_the_keys_are_still_the_ones_the_endpoint_returns(db):
    db.insert(_A_MIXED_TABLE)

    stats, _ = await _stats(db)

    assert set(stats) == {
        "total_executors", "active_executors", "total_pnl_quote", "total_volume_quote",
        "type_counts", "status_counts", "connector_counts",
    }
    assert stats["total_executors"] == 5
    assert stats["active_executors"] == 2
    assert isinstance(stats["total_pnl_quote"], float)
    assert isinstance(stats["total_volume_quote"], float)


@pytest.mark.asyncio
async def test_a_status_with_no_executors_is_absent_rather_than_zero(db):
    """The grouped pivot must not invent keys the GROUP BY never produced."""
    db.insert([{"status": "RUNNING"}, {"status": "RUNNING"}])

    stats, _ = await _stats(db)

    assert stats["status_counts"] == {"RUNNING": 2}
    assert stats["active_executors"] == 2
