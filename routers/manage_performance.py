from decimal import Decimal
from typing import List

import json
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo
from hummingbot.strategy_v2.backtesting.backtesting_engine_base import BacktestingEngineBase

from routers.manage_backtesting import BACKTESTING_ENGINES

from fastapi import APIRouter

router = APIRouter(tags=["Market Performance"])


@router.post("/get-performance-results")
async def get_performance_results(executors_and_controller_type: dict):
    executors = executors_and_controller_type.get("executors")
    controller_type = executors_and_controller_type.get("controller_type")
    performance_results = {}
    try:
        for executor in executors:
            if isinstance(executor["custom_info"], str):
                executor["custom_info"] = json.loads(executor["custom_info"])
        parsed_executors = [ExecutorInfo(**executor) for executor in executors]
        backtesting_engine = BACKTESTING_ENGINES[controller_type]
        performance_results["results"] = backtesting_engine.summarize_results(parsed_executors)
        results = performance_results["results"]
        results["sharpe_ratio"] = results["sharpe_ratio"] if results["sharpe_ratio"] is not None else 0
        return {
            "results": performance_results["results"],
        }

    except Exception as e:
        return {"error": str(e)}
