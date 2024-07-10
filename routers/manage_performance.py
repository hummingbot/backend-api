from decimal import Decimal
from typing import List

import json
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo
from routers.manage_backtesting import BACKTESTING_ENGINES, BacktestingEngine

from fastapi import APIRouter

router = APIRouter(tags=["Market Performance"])


@router.post("/get-performance-results")
async def get_performance_results(executors: List[dict]):
    performance_results = {}
    try:
        for executor in executors:
            if isinstance(executor["custom_info"], str):
                executor["custom_info"] = json.loads(executor["custom_info"])
        parsed_executors = [ExecutorInfo(**executor) for executor in executors]
        performance_results["results"] = BacktestingEngine.summarize_results(parsed_executors)
        results = performance_results["results"]
        results["sharpe_ratio"] = results["sharpe_ratio"] if results["sharpe_ratio"] is not None else 0
        return {
            "results": performance_results["results"],
        }

    except Exception as e:
        return {"error": str(e)}


@router.post("/get-performance-results-with-config")
async def get_performance_results_with_config(executors: List[dict], config: dict):
    performance_results = {}
    try:
        controller_config = BacktestingEngine.get_controller_config_instance_from_dict(config)
        performance_engine = BACKTESTING_ENGINES.get(controller_config.controller_type)
        if not performance_engine:
            raise ValueError(f"Performance engine for controller type {controller_config.controller_type} not found.")
        for executor in executors:
            executor["custom_info"] = json.loads(executor["custom_info"])
        parsed_executors = [ExecutorInfo(**executor) for executor in executors]
        total_amount_quote = Decimal(str(controller_config.total_amount_quote))
        performance_results["results"] = performance_engine.summarize_results(parsed_executors, total_amount_quote)
        results = performance_results["results"]
        results["sharpe_ratio"] = results["sharpe_ratio"] if results["sharpe_ratio"] is not None else 0

        return {
            "executors": executors,
            "results": performance_results["results"],
        }

    except Exception as e:
        return {"error": str(e)}
