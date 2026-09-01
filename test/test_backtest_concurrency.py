"""
Tests that two backtests in flight at once do not corrupt each other's results, and that
no more of them run at once than the configured cap allows.

History: the service used to keep a single BacktestingEngineBase so its data provider could
cache downloaded candles across runs, but run_backtesting builds the run on that shared
instance -- time window, controller, resolution -- and then suspends on the multi-second
candle download. A second run entering that window overwrote the first run's state and both
returned silently wrong numbers, with no exception to notice. CORR-060 closed that with a
lock around the run; ARCH-063 then moved each run into its own worker process, which owns
its engine outright, so the two runs have no reachable shared state to corrupt at all. The
lock is gone because there is nothing left for it to guard.

These tests therefore still assert the CORR-060 guarantee -- every run's result carries its
own config and nothing else's -- but against the process mechanism, and they add the cap:
concurrency is now a deliberate, configured number rather than an accident of a lock.

The worker below stands in for the real one so no market data is needed. It runs in a real
spawned child, so the isolation and the cap are exercised for real; it just counts how many
siblings are alive alongside it (one marker file per live worker) instead of simulating.

The repo has no async test setup, so coroutines are driven with asyncio.run().

Run with: pytest test/test_backtest_concurrency.py -v
"""
import asyncio
import os
import pickle
import time
from pathlib import Path

from services.backtesting_service import BacktestingService

HOLD_SECONDS = 0.4


def _echo_worker(config, controllers_path, controllers_module, out_path):
    """Worker target: echo the run's own config back, and report the peak liveness seen."""
    live_dir = Path(config["live_dir"])
    live_dir.mkdir(parents=True, exist_ok=True)
    marker = live_dir / str(os.getpid())
    marker.write_text("")
    peak = 0
    try:
        deadline = time.monotonic() + HOLD_SECONDS
        while time.monotonic() < deadline:
            peak = max(peak, len(list(live_dir.iterdir())))
            time.sleep(0.02)
    finally:
        marker.unlink(missing_ok=True)

    tag = config["config"]["id"]
    result = {
        "executors": [],
        # Same shape as a real payload: {column: {row_index: value}}.
        "processed_data": {
            "controller_id": {0: tag},
            "resolution": {0: config["backtesting_resolution"]},
            "start": {0: config["start_time"]},
        },
        "results": {"sharpe_ratio": 0, "controller_id": tag, "peak_concurrency": peak},
        "position_holds": [],
        "position_held_timeseries": [],
        "pnl_timeseries": [],
    }
    with open(out_path, "wb") as fh:
        pickle.dump({"ok": True, "result": result}, fh)


class StubService(BacktestingService):
    """BacktestingService driving the stub worker, so no market data is needed."""

    def __init__(self, tmp_path, max_concurrent=1):
        super().__init__(
            max_results=10,
            results_path=str(tmp_path / "results"),
            max_concurrent=max_concurrent,
            timeout_seconds=30,
        )
        self._worker_target = _echo_worker


def _config(tmp_path, tag, start, resolution):
    return {
        "config": {"id": tag, "controller_name": "ema_trend_v1"},
        "start_time": start,
        "end_time": start + 3600,
        "backtesting_resolution": resolution,
        "live_dir": str(tmp_path / "live"),
    }


def _tags(result):
    """The identifying values the run carried, read back out of its own result.

    processed_data is a one-row frame rendered as {column: {row_index: value}}; the row key
    is an int in memory and a string once the archive has round-tripped through JSON, so the
    single value is taken positionally.
    """
    features = result["processed_data"]
    return (
        next(iter(features["controller_id"].values())),
        next(iter(features["resolution"].values())),
        next(iter(features["start"].values())),
        result["results"]["controller_id"],
    )


def test_concurrent_executions_each_return_their_own_config(tmp_path):
    async def scenario():
        service = StubService(tmp_path)
        first, second = await asyncio.gather(
            service._execute_backtest(_config(tmp_path, "alpha", 1000, "1m")),
            service._execute_backtest(_config(tmp_path, "beta", 5000, "1h")),
        )
        assert _tags(first) == ("alpha", "1m", 1000, "alpha")
        assert _tags(second) == ("beta", "1h", 5000, "beta")
        # No two runs were ever in flight at once under the default cap of one.
        assert first["results"]["peak_concurrency"] == 1
        assert second["results"]["peak_concurrency"] == 1

    asyncio.run(scenario())


def test_sync_run_and_background_task_do_not_cross_configs(tmp_path):
    """POST /backtesting/run and POST /backtesting/tasks share the one service singleton."""

    async def scenario():
        service = StubService(tmp_path)
        task = service.submit_task(_config(tmp_path, "background", 7000, "3m"))
        sync_result = await service.run_backtest_sync(_config(tmp_path, "foreground", 2000, "5m"))
        await task._asyncio_task

        assert _tags(sync_result) == ("foreground", "5m", 2000, "foreground")
        # The archived task keeps only its metrics resident; the bulk is rehydrated from disk.
        stored = service.get_task_payload(task.task_id)["result"]
        assert _tags(stored) == ("background", "3m", 7000, "background")
        assert sync_result["results"]["peak_concurrency"] == 1

    asyncio.run(scenario())


def test_submissions_beyond_the_cap_queue_instead_of_all_running(tmp_path):
    """Four submissions against a cap of one: only one worker is ever alive."""

    async def scenario():
        service = StubService(tmp_path, max_concurrent=1)
        tasks = [service.submit_task(_config(tmp_path, f"run{i}", 1000 * i, "1m")) for i in range(4)]
        await asyncio.gather(*(t._asyncio_task for t in tasks))
        for task in tasks:
            result = service.get_task_payload(task.task_id)["result"]
            assert result["results"]["peak_concurrency"] == 1
            assert result["results"]["controller_id"] == task.config["config"]["id"]

    asyncio.run(scenario())


def test_the_cap_is_what_serializes_not_luck(tmp_path):
    """With a cap of two, two runs really do overlap -- so the cap above is doing the work."""

    async def scenario():
        service = StubService(tmp_path, max_concurrent=2)
        first, second = await asyncio.gather(
            service._execute_backtest(_config(tmp_path, "alpha", 1000, "1m")),
            service._execute_backtest(_config(tmp_path, "beta", 5000, "1h")),
        )
        assert max(first["results"]["peak_concurrency"], second["results"]["peak_concurrency"]) == 2
        # Overlapping did not let either run pick up the other's config.
        assert _tags(first) == ("alpha", "1m", 1000, "alpha")
        assert _tags(second) == ("beta", "1h", 5000, "beta")

    asyncio.run(scenario())
