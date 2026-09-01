"""
Tests that two backtests in flight at once do not corrupt each other's results.

The service keeps a single BacktestingEngineBase so its data provider can cache downloaded
candles across runs, but run_backtesting builds the run on that shared instance -- time
window, controller, resolution -- and then suspends on the multi-second candle download.
Before the engine lock, a second run entering that window overwrote the first run's state
and both returned silently wrong numbers, with no exception to notice.

The stub engine below reproduces exactly that shape: it stores the run on itself, awaits,
and only then reads its own state back to build the result. Run it against an unlocked
service and the assertions here fail.

The repo has no async test setup, so coroutines are driven with asyncio.run().

Run with: pytest test/test_backtest_concurrency.py -v
"""
import asyncio

import pandas as pd

from services.backtesting_service import BacktestingService


class SharedStateEngine:
    """Stands in for BacktestingEngineBase, mutating itself the same way run_backtesting does."""

    def __init__(self):
        self.controller_config = None
        self.backtesting_resolution = None
        self.window = None
        self.in_flight = 0
        self.max_in_flight = 0

    @classmethod
    def get_controller_config_instance_from_dict(cls, config_data, controllers_module=None):
        # Stateless classmethod on the real engine too -- not part of the race.
        return config_data

    async def run_backtesting(self, controller_config, trade_cost, start, end, backtesting_resolution):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            self.controller_config = controller_config
            self.backtesting_resolution = backtesting_resolution
            self.window = (start, end)
            await asyncio.sleep(0.01)  # the historical candle download
            features = pd.DataFrame(
                {
                    "controller_id": [self.controller_config["id"]],
                    "resolution": [self.backtesting_resolution],
                    "start": [self.window[0]],
                }
            )
            return {
                "executors": [],
                "results": {"sharpe_ratio": None, "controller_id": self.controller_config["id"]},
                "processed_data": {"features": features},
                "position_holds": [],
                "position_held_timeseries": [],
                "pnl_timeseries": [],
            }
        finally:
            self.in_flight -= 1


class StubService(BacktestingService):
    """BacktestingService driving the stub engine, so no market data is needed."""

    def __init__(self, tmp_path):
        super().__init__(max_results=10, results_path=str(tmp_path))
        self._engine = SharedStateEngine()


def _config(tag, start, resolution):
    return {
        "config": {"id": tag, "controller_name": "ema_trend_v1"},
        "start_time": start,
        "end_time": start + 3600,
        "backtesting_resolution": resolution,
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
            service._execute_backtest(_config("alpha", 1000, "1m")),
            service._execute_backtest(_config("beta", 5000, "1h")),
        )
        assert _tags(first) == ("alpha", "1m", 1000, "alpha")
        assert _tags(second) == ("beta", "1h", 5000, "beta")
        # No engine was ever reachable from two in-flight backtests at once.
        assert service._engine.max_in_flight == 1

    asyncio.run(scenario())


def test_sync_run_and_background_task_do_not_cross_configs(tmp_path):
    """POST /backtesting/run and POST /backtesting/tasks share the one service singleton."""

    async def scenario():
        service = StubService(tmp_path)
        task = service.submit_task(_config("background", 7000, "3m"))
        sync_result = await service.run_backtest_sync(_config("foreground", 2000, "5m"))
        await task._asyncio_task

        assert _tags(sync_result) == ("foreground", "5m", 2000, "foreground")
        # The archived task keeps only its metrics resident; the bulk is rehydrated from disk.
        stored = service.get_task_payload(task.task_id)["result"]
        assert _tags(stored) == ("background", "3m", 7000, "background")
        assert service._engine.max_in_flight == 1

    asyncio.run(scenario())
