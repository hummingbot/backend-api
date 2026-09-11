"""
Tests that a backtest runs off the API's event loop, can actually be stopped, and gives up
when it overruns its wall-clock budget.

The incident these pin down: run_backtesting is awaitable, but past the candle download it
is an uninterrupted CPU loop over every candle. Awaited inline it ran on the API's only
loop thread, which sat at 100% for hours while every endpoint -- /docs included -- stopped
answering, and DELETE /backtesting/tasks/{id} reported CANCELLED while the computation kept
going, because asyncio delivers a cancellation at an await and there was none to deliver it
at. Only docker restart cleared it.

Each test uses a real spawned worker with a cheap target, so what is exercised is the actual
process supervision -- the poll loop, the terminate, the budget -- and not a mock of it.

The repo has no async test setup, so coroutines are driven with asyncio.run().

Run with: pytest test/test_backtest_offloading.py -v
"""
import asyncio
import os
import pickle
import time
from pathlib import Path

import pytest

from services.backtesting_service import BacktestingService, BacktestTaskStatus

RESULT = {
    "executors": [],
    "processed_data": {},
    "results": {"sharpe_ratio": 0, "net_pnl": 0.0},
    "position_holds": [],
    "position_held_timeseries": [],
    "pnl_timeseries": [],
}


def _burn_worker(config, controllers_path, controllers_module, out_path):
    """Worker target: hold a core the way the real simulation does, then return a payload."""
    deadline = time.monotonic() + float(config["burn_seconds"])
    spins = 0
    while time.monotonic() < deadline:
        spins += 1
    with open(out_path, "wb") as fh:
        pickle.dump({"ok": True, "result": {**RESULT, "results": {**RESULT["results"], "spins": spins}}}, fh)


def _runaway_worker(config, controllers_path, controllers_module, out_path):
    """Worker target: announce its pid, then never finish -- the run that needs stopping."""
    Path(config["pid_file"]).write_text(str(os.getpid()))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        pass
    with open(out_path, "wb") as fh:
        pickle.dump({"ok": True, "result": RESULT}, fh)


def _service(tmp_path, timeout_seconds=30):
    service = BacktestingService(
        max_results=10,
        results_path=str(tmp_path / "results"),
        max_concurrent=1,
        timeout_seconds=timeout_seconds,
    )
    return service


def _config(tmp_path, **extra):
    return {
        "config": {"id": "c1", "controller_name": "ema_trend_v1"},
        "start_time": 1000,
        "end_time": 4600,
        "backtesting_resolution": "1m",
        **extra,
    }


def _is_alive(pid):
    """True while the pid exists. The parent reaps its worker, so a dead one is gone, not a zombie."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError) as e:
        return isinstance(e, PermissionError)
    return True


async def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


def test_a_running_backtest_does_not_block_the_event_loop(tmp_path):
    """The loop keeps serving while a backtest burns a core: the work is not on this thread."""

    async def scenario():
        service = _service(tmp_path)
        service._worker_target = _burn_worker

        ticks = 0

        async def heartbeat():
            """Stands in for every other endpoint the API is supposed to keep answering."""
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        started = time.monotonic()
        result = await service._execute_backtest(_config(tmp_path, burn_seconds=1.0))
        elapsed = time.monotonic() - started
        beat.cancel()

        assert result["results"]["spins"] > 0  # the worker really did burn the time
        assert elapsed >= 1.0
        # Inline on the loop the heartbeat would have been frozen for the whole run; the
        # bar is deliberately far below the ~100 ticks a free loop manages in a second.
        assert ticks >= 30, f"event loop only ticked {ticks} times during a {elapsed:.1f}s backtest"

    asyncio.run(scenario())


def test_cancelling_a_task_actually_kills_the_computation(tmp_path):
    """DELETE /backtesting/tasks/{id} stops the work, instead of only relabelling the task."""

    async def scenario():
        service = _service(tmp_path)
        service._worker_target = _runaway_worker
        pid_file = tmp_path / "worker.pid"

        task = service.submit_task(_config(tmp_path, pid_file=str(pid_file)))
        assert await _wait_for(pid_file.exists, timeout=20), "worker never started"
        pid = int(pid_file.read_text())
        assert _is_alive(pid)

        assert service.cancel_task(task.task_id) is True
        with pytest.raises(asyncio.CancelledError):
            await task._asyncio_task

        assert task.status == BacktestTaskStatus.CANCELLED
        assert not _is_alive(pid), "the worker survived the cancellation and is still burning a core"

    asyncio.run(scenario())


def test_a_backtest_past_its_budget_is_terminated_and_reported(tmp_path):
    """No operator intervention: the run is killed and the task explains why it failed."""

    async def scenario():
        # The budget covers the whole run, worker start-up included, so it has to leave room
        # for the child to come up -- a spawned interpreter takes the best part of a second.
        service = _service(tmp_path, timeout_seconds=4.0)
        service._worker_target = _runaway_worker
        pid_file = tmp_path / "worker.pid"

        task = service.submit_task(_config(tmp_path, pid_file=str(pid_file)))
        await task._asyncio_task
        assert pid_file.exists(), "worker never started"
        pid = int(pid_file.read_text())

        assert task.status == BacktestTaskStatus.FAILED
        assert "budget" in (task.error or "")
        assert not _is_alive(pid), "the timed-out worker was left running"

    asyncio.run(scenario())


def test_a_worker_that_dies_is_reported_as_a_failure(tmp_path):
    """A worker killed from outside (OOM, operator) fails the task rather than hanging it."""

    async def scenario():
        service = _service(tmp_path)
        service._worker_target = _runaway_worker
        pid_file = tmp_path / "worker.pid"

        task = service.submit_task(_config(tmp_path, pid_file=str(pid_file)))
        assert await _wait_for(pid_file.exists, timeout=20), "worker never started"
        os.kill(int(pid_file.read_text()), 9)

        await task._asyncio_task
        assert task.status == BacktestTaskStatus.FAILED
        assert "without producing a result" in (task.error or "")

    asyncio.run(scenario())


def test_worker_files_do_not_accumulate(tmp_path):
    """Each run cleans up after itself, and a crash's leftovers go on the next start."""

    async def scenario():
        service = _service(tmp_path)
        service._worker_target = _burn_worker
        await service._execute_backtest(_config(tmp_path, burn_seconds=0.05))
        assert list(service._results_dir.glob(".worker-*.pkl")) == []

        stale = service._results_dir / ".worker-deadbeef.pkl"
        stale.write_bytes(b"leftover")
        _service(tmp_path)
        assert not stale.exists()

    asyncio.run(scenario())
