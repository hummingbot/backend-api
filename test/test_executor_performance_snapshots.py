"""An executor's performance is a series, and one route serves it beside the controllers'.

Before this, an executor's database row was written exactly twice -- zeros at creation,
the real figures at completion -- so a RUNNING executor's row said its PnL was zero for
its entire life, and the live numbers existed only in ExecutorService memory. Two things
followed:

  1. There was no series to chart. A running executor could not draw its own curve, and a
     closed one had a single point.
  2. **An API restart destroyed the accounting of every executor that was live.**
     cleanup_orphaned_executors flipped each RUNNING row to TERMINATED/SYSTEM_CLEANUP and
     touched none of the PnL columns, so the creation-time zeros became the permanent
     record and /executors/performance summed them forever.

What is pinned here: the periodic snapshot, the terminal row that ends a closed
executor's series inside the snapshot table (no join to `executors`, and no exposure to
the two paths that leave that row at zero), the reap adopting the last snapshot, the
grain-parameterized sampler, retention, and the normalized row shape both subjects map
into.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("hummingbot")

from database.models import ControllerPerformanceSnapshot, ExecutorPerformanceSnapshot  # noqa: E402
from database.repositories.executor_performance_repository import ExecutorPerformanceRepository  # noqa: E402
from models.performance import controller_row_to_performance_row, executor_row_to_performance_row  # noqa: E402
from services.executor_service import ExecutorService  # noqa: E402

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------------------
# Fakes: just enough of the async session surface these repositories touch
# --------------------------------------------------------------------------------------

class _RecordingSession:
    """Collects add_all rows; execute() returns whatever the test queued."""

    def __init__(self, results=None):
        self.added = []
        self.flushed = 0
        self._results = list(results or [])
        self.executed = []

    def add_all(self, rows):
        self.added.extend(rows)

    def add(self, row):
        self.added.append(row)

    async def execute(self, statement):
        self.executed.append(statement)
        if self._results:
            return self._results.pop(0)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []), rowcount=0)

    async def flush(self):
        self.flushed += 1


def _scalars(rows):
    return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


def _snapshot(executor_id="e-1", timestamp=NOW, is_terminal=False, close_type=None,
              net_pnl=0, net_pnl_pct=0, fees=0, filled=0, status="RUNNING"):
    return ExecutorPerformanceSnapshot(
        timestamp=timestamp,
        executor_id=executor_id,
        executor_type="position_executor",
        account_name="master_account",
        connector_name="binance_perpetual",
        trading_pair="BTC-USDT",
        controller_id="main",
        status=status,
        close_type=close_type,
        is_terminal=is_terminal,
        net_pnl_quote=Decimal(str(net_pnl)),
        net_pnl_pct=Decimal(str(net_pnl_pct)),
        cum_fees_quote=Decimal(str(fees)),
        filled_amount_quote=Decimal(str(filled)),
    )


def _service(active=None, **kwargs):
    """An ExecutorService with nothing running but the pieces the snapshot path reads."""
    service = ExecutorService.__new__(ExecutorService)
    service.db_manager = kwargs.pop("db_manager", MagicMock())
    service.default_account = "master_account"
    service.performance_snapshot_interval = kwargs.pop("performance_snapshot_interval", 60.0)
    service.performance_retention_days = kwargs.pop("performance_retention_days", 0)
    service._active_executors = active or {}
    service._executor_metadata = kwargs.pop("metadata", {})
    return service


def _live_executor(net_pnl="12.5", net_pnl_pct="0.0125", fees="1.8", filled="4200",
                   status="RUNNING"):
    executor = MagicMock()
    executor.executor_info = SimpleNamespace(
        net_pnl_quote=Decimal(net_pnl),
        net_pnl_pct=Decimal(net_pnl_pct),
        cum_fees_quote=Decimal(fees),
        filled_amount_quote=Decimal(filled),
    )
    executor.status.name = status
    return executor


# --------------------------------------------------------------------------------------
# The table: narrow, typed, and with no second volume column
# --------------------------------------------------------------------------------------

class TestTheTable:
    def test_it_has_no_separate_volume_column(self):
        """filled_amount_quote IS the volume traded, on every executor type including LP.

        The split existed once (volume_traded_quote) and was removed upstream. Any design
        that re-introduces it here breaks like-for-like summing on purpose.
        """
        assert not hasattr(ExecutorPerformanceSnapshot, "volume_traded_quote"), (
            "the snapshot table grew a second volume column; filled_amount_quote is it"
        )

    def test_it_carries_no_custom_info_blob(self):
        """These payloads carry fill_events and grid levels; two code paths strip them.

        A per-minute row is the last place to put them back.
        """
        columns = {c.name for c in ExecutorPerformanceSnapshot.__table__.columns}
        assert "custom_info" not in columns
        assert "performance" not in columns

    def test_one_executors_series_is_indexed(self):
        """WHERE executor_id = ? ORDER BY timestamp DESC is the hot query on both readers."""
        composite = {
            tuple(c.name for c in index.columns)
            for index in ExecutorPerformanceSnapshot.__table__.indexes
        }
        assert ("executor_id", "timestamp") in composite


# --------------------------------------------------------------------------------------
# The dump: one row per live executor, and a database failure never breaks the loop
# --------------------------------------------------------------------------------------

class TestTheDump:
    @pytest.mark.asyncio
    async def test_it_writes_one_row_per_live_executor(self):
        session = _RecordingSession()
        service = _service(
            active={"e-1": _live_executor(), "e-2": _live_executor(net_pnl="-3")},
            metadata={
                "e-1": _metadata("e-1"),
                "e-2": _metadata("e-2"),
            },
        )
        _with_session(service, session)

        await service._dump_executor_performance()

        assert len(session.added) == 2
        assert {row.executor_id for row in session.added} == {"e-1", "e-2"}
        assert all(row.is_terminal is False for row in session.added)
        # One timestamp for the whole batch: the points of one tick line up on a chart.
        assert len({row.timestamp for row in session.added}) == 1

    @pytest.mark.asyncio
    async def test_the_row_carries_the_metrics_the_executor_reports_right_now(self):
        session = _RecordingSession()
        service = _service(active={"e-1": _live_executor()}, metadata={"e-1": _metadata("e-1")})
        _with_session(service, session)

        await service._dump_executor_performance()

        row = session.added[0]
        assert row.net_pnl_quote == Decimal("12.5")
        assert row.cum_fees_quote == Decimal("1.8")
        assert row.filled_amount_quote == Decimal("4200")
        assert row.status == "RUNNING"

    @pytest.mark.asyncio
    async def test_an_executor_whose_info_cannot_be_read_is_skipped_not_zeroed(self):
        """A fabricated zero mid-series is worse than a gap: a reader cannot tell it from
        an executor that genuinely made nothing."""
        broken = MagicMock()
        type(broken).executor_info = property(lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
        session = _RecordingSession()
        service = _service(
            active={"ok": _live_executor(), "broken": broken},
            metadata={"ok": _metadata("ok"), "broken": _metadata("broken")},
        )
        _with_session(service, session)

        await service._dump_executor_performance()

        assert [row.executor_id for row in session.added] == ["ok"]

    @pytest.mark.asyncio
    async def test_a_database_failure_is_logged_and_does_not_propagate(self):
        """This shares the control loop's tick; a dropped snapshot must not stop executors."""
        service = _service(active={"e-1": _live_executor()}, metadata={"e-1": _metadata("e-1")})

        class _Exploding:
            def get_session_context(self):
                raise RuntimeError("database is on fire")

        service.db_manager = _Exploding()

        await service._dump_executor_performance()  # must not raise

    @pytest.mark.asyncio
    async def test_nothing_live_means_no_session_is_opened(self):
        """The tick costs nothing when there is nothing to sample."""
        db_manager = MagicMock()
        service = _service(active={}, db_manager=db_manager)

        await service._dump_executor_performance()

        db_manager.get_session_context.assert_not_called()


# --------------------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------------------

class TestRetention:
    @pytest.mark.asyncio
    async def test_the_default_deletes_nothing(self):
        """PERFORMANCE_RETENTION_DAYS=0 is what every existing deployment does today; an
        upgrade must not start deleting an operator's history."""
        db_manager = MagicMock()
        service = _service(performance_retention_days=0, db_manager=db_manager)

        await service._prune_performance_snapshots()

        db_manager.get_session_context.assert_not_called()

    @pytest.mark.asyncio
    async def test_it_deletes_from_both_snapshot_tables(self):
        """Retention is one policy, not two: the operator says how much performance
        history to keep and both series obey."""
        session = _RecordingSession(results=[
            SimpleNamespace(rowcount=7),
            SimpleNamespace(rowcount=3),
        ])
        repo = ExecutorPerformanceRepository(session)

        executor_rows, controller_rows = await repo.prune_older_than(NOW - timedelta(days=30))

        assert (executor_rows, controller_rows) == (7, 3)
        targeted = [statement.table.name for statement in session.executed]
        assert targeted == [
            ExecutorPerformanceSnapshot.__tablename__,
            ControllerPerformanceSnapshot.__tablename__,
        ]


# --------------------------------------------------------------------------------------
# The sampler: the 5-minute assumption is gone
# --------------------------------------------------------------------------------------

class TestTheSampler:
    def _series(self, count, step_minutes=1):
        """`count` rows, newest first, `step_minutes` apart -- the stored order."""
        return [
            {"timestamp": (NOW - timedelta(minutes=i * step_minutes)).isoformat()}
            for i in range(count)
        ]

    def test_asking_for_the_native_grain_returns_every_row(self):
        history = self._series(10)
        sampled = ExecutorPerformanceRepository._sample_by_interval(history, 1, grain_minutes=1.0)
        assert sampled == history

    def test_five_minutes_over_a_sixty_second_grain_returns_every_fifth_row(self):
        """The controller repository hard-codes `interval_minutes <= 5` and `// 5`. At a
        60-second grain that returned every row untouched."""
        history = self._series(21)
        sampled = ExecutorPerformanceRepository._sample_by_interval(history, 5, grain_minutes=1.0)

        assert [history.index(row) for row in sampled] == [0, 5, 10, 15, 20]

    def test_asking_for_less_than_the_grain_still_returns_the_grain(self):
        """`interval` is a floor, not a guarantee."""
        history = self._series(6, step_minutes=5)
        sampled = ExecutorPerformanceRepository._sample_by_interval(history, 1, grain_minutes=5.0)
        assert sampled == history

    def test_a_zero_interval_cannot_reach_the_divisor(self):
        repo = ExecutorPerformanceRepository(_RecordingSession(), grain_minutes=0)
        assert repo.grain_minutes > 0

    def test_one_minute_is_a_known_interval(self):
        """The executor grain is finer than anything the controller map could express."""
        assert ExecutorPerformanceRepository._interval_to_minutes("1m") == 1


# --------------------------------------------------------------------------------------
# Pagination: the same contract as the controller route
# --------------------------------------------------------------------------------------

class TestPagination:
    @pytest.mark.asyncio
    async def test_a_full_page_reports_more_and_cursors_on_the_last_timestamp(self):
        rows = [_snapshot(timestamp=NOW - timedelta(minutes=i)) for i in range(4)]
        repo = ExecutorPerformanceRepository(_RecordingSession(results=[_scalars(rows)]))

        page, next_cursor, has_more = await repo.get_performance_history(limit=3, interval="1m")

        assert len(page) == 3
        assert has_more is True
        assert next_cursor == page[-1]["timestamp"]

    @pytest.mark.asyncio
    async def test_the_last_page_reports_no_more_and_no_cursor(self):
        rows = [_snapshot(timestamp=NOW - timedelta(minutes=i)) for i in range(2)]
        repo = ExecutorPerformanceRepository(_RecordingSession(results=[_scalars(rows)]))

        page, next_cursor, has_more = await repo.get_performance_history(limit=3, interval="1m")

        assert len(page) == 2
        assert has_more is False
        assert next_cursor is None

    @pytest.mark.asyncio
    async def test_walking_the_cursor_returns_every_row_exactly_once(self):
        """Two pages of a four-row series, driven by next_cursor as a client would."""
        all_rows = [_snapshot(timestamp=NOW - timedelta(minutes=i)) for i in range(4)]

        # Page one over-fetches by one; page two is filtered by the cursor.
        session = _RecordingSession(results=[_scalars(all_rows[:3]), _scalars(all_rows[2:])])
        repo = ExecutorPerformanceRepository(session)

        first, cursor, has_more = await repo.get_performance_history(limit=2, interval="1m")
        assert has_more is True
        second, _, _ = await repo.get_performance_history(limit=2, cursor=cursor, interval="1m")

        seen = [row["timestamp"] for row in first + second]
        assert len(seen) == len(set(seen)), "a row was served on both pages"


# --------------------------------------------------------------------------------------
# The terminal row and the reap
# --------------------------------------------------------------------------------------

class TestTheTerminalRow:
    def test_it_carries_the_close_type_and_the_final_metrics(self):
        service = _service(metadata={"e-1": _metadata("e-1")})
        executor = _live_executor(status="TERMINATED")

        row = service._build_snapshot_row(
            "e-1", executor, is_terminal=True,
            metrics={
                "net_pnl_quote": Decimal("42"),
                "net_pnl_pct": Decimal("0.05"),
                "cum_fees_quote": Decimal("2"),
                "filled_amount_quote": Decimal("900"),
            },
            status="TERMINATED",
            close_type="TAKE_PROFIT",
        )

        assert row["is_terminal"] is True
        assert row["close_type"] == "TAKE_PROFIT"
        assert row["net_pnl_quote"] == Decimal("42")
        assert row["status"] == "TERMINATED"

    def test_a_periodic_row_never_carries_a_close_type(self):
        service = _service(metadata={"e-1": _metadata("e-1")})

        row = service._build_snapshot_row("e-1", _live_executor(), is_terminal=False)

        assert row["is_terminal"] is False
        assert row["close_type"] is None

    def test_the_row_denormalizes_the_identity_so_a_series_needs_no_join(self):
        service = _service(metadata={"e-1": _metadata("e-1")})

        row = service._build_snapshot_row("e-1", _live_executor(), is_terminal=False)

        assert row["executor_type"] == "position_executor"
        assert row["account_name"] == "master_account"
        assert row["connector_name"] == "binance_perpetual"
        assert row["trading_pair"] == "BTC-USDT"
        assert row["controller_id"] == "main"


class TestTheReapAdoptsTheLastSnapshot:
    @pytest.mark.asyncio
    async def test_a_terminated_row_takes_the_last_snapshots_figures(self):
        """The restart bug. Without this the row keeps its creation-time zeros forever."""
        from database.repositories.executor_repository import ExecutorRepository

        orphan = SimpleNamespace(
            executor_id="e-1", status="RUNNING", close_type=None, closed_at=None,
            net_pnl_quote=Decimal("0"), net_pnl_pct=Decimal("0"),
            cum_fees_quote=Decimal("0"), filled_amount_quote=Decimal("0"),
        )
        latest = _snapshot(net_pnl="17.25", net_pnl_pct="0.03", fees="0.9", filled="3100")
        session = _RecordingSession(results=[_scalars([orphan]), _scalars([latest])])

        cleaned = await ExecutorRepository(session).cleanup_orphaned_executors(active_executor_ids=[])

        assert cleaned == 1
        assert orphan.status == "TERMINATED"
        assert orphan.close_type == "SYSTEM_CLEANUP"
        assert orphan.net_pnl_quote == Decimal("17.25")
        assert orphan.cum_fees_quote == Decimal("0.9")
        assert orphan.filled_amount_quote == Decimal("3100")

    @pytest.mark.asyncio
    async def test_an_executor_with_no_snapshot_keeps_the_old_behaviour(self):
        """Created and orphaned inside one interval: there is nothing better to write."""
        from database.repositories.executor_repository import ExecutorRepository

        orphan = SimpleNamespace(
            executor_id="e-1", status="RUNNING", close_type=None, closed_at=None,
            net_pnl_quote=Decimal("0"), net_pnl_pct=Decimal("0"),
            cum_fees_quote=Decimal("0"), filled_amount_quote=Decimal("0"),
        )
        session = _RecordingSession(results=[_scalars([orphan]), _scalars([])])

        cleaned = await ExecutorRepository(session).cleanup_orphaned_executors(active_executor_ids=[])

        assert cleaned == 1
        assert orphan.status == "TERMINATED"
        assert orphan.net_pnl_quote == Decimal("0")

    @pytest.mark.asyncio
    async def test_nothing_orphaned_writes_nothing(self):
        from database.repositories.executor_repository import ExecutorRepository

        session = _RecordingSession(results=[_scalars([])])

        assert await ExecutorRepository(session).cleanup_orphaned_executors(active_executor_ids=[]) == 0
        assert session.flushed == 0


# --------------------------------------------------------------------------------------
# The normalized row: both subjects, one shape
# --------------------------------------------------------------------------------------

class TestTheNormalizedRow:
    def test_a_controller_row_keeps_its_whole_report(self):
        """The normalization is additive, never lossy: everything with no executor
        counterpart stays reachable in the passthrough."""
        row = controller_row_to_performance_row({
            "timestamp": NOW.isoformat(),
            "bot_name": "bot-a",
            "controller_id": "ctrl-1",
            "status": "running",
            "performance": {
                "realized_pnl_quote": 3.0,
                "unrealized_pnl_quote": 1.5,
                "global_pnl_quote": 4.5,
                "global_pnl_pct": 0.02,
                "volume_traded": 1000.0,
                "inventory_imbalance": -0.3,
                "close_type_counts": {"TAKE_PROFIT": 2},
            },
            "custom_info": {"levels": 4},
        })

        assert row.subject == "controller"
        assert row.scope_id == "ctrl-1"
        assert row.bot_name == "bot-a"
        assert (row.realized_pnl_quote, row.unrealized_pnl_quote) == (3.0, 1.5)
        assert row.volume_quote == 1000.0
        assert row.performance["inventory_imbalance"] == -0.3
        assert row.custom_info == {"levels": 4}

    def test_a_controller_reports_unknown_fees_not_zero_fees(self):
        """PerformanceReport genuinely has no fees field. Zero and unknown are different
        -- a consumer charting fees has to be able to tell."""
        row = controller_row_to_performance_row({
            "timestamp": NOW.isoformat(), "controller_id": "c", "status": "running",
            "performance": {}, "custom_info": {},
        })

        assert row.cum_fees_quote is None

    def test_a_live_executors_pnl_is_unrealized(self):
        row = executor_row_to_performance_row(
            ExecutorPerformanceRepository._to_dict(_snapshot(net_pnl="12.5", filled="4200", fees="1.8"))
        )

        assert row.subject == "executor"
        assert row.scope_id == "e-1"
        assert row.unrealized_pnl_quote == 12.5
        assert row.realized_pnl_quote == 0.0
        assert row.global_pnl_quote == 12.5
        assert row.volume_quote == 4200.0
        assert row.cum_fees_quote == 1.8
        assert row.is_terminal is False

    def test_a_settled_executors_pnl_is_realized(self):
        row = executor_row_to_performance_row(
            ExecutorPerformanceRepository._to_dict(
                _snapshot(is_terminal=True, close_type="TAKE_PROFIT", net_pnl="12.5",
                          status="TERMINATED")
            )
        )

        assert row.realized_pnl_quote == 12.5
        assert row.unrealized_pnl_quote == 0.0
        assert row.is_terminal is True
        assert row.close_type == "TAKE_PROFIT"

    def test_a_held_position_is_not_counted_as_realized(self):
        """POSITION_HOLD hands the position on to position_holds; counting it realized
        here would double-count it -- the same exclusion get_performance_report applies."""
        row = executor_row_to_performance_row(
            ExecutorPerformanceRepository._to_dict(
                _snapshot(is_terminal=True, close_type="POSITION_HOLD", net_pnl="12.5")
            )
        )

        assert row.realized_pnl_quote == 0.0
        assert row.unrealized_pnl_quote == 12.5

    def test_an_executor_row_carries_no_heavy_passthrough(self):
        row = executor_row_to_performance_row(
            ExecutorPerformanceRepository._to_dict(_snapshot())
        )

        assert row.performance == {}
        assert row.custom_info == {}

    def test_both_subjects_produce_the_same_field_set(self):
        """The whole point: a client writes seriesFor(scope) once."""
        controller = controller_row_to_performance_row({
            "timestamp": NOW.isoformat(), "controller_id": "c", "status": "running",
            "performance": {}, "custom_info": {},
        })
        executor = executor_row_to_performance_row(
            ExecutorPerformanceRepository._to_dict(_snapshot())
        )

        assert set(controller.model_dump()) == set(executor.model_dump())


# --------------------------------------------------------------------------------------
# The route: one URL, one envelope, and filters that belong to their subject
# --------------------------------------------------------------------------------------

def _client(controller_history=None, executor_history=None,
            controller_latest=None, executor_latest=None):
    """The performance router alone, with both services stubbed."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import routers.performance as performance_router
    from deps import get_bots_orchestrator, get_executor_service

    bots_manager = MagicMock()
    executor_service = MagicMock()

    async def _controllers(**kwargs):
        bots_manager.last_call = kwargs
        return controller_history or ([], None, False)

    async def _executors(**kwargs):
        executor_service.last_call = kwargs
        return executor_history or ([], None, False)

    async def _controllers_latest(**kwargs):
        bots_manager.last_latest_call = kwargs
        return list(controller_latest or [])

    async def _executors_latest(**kwargs):
        executor_service.last_latest_call = kwargs
        return list(executor_latest or [])

    bots_manager.get_controller_performance_history = _controllers
    executor_service.get_executor_performance_history = _executors
    bots_manager.get_latest_controller_performance = _controllers_latest
    executor_service.get_latest_executor_performance = _executors_latest

    app = FastAPI()
    app.include_router(performance_router.router)
    app.dependency_overrides[get_bots_orchestrator] = lambda: bots_manager
    app.dependency_overrides[get_executor_service] = lambda: executor_service
    return TestClient(app), bots_manager, executor_service


class TestTheRoute:
    def test_it_serves_the_executor_series_in_the_normalized_shape(self):
        rows = [ExecutorPerformanceRepository._to_dict(_snapshot(net_pnl="12.5", filled="4200"))]
        client, _, _ = _client(executor_history=(rows, "2026-09-01T11:59:00+00:00", True))

        response = client.get("/performance/history", params={"subject": "executor", "executor_id": "e-1"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success"
        assert body["data"][0]["subject"] == "executor"
        assert body["data"][0]["scope_id"] == "e-1"
        assert body["data"][0]["volume_quote"] == 4200.0
        assert body["pagination"] == {
            "next_cursor": "2026-09-01T11:59:00+00:00",
            "has_more": True,
            "limit": 100,
            "interval": "5m",
        }

    def test_it_serves_the_controller_series_through_the_existing_query_path(self):
        rows = [{
            "timestamp": NOW.isoformat(), "bot_name": "bot-a", "controller_id": "c-1",
            "status": "running",
            "performance": {"global_pnl_quote": 4.5, "volume_traded": 1000.0},
            "custom_info": {"levels": 4},
        }]
        client, bots_manager, _ = _client(controller_history=(rows, None, False))

        response = client.get("/performance/history", params={"subject": "controller", "bot_name": "bot-a"})

        assert response.status_code == 200
        row = response.json()["data"][0]
        assert row["subject"] == "controller"
        assert row["global_pnl_quote"] == 4.5
        assert row["cum_fees_quote"] is None
        # Nothing dropped: the raw payloads are still there.
        assert row["performance"]["volume_traded"] == 1000.0
        assert row["custom_info"] == {"levels": 4}
        assert bots_manager.last_call["bot_name"] == "bot-a"

    def test_an_executor_filter_on_the_controller_subject_is_a_400(self):
        """FastAPI cannot express this, so it is explicit: a filter aimed at the wrong
        population would otherwise be accepted and silently ignored, which reads as an
        empty result rather than a mistake."""
        client, _, _ = _client()

        response = client.get("/performance/history", params={"subject": "controller", "executor_id": "e-1"})

        assert response.status_code == 400
        assert "executor_id" in response.json()["detail"]

    def test_a_bot_name_on_the_executor_subject_is_a_400(self):
        """In-process executors have no bot; it would always match nothing."""
        client, _, _ = _client()

        response = client.get("/performance/history", params={"subject": "executor", "bot_name": "bot-a"})

        assert response.status_code == 400
        assert "bot_name" in response.json()["detail"]

    def test_controller_id_is_legal_on_both_subjects(self):
        """It filters WITHIN a subject. The two namespaces are not the same thing, which
        is exactly why it is never a key to join them on."""
        client, _, _ = _client()

        for subject in ("controller", "executor"):
            assert client.get(
                "/performance/history", params={"subject": subject, "controller_id": "main"}
            ).status_code == 200

    def test_the_subject_is_required(self):
        client, _, _ = _client()
        assert client.get("/performance/history").status_code == 422

    def test_the_limit_is_clamped_at_a_thousand(self):
        client, _, _ = _client()
        assert client.get(
            "/performance/history", params={"subject": "executor", "limit": 1001}
        ).status_code == 422

    def test_one_minute_is_only_meaningful_on_the_executor_subject_but_accepted_on_both(self):
        """`interval` is a floor: asking a 5-minute controller series for 1m returns its
        native grain rather than an error."""
        client, _, _ = _client()
        assert client.get(
            "/performance/history", params={"subject": "controller", "interval": "1m"}
        ).status_code == 200

    def test_a_malformed_timestamp_is_a_400_not_a_500(self):
        client, _, _ = _client()

        response = client.get(
            "/performance/history", params={"subject": "executor", "start_time": "yesterday"}
        )

        assert response.status_code == 400
        assert "Invalid datetime format" in response.json()["detail"]


# --------------------------------------------------------------------------------------
# The existing controller routes are untouched
# --------------------------------------------------------------------------------------

class TestTheControllerPathIsUntouched:
    def test_the_controller_repository_still_assumes_its_own_five_minute_grain(self):
        """It is on a wire-compatible path. The new repository re-implements the sampler
        with a grain parameter rather than generalizing this one."""
        import inspect

        from database.repositories.controller_performance_repository import ControllerPerformanceRepository

        source = inspect.getsource(ControllerPerformanceRepository)
        assert "interval_minutes <= 5" in source
        assert "interval_minutes // 5" in source
        assert "grain_minutes" not in source

    def test_the_unified_route_reuses_the_controller_query_path(self):
        """Sharing one call is what stops the two routes from ever drifting."""
        import inspect

        import routers.performance as performance_router

        source = inspect.getsource(performance_router)
        assert "get_controller_performance_history" in source

    def test_the_existing_controller_routes_still_answer_the_old_shape(self):
        import inspect

        import routers.bot_orchestration as bot_orchestration

        source = inspect.getsource(bot_orchestration)
        assert '@router.get("/controller-performance-latest")' in source
        assert '@router.get("/controller-performance-history")' in source
        # Nothing from the new normalization leaked onto the old path.
        assert "PerformanceRow" not in source


# --------------------------------------------------------------------------------------
# Helpers used above
# --------------------------------------------------------------------------------------

def _metadata(_executor_id):
    return {
        "executor_type": "position_executor",
        "account_name": "master_account",
        "connector_name": "binance_perpetual",
        "trading_pair": "BTC-USDT",
        "controller_id": "main",
        "created_at": NOW,
    }


def _with_session(service, session):
    """Point the service's db_manager at a fixed fake session."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _context():
        yield session

    service.db_manager = SimpleNamespace(get_session_context=_context)


# --------------------------------------------------------------------------------------
# /performance/latest: the current value of every scope, so a consumer can drop
# /bot-orchestration/controller-performance-latest as well as -history
# --------------------------------------------------------------------------------------

def _controller_latest_row(controller_id="c-1", bot_name="bot-a", timestamp=NOW, pnl=4.5):
    return {
        "timestamp": timestamp.isoformat(),
        "bot_name": bot_name,
        "controller_id": controller_id,
        "status": "running",
        "performance": {"global_pnl_quote": pnl, "volume_traded": 1000.0},
        "custom_info": {"levels": 4},
    }


class TestTheLatestQuery:
    @pytest.mark.asyncio
    async def test_it_returns_the_last_row_of_each_executor(self):
        session = _RecordingSession([_scalars([
            _snapshot(executor_id="e-2", net_pnl="7"),
            _snapshot(executor_id="e-1", net_pnl="3"),
        ])])

        rows = await ExecutorPerformanceRepository(session).get_latest()

        assert [r["executor_id"] for r in rows] == ["e-2", "e-1"]
        assert rows[0]["net_pnl_quote"] == 7.0

    @pytest.mark.asyncio
    async def test_it_orders_newest_first_and_takes_a_limit(self):
        """Every executor that ever ran leaves a terminal row, so the unfiltered result
        would grow without bound. Newest-first plus a limit puts the executors that are
        still being snapshotted at the top."""
        session = _RecordingSession([_scalars([])])

        await ExecutorPerformanceRepository(session).get_latest(limit=25)

        sql = str(session.executed[0].compile(compile_kwargs={"literal_binds": True}))
        assert "ORDER BY" in sql and "DESC" in sql
        assert "LIMIT 25" in sql

    @pytest.mark.asyncio
    async def test_no_limit_means_no_limit_clause(self):
        session = _RecordingSession([_scalars([])])

        await ExecutorPerformanceRepository(session).get_latest()

        assert "LIMIT" not in str(session.executed[0].compile(compile_kwargs={"literal_binds": True}))

    @pytest.mark.asyncio
    async def test_a_filter_narrows_the_grouped_subquery(self):
        """The filter decides which executors are aggregated at all, rather than
        aggregating the whole table and discarding most of it afterwards."""
        session = _RecordingSession([_scalars([])])

        await ExecutorPerformanceRepository(session).get_latest(account_name="master_account")

        sql = str(session.executed[0].compile(compile_kwargs={"literal_binds": True}))
        grouped = sql.split("GROUP BY")[0]
        assert "master_account" in grouped, "the filter landed outside the grouped subquery"

    @pytest.mark.asyncio
    async def test_a_closed_executors_last_row_is_its_terminal_row(self):
        """So "the final value" needs no second call and no join to `executors`."""
        session = _RecordingSession([_scalars([
            _snapshot(is_terminal=True, close_type="TAKE_PROFIT", net_pnl="9", status="TERMINATED"),
        ])])

        rows = await ExecutorPerformanceRepository(session).get_latest()

        assert rows[0]["is_terminal"] is True
        assert rows[0]["close_type"] == "TAKE_PROFIT"


class TestTheLatestRoute:
    def test_it_serves_the_executor_scopes_in_the_normalized_shape(self):
        rows = [ExecutorPerformanceRepository._to_dict(_snapshot(net_pnl="12.5", filled="4200"))]
        client, _, _ = _client(executor_latest=rows)

        response = client.get("/performance/latest", params={"subject": "executor"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success"
        assert body["data"][0]["subject"] == "executor"
        assert body["data"][0]["scope_id"] == "e-1"
        assert body["data"][0]["volume_quote"] == 4200.0
        # One row per scope is not a series: there is nothing to cursor through.
        assert "pagination" not in body

    def test_it_serves_the_controller_scopes_through_the_existing_query_path(self):
        client, bots_manager, _ = _client(controller_latest=[_controller_latest_row()])

        response = client.get("/performance/latest", params={"subject": "controller", "bot_name": "bot-a"})

        assert response.status_code == 200
        row = response.json()["data"][0]
        assert row["subject"] == "controller"
        assert row["scope_id"] == "c-1"
        assert row["global_pnl_quote"] == 4.5
        assert row["cum_fees_quote"] is None
        assert row["performance"]["volume_traded"] == 1000.0
        assert bots_manager.last_latest_call["bot_name"] == "bot-a"

    def test_the_controller_scopes_come_back_newest_first(self):
        """get_latest_controller_performance returns join order, so the route sorts --
        otherwise `limit` would truncate an arbitrary set of scopes."""
        older = _controller_latest_row(controller_id="old", timestamp=NOW - timedelta(hours=2))
        newer = _controller_latest_row(controller_id="new", timestamp=NOW)
        client, _, _ = _client(controller_latest=[older, newer])

        response = client.get("/performance/latest", params={"subject": "controller"})

        assert [r["scope_id"] for r in response.json()["data"]] == ["new", "old"]

    def test_a_malformed_controller_timestamp_sorts_last_instead_of_500ing(self):
        """One bad row must not take down a dashboard's whole tile set."""
        bad = _controller_latest_row(controller_id="bad")
        bad["timestamp"] = "not-a-timestamp"
        client, _, _ = _client(controller_latest=[bad, _controller_latest_row(controller_id="good")])

        response = client.get("/performance/latest", params={"subject": "controller"})

        assert response.status_code == 200
        assert [r["scope_id"] for r in response.json()["data"]] == ["good", "bad"]

    def test_controller_id_narrows_the_controller_scopes(self):
        """get_latest_controller_performance only takes bot_name -- it is the method the
        wire-compatible route calls and is deliberately unchanged -- so the route filters."""
        client, _, _ = _client(controller_latest=[
            _controller_latest_row(controller_id="c-1"),
            _controller_latest_row(controller_id="c-2"),
        ])

        response = client.get("/performance/latest",
                              params={"subject": "controller", "controller_id": "c-2"})

        assert [r["scope_id"] for r in response.json()["data"]] == ["c-2"]

    def test_the_limit_caps_the_controller_scopes(self):
        client, _, _ = _client(controller_latest=[
            _controller_latest_row(controller_id=f"c-{i}", timestamp=NOW - timedelta(minutes=i))
            for i in range(5)
        ])

        response = client.get("/performance/latest", params={"subject": "controller", "limit": 2})

        assert [r["scope_id"] for r in response.json()["data"]] == ["c-0", "c-1"]

    def test_the_limit_reaches_the_executor_query(self):
        client, _, executor_service = _client()

        client.get("/performance/latest", params={"subject": "executor", "limit": 7})

        assert executor_service.last_latest_call["limit"] == 7

    def test_an_executor_filter_on_the_controller_subject_is_a_400(self):
        client, _, _ = _client()

        response = client.get("/performance/latest",
                              params={"subject": "controller", "executor_id": "e-1"})

        assert response.status_code == 400
        assert "executor_id" in response.json()["detail"]

    def test_a_bot_name_on_the_executor_subject_is_a_400(self):
        client, _, _ = _client()

        response = client.get("/performance/latest",
                              params={"subject": "executor", "bot_name": "bot-a"})

        assert response.status_code == 400
        assert "bot_name" in response.json()["detail"]

    def test_both_routes_enforce_the_same_filter_rule(self):
        """One helper, so /history and /latest cannot drift into disagreeing about which
        filter belongs to which population."""
        client, _, _ = _client()

        for path in ("/performance/history", "/performance/latest"):
            assert client.get(path, params={"subject": "controller", "trading_pair": "BTC-USDT"}).status_code == 400
            assert client.get(path, params={"subject": "executor", "bot_name": "b"}).status_code == 400

    def test_the_subject_is_required(self):
        client, _, _ = _client()

        assert client.get("/performance/latest").status_code == 422

    def test_the_limit_is_clamped_at_a_thousand(self):
        client, _, _ = _client()

        assert client.get("/performance/latest",
                          params={"subject": "executor", "limit": 1001}).status_code == 422

    def test_both_subjects_produce_the_same_field_set(self):
        """The whole point: latestFor(scope) is written once, not twice."""
        executor_client, _, _ = _client(
            executor_latest=[ExecutorPerformanceRepository._to_dict(_snapshot())])
        controller_client, _, _ = _client(controller_latest=[_controller_latest_row()])

        executor_row = executor_client.get(
            "/performance/latest", params={"subject": "executor"}).json()["data"][0]
        controller_row = controller_client.get(
            "/performance/latest", params={"subject": "controller"}).json()["data"][0]

        assert executor_row.keys() == controller_row.keys()

    def test_it_shares_the_row_shape_with_the_history_route(self):
        """A dashboard's live tiles and its charts read the same fields off one client."""
        row = ExecutorPerformanceRepository._to_dict(_snapshot())
        client, _, _ = _client(executor_latest=[row], executor_history=([row], None, False))

        latest = client.get("/performance/latest", params={"subject": "executor"}).json()["data"][0]
        history = client.get("/performance/history", params={"subject": "executor"}).json()["data"][0]

        assert latest == history

    def test_the_old_latest_route_still_exists(self):
        """Wire compatibility is absolute: this is new surface, and a consumer migrates
        when it chooses to."""
        import inspect

        import routers.bot_orchestration as bot_orchestration

        source = inspect.getsource(bot_orchestration)
        assert '@router.get("/controller-performance-latest")' in source
