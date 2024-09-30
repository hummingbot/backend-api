from typing import List, Any, Dict

from hummingbot.strategy_v2.backtesting.backtesting_engine_base import BacktestingEngineBase

from routers.manage_backtesting import BACKTESTING_ENGINES

from fastapi import APIRouter

from utils.etl_databases import PerformanceDataSource

router = APIRouter(tags=["Market Performance"])


@router.post("/get-performance-results")
async def get_performance_results(payload: Dict[str, Any]):
    executors = payload.get("executors")
    data_source = PerformanceDataSource(executors)
    performance_results = {}
    try:
        backtesting_engine = BacktestingEngineBase()
        performance_results["results"] = backtesting_engine.summarize_results(data_source.executor_info_list)
        results = performance_results["results"]
        results["sharpe_ratio"] = results["sharpe_ratio"] if results["sharpe_ratio"] is not None else 0
        return {
            "executors": executors,
            "results": performance_results["results"],
        }

    except Exception as e:
        return {"error": str(e)}
