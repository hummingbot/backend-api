"""
BacktestingService manages background backtesting tasks.

Results are archived to gzipped JSON on completion; only their metrics stay resident.

A finished backtest is dominated by bulk arrays -- processed_data and pnl_timeseries are
~98% of a payload (7.6 MB for a 7-day run at 1m), while the metrics anyone actually reads
back are under 10 KB. Holding whole results in memory therefore rationed the wrong
resource: a flat 50-task cap was ~380 MB for 7-day runs but ~3.2 GB for 60-day ones, and
because reaping can only drop *terminal* tasks it did nothing at all during a submission
burst -- exactly when nothing has finished yet and memory is climbing fastest.

With the bulk on disk a resident task costs a few KB, so retention is a count of results
rather than a memory budget, the full payload is rehydrated on read, and results survive
a restart instead of dying with the process.

The simulation itself runs in a spawned worker process, never on the API's event loop.
`run_backtesting` is awaitable but, once the candles are downloaded, it is an uninterrupted
CPU loop over every candle with no suspension point in it. Awaited inline it pinned the
loop thread at 100% for as long as the run lasted -- every endpoint, `/docs` included,
stopped answering -- and `DELETE /backtesting/tasks/{id}` was powerless, because asyncio
delivers a cancellation at an await and there was none to deliver it at. A worker process
fixes both: the loop only ever waits on a short poll, so cancellation lands within
milliseconds and the child can be signalled dead, which is the only thing that stops a
wedged CPU loop. It also caps how many runs are in flight and abandons one that overruns
its wall-clock budget.
"""
import asyncio
import gzip
import json
import logging
import multiprocessing
import pickle
import time
import traceback
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

from config import settings

logger = logging.getLogger(__name__)

# How many reaped ids to remember, so a caller polling a result that was dropped gets a
# definite "it existed and is gone" instead of a bare 404 it cannot distinguish from a typo.
_REAPED_MEMORY = 1000

# How often the loop checks on the worker. It is the upper bound on how long a cancellation
# or a timeout takes to be acted on, and 20 wakeups a second cost nothing next to a run that
# lasts minutes.
_POLL_INTERVAL = 0.05

# How long a signalled worker is given to die before it is killed outright.
_TERM_GRACE = 1.0

# Prefix for the file a worker leaves its outcome in, inside the results directory.
_WORKER_PREFIX = ".worker-"


class BacktestTaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL = (BacktestTaskStatus.COMPLETED, BacktestTaskStatus.FAILED, BacktestTaskStatus.CANCELLED)


def _json_default(obj: Any) -> Any:
    """Coerce values the stdlib encoder rejects (numpy scalars, timestamps).

    Float dict keys -- processed_data is keyed by epoch seconds -- are coerced to strings
    by json.dump itself, which is exactly what FastAPI's encoder already did on the wire.
    A disk round-trip therefore reproduces the same JSON the API returned before.
    """
    if hasattr(obj, "item"):  # numpy scalar
        return obj.item()
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


class BacktestTimeout(Exception):
    """A run exceeded its wall-clock budget and its worker was terminated."""


# -- worker process --
#
# Everything below runs in the child. The engine is built there and dies with it, so each
# run owns its BacktestingEngineBase outright: the shared-instance race CORR-060 closed with
# a lock cannot occur across processes at all. The cost is that the per-instance
# BacktestingDataProvider.candles_feeds cache no longer spans runs and candles are downloaded
# once per backtest; a shared candle cache is the follow-up, not something to fake here.


def _shape_result(backtesting_results: dict) -> dict:
    """Turn the engine's raw output into the JSON-ready payload the API returns."""
    processed_data = backtesting_results["processed_data"]["features"].fillna(0)
    executors_info = [e.to_dict() for e in backtesting_results["executors"]]
    results = backtesting_results["results"]
    results["sharpe_ratio"] = results["sharpe_ratio"] if results["sharpe_ratio"] is not None else 0

    # Serialize position holds
    position_holds = []
    for ph in backtesting_results.get("position_holds", []):
        position_holds.append({
            "connector_name": ph.connector_name,
            "trading_pair": ph.trading_pair,
            "buy_amount_base": float(ph.buy_amount_base),
            "buy_amount_quote": float(ph.buy_amount_quote),
            "sell_amount_base": float(ph.sell_amount_base),
            "sell_amount_quote": float(ph.sell_amount_quote),
            "net_amount_base": float(ph.net_amount_base),
            "cum_fees_quote": float(ph.cum_fees_quote),
            "volume_traded_quote": float(ph.volume_traded_quote),
            "is_closed": ph.is_closed,
            "n_executors": len(ph.source_executor_ids),
        })

    return {
        "executors": executors_info,
        "processed_data": processed_data.to_dict(),
        "results": results,
        "position_holds": position_holds,
        "position_held_timeseries": backtesting_results.get("position_held_timeseries", []),
        "pnl_timeseries": backtesting_results.get("pnl_timeseries", []),
    }


def _run_backtest_blocking(config: dict, controllers_path: str, controllers_module: str) -> dict:
    """Build the controller config, run the simulation to completion, shape the payload."""
    # Imported here rather than at module scope so only a process that actually backtests
    # pays for pulling in hummingbot.
    from hummingbot.strategy_v2.backtesting.backtesting_engine_base import BacktestingEngineBase

    engine = BacktestingEngineBase()
    if isinstance(config["config"], str):
        controller_config = engine.get_controller_config_instance_from_yml(
            config_path=config["config"],
            controllers_conf_dir_path=controllers_path,
            controllers_module=controllers_module
        )
    else:
        controller_config = engine.get_controller_config_instance_from_dict(
            config_data=config["config"],
            controllers_module=controllers_module
        )
    backtesting_results = asyncio.run(engine.run_backtesting(
        controller_config=controller_config,
        trade_cost=config.get("trade_cost", 0.0006),
        start=int(config["start_time"]),
        end=int(config["end_time"]),
        backtesting_resolution=config.get("backtesting_resolution", "1m"),
    ))
    return _shape_result(backtesting_results)


def _worker_main(config: dict, controllers_path: str, controllers_module: str, out_path: str) -> None:
    """Worker entry point: run one backtest and leave a pickled envelope at out_path.

    The outcome goes through a file rather than a pipe because a result is megabytes and a
    pipe holds only tens of kilobytes: a child blocking in send() against a parent that is
    waiting for it to exit is a deadlock, and the parent here waits on the process, not on
    a read. The file is written whole before the child exits, so its absence afterwards
    means the worker died rather than finished.
    """
    try:
        result = _run_backtest_blocking(config, controllers_path, controllers_module)
        blob = pickle.dumps({"ok": True, "result": result}, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as e:
        blob = pickle.dumps({"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()})
    with open(out_path, "wb") as fh:
        fh.write(blob)


def _terminate(proc: multiprocessing.Process) -> None:
    """Stop a worker and reap it. A CPU-bound child is signalled, not asked."""
    if proc.pid is None:
        return
    try:
        if proc.is_alive():
            proc.terminate()
            proc.join(_TERM_GRACE)
        if proc.is_alive():
            proc.kill()
            proc.join(_TERM_GRACE)
        proc.join(0)
        proc.close()
    except (OSError, ValueError) as e:  # already reaped, or closed under us
        logger.debug(f"Backtest worker cleanup: {e}")


class BacktestTask:
    def __init__(self, task_id: str, config: dict):
        self.task_id = task_id
        self.config = config
        self.status = BacktestTaskStatus.PENDING
        self.created_at = datetime.now(timezone.utc)
        self.started_at: Optional[datetime] = None
        self.completed_at: Optional[datetime] = None
        # Once archived this holds metrics only; the full payload lives on disk.
        self.result: Optional[Dict[str, Any]] = None
        self.archived = False
        self.error: Optional[str] = None
        self._asyncio_task: Optional[asyncio.Task] = None

    def to_dict(self, include_result: bool = True) -> dict:
        data = {
            "task_id": self.task_id,
            "status": self.status.value,
            "config": self.config,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error": self.error,
        }
        if include_result and self.result is not None:
            data["result"] = self.result
        return data

    def metadata(self) -> dict:
        """Light record for the on-disk index -- everything but the bulk arrays."""
        return {**self.to_dict(include_result=False), "result": self.result, "archived": self.archived}


class BacktestingService:
    def __init__(
        self,
        max_results: Optional[int] = None,
        results_path: Optional[str] = None,
        max_concurrent: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
    ):
        self._tasks: "OrderedDict[str, BacktestTask]" = OrderedDict()
        # Each run gets its own engine, in its own process. An engine is mutated in place by
        # run_backtesting -- the time window, the controller, the resolution and the per-run
        # accumulators all live on self -- and then suspends for seconds on the candle
        # download, so sharing one instance between overlapping runs returned silently wrong
        # numbers (CORR-060). A process boundary makes that unshareable by construction.
        self._worker_target = _worker_main
        # Runs beyond the cap queue on this rather than piling a core each onto the box.
        max_concurrent = max_concurrent if max_concurrent is not None else settings.backtesting.max_concurrent
        self._max_concurrent = max(1, int(max_concurrent))
        self._slots = asyncio.Semaphore(self._max_concurrent)
        self._timeout = (
            timeout_seconds if timeout_seconds is not None else settings.backtesting.timeout_seconds
        )
        self._max_results = max_results if max_results is not None else settings.backtesting.max_results
        self._results_dir = Path(results_path if results_path is not None else settings.backtesting.results_path)
        self._reaped: "OrderedDict[str, str]" = OrderedDict()
        self._results_dir.mkdir(parents=True, exist_ok=True)
        self._clear_worker_files()
        self._restore()
        # Honour a limit that was lowered since the last run.
        self._reap()

    @property
    def tasks(self) -> Dict[str, BacktestTask]:
        return self._tasks

    # -- submission and lifecycle --

    def submit_task(self, config: dict) -> BacktestTask:
        """Submit a new backtesting task to run in the background."""
        task_id = str(uuid.uuid4())[:8]
        task = BacktestTask(task_id=task_id, config=config)
        self._tasks[task_id] = task
        task._asyncio_task = asyncio.create_task(self._run_task(task))
        self._reap()
        logger.info(f"Backtesting task {task_id} submitted")
        return task

    def get_task(self, task_id: str) -> Optional[BacktestTask]:
        return self._tasks.get(task_id)

    def get_task_payload(self, task_id: str) -> Optional[dict]:
        """Task dict with the complete result, rehydrated from disk when archived."""
        task = self._tasks.get(task_id)
        if task is None:
            return None
        data = task.to_dict(include_result=False)
        result = self._read_archive(task_id) if task.archived else None
        if result is None:
            result = task.result  # never archived, or the archive went missing
        if result is not None:
            data["result"] = result
        return data

    def was_reaped(self, task_id: str) -> bool:
        """True if the task completed and was later dropped to honour the retention limit."""
        return task_id in self._reaped

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a running task or remove a completed one, discarding its archive.

        Cancelling the coroutine is enough to stop the computation now that the coroutine
        actually suspends: the cancellation is delivered at the next poll, at most
        _POLL_INTERVAL away, and _run_in_worker's finally signals the worker dead. Before the
        run moved off the loop this call was a lie -- it reported CANCELLED while the
        simulation kept the process pinned, because there was no await to deliver it at.
        """
        task = self._tasks.get(task_id)
        if task is None:
            return False
        if task._asyncio_task and not task._asyncio_task.done():
            task._asyncio_task.cancel()
            task.status = BacktestTaskStatus.CANCELLED
            task.completed_at = datetime.now(timezone.utc)
        self._drop(task_id, reaped=False)
        return True

    def list_tasks(self) -> list:
        """List all tasks (without results for brevity)."""
        return [t.to_dict(include_result=False) for t in self._tasks.values()]

    async def run_backtest_sync(self, config: dict) -> dict:
        """Run a backtest synchronously (returns full result directly, stores nothing)."""
        return await self._execute_backtest(config)

    async def _run_task(self, task: BacktestTask):
        """Background coroutine that executes the backtest."""
        task.status = BacktestTaskStatus.RUNNING
        task.started_at = datetime.now(timezone.utc)
        try:
            result = await self._execute_backtest(task.config)
            task.status = BacktestTaskStatus.COMPLETED
            self._archive(task, result)
            logger.info(f"Backtesting task {task.task_id} completed")
        except asyncio.CancelledError:
            task.status = BacktestTaskStatus.CANCELLED
            logger.info(f"Backtesting task {task.task_id} cancelled")
            raise
        except Exception as e:
            task.status = BacktestTaskStatus.FAILED
            task.error = str(e)
            logger.error(f"Backtesting task {task.task_id} failed: {e}", exc_info=True)
        finally:
            task.completed_at = datetime.now(timezone.utc)
            self._persist_index()
            # Reap here too: a burst that is submitted all at once and only then starts
            # finishing would otherwise never be trimmed, since nothing is terminal at
            # submit time and no further submissions arrive to trigger it.
            self._reap()

    async def _execute_backtest(self, config: dict) -> dict:
        """Core backtest execution logic shared by sync and async modes."""
        async with self._slots:
            return await self._run_in_worker(config)

    async def _run_in_worker(self, config: dict) -> dict:
        """Run one backtest in a child process, supervised from the loop.

        The loop never touches the simulation: it waits in short sleeps, which is what makes
        the API stay responsive, makes a cancellation land within a poll interval, and lets
        the wall-clock budget be enforced. Whatever ends the wait -- success, timeout,
        cancellation, or the caller hanging up -- the worker is signalled dead on the way out,
        so no orphan is left burning a core.
        """
        ctx = multiprocessing.get_context("spawn")
        out_path = self._results_dir / f"{_WORKER_PREFIX}{uuid.uuid4().hex}.pkl"
        proc = ctx.Process(
            target=self._worker_target,
            args=(config, settings.app.controllers_path, settings.app.controllers_module, str(out_path)),
            daemon=True,
        )
        proc.start()
        deadline = time.monotonic() + self._timeout
        try:
            while proc.is_alive():
                if time.monotonic() >= deadline:
                    raise BacktestTimeout(
                        f"Backtest exceeded its wall-clock budget of {self._timeout:g}s and was terminated"
                    )
                await asyncio.sleep(_POLL_INTERVAL)
            return self._read_outcome(out_path, proc.exitcode)
        finally:
            _terminate(proc)
            try:
                out_path.unlink(missing_ok=True)
            except OSError as e:
                logger.warning(f"Could not remove backtest worker file {out_path}: {e}")

    @staticmethod
    def _read_outcome(out_path: Path, exitcode: Optional[int]) -> dict:
        """Unwrap what the worker left behind, or explain why there is nothing to unwrap."""
        if not out_path.exists():
            raise RuntimeError(
                f"Backtest worker exited with code {exitcode} without producing a result"
            )
        try:
            with open(out_path, "rb") as fh:
                envelope = pickle.load(fh)
        except (OSError, pickle.UnpicklingError, EOFError, AttributeError) as e:
            raise RuntimeError(f"Could not read the backtest worker result: {e}")
        if not envelope.get("ok"):
            logger.error(f"Backtest worker failed:\n{envelope.get('traceback', '')}")
            raise RuntimeError(envelope.get("error", "backtest failed in the worker process"))
        return envelope["result"]

    # -- archive --

    def _archive_path(self, task_id: str) -> Path:
        return self._results_dir / f"{task_id}.json.gz"

    @property
    def _index_path(self) -> Path:
        return self._results_dir / "_index.json"

    def _archive(self, task: BacktestTask, result: dict) -> None:
        """Write the full payload to disk and keep only its metrics resident."""
        try:
            with gzip.open(self._archive_path(task.task_id), "wt", encoding="utf-8") as fh:
                json.dump(result, fh, default=_json_default)
        except (OSError, TypeError, ValueError) as e:
            # Losing the result would be worse than holding it: keep it in memory and let
            # the retention limit reclaim it later.
            logger.error(f"Could not archive backtest {task.task_id}, keeping it in memory: {e}")
            task.result = result
            return
        task.result = {"results": result.get("results", {})}
        task.archived = True

    def _read_archive(self, task_id: str) -> Optional[dict]:
        path = self._archive_path(task_id)
        if not path.exists():
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError) as e:
            logger.error(f"Could not read archived backtest {task_id}: {e}")
            return None

    def _persist_index(self) -> None:
        try:
            index = {tid: t.metadata() for tid, t in self._tasks.items() if t.status in _TERMINAL}
            with open(self._index_path, "w", encoding="utf-8") as fh:
                json.dump(index, fh, default=_json_default)
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"Could not persist backtest index: {e}")

    def _clear_worker_files(self) -> None:
        """Drop worker outcome files a previous process died before collecting."""
        for path in self._results_dir.glob(f"{_WORKER_PREFIX}*.pkl"):
            try:
                path.unlink()
            except OSError as e:
                logger.warning(f"Could not remove stale backtest worker file {path}: {e}")

    def _restore(self) -> None:
        """Rebuild finished tasks from the index so results outlive a restart."""
        if not self._index_path.exists():
            return
        try:
            with open(self._index_path, encoding="utf-8") as fh:
                index = json.load(fh)
        except (OSError, ValueError) as e:
            logger.error(f"Could not read backtest index, starting empty: {e}")
            return

        for task_id, meta in index.items():
            try:
                task = BacktestTask(task_id=task_id, config=meta.get("config") or {})
                task.status = BacktestTaskStatus(meta.get("status", "completed"))
                task.created_at = datetime.fromisoformat(meta["created_at"])
                for field in ("started_at", "completed_at"):
                    if meta.get(field):
                        setattr(task, field, datetime.fromisoformat(meta[field]))
                task.error = meta.get("error")
                task.result = meta.get("result")
                task.archived = bool(meta.get("archived")) and self._archive_path(task_id).exists()
                self._tasks[task_id] = task
            except (KeyError, TypeError, ValueError) as e:
                logger.warning(f"Skipping unreadable backtest index entry '{task_id}': {e}")

        if self._tasks:
            logger.info(f"Restored {len(self._tasks)} backtest result(s) from {self._results_dir}")

    # -- retention --

    def _reap(self) -> None:
        """Drop the oldest finished results beyond the retention limit.

        Running tasks are never dropped -- there is nothing to reclaim from one anyway,
        since a result exists only once it completes. A large burst can therefore briefly
        exceed the limit, which is safe now that a resident task is a few KB.
        """
        if len(self._tasks) <= self._max_results:
            return
        terminal = [(tid, t) for tid, t in self._tasks.items() if t.status in _TERMINAL]
        terminal.sort(key=lambda kv: kv[1].completed_at or kv[1].created_at)
        while len(self._tasks) > self._max_results and terminal:
            task_id, _ = terminal.pop(0)
            self._drop(task_id, reaped=True)

    def _drop(self, task_id: str, reaped: bool) -> None:
        self._tasks.pop(task_id, None)
        path = self._archive_path(task_id)
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.error(f"Could not delete archived backtest {task_id}: {e}")
        if reaped:
            self._reaped[task_id] = datetime.now(timezone.utc).isoformat()
            while len(self._reaped) > _REAPED_MEMORY:
                self._reaped.popitem(last=False)
        self._persist_index()
