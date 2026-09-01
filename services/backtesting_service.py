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
"""
import asyncio
import gzip
import json
import logging
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

from hummingbot.strategy_v2.backtesting.backtesting_engine_base import BacktestingEngineBase

from config import settings

logger = logging.getLogger(__name__)

# How many reaped ids to remember, so a caller polling a result that was dropped gets a
# definite "it existed and is gone" instead of a bare 404 it cannot distinguish from a typo.
_REAPED_MEMORY = 1000


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
    def __init__(self, max_results: Optional[int] = None, results_path: Optional[str] = None):
        self._tasks: "OrderedDict[str, BacktestTask]" = OrderedDict()
        self._engine = BacktestingEngineBase()
        # One engine is shared so its BacktestingDataProvider keeps caching downloaded candles
        # across runs, but run_backtesting mutates that engine in place -- the time window, the
        # controller, the resolution and the per-run accumulators all live on self -- and then
        # suspends for seconds on the historical candle download. Two runs interleaving there
        # resume against each other's state and return silently wrong numbers, so the engine is
        # lent to one backtest at a time. Everything run_backtesting returns is rebound per run,
        # so a result stays valid once the lock is released.
        self._engine_lock = asyncio.Lock()
        self._max_results = max_results if max_results is not None else settings.backtesting.max_results
        self._results_dir = Path(results_path if results_path is not None else settings.backtesting.results_path)
        self._reaped: "OrderedDict[str, str]" = OrderedDict()
        self._results_dir.mkdir(parents=True, exist_ok=True)
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
        """Cancel a running task or remove a completed one, discarding its archive."""
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
        if isinstance(config["config"], str):
            controller_config = self._engine.get_controller_config_instance_from_yml(
                config_path=config["config"],
                controllers_conf_dir_path=settings.app.controllers_path,
                controllers_module=settings.app.controllers_module
            )
        else:
            controller_config = self._engine.get_controller_config_instance_from_dict(
                config_data=config["config"],
                controllers_module=settings.app.controllers_module
            )
        # The config instances above come from stateless classmethods; only the run itself
        # needs the engine to be exclusively ours.
        async with self._engine_lock:
            backtesting_results = await self._engine.run_backtesting(
                controller_config=controller_config,
                trade_cost=config.get("trade_cost", 0.0006),
                start=int(config["start_time"]),
                end=int(config["end_time"]),
                backtesting_resolution=config.get("backtesting_resolution", "1m"),
            )
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
