from decimal import Decimal
from typing import List

import json
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo
from routers.manage_backtesting import BACKTESTING_ENGINES, BacktestingEngine

from fastapi import APIRouter

router = APIRouter(tags=["Market Performance"])


@router.post("/run-performance")
async def run_performance(executors: List[dict], config: dict):
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


def parse_executors(executors: List[dict]) -> List[ExecutorInfo]:
    executor_values = []
    for row in executors:
        executor_values.append(ExecutorInfo(
            id=row["id"],
            timestamp=row["timestamp"],
            type=row["type"],
            close_timestamp=row["close_timestamp"],
            close_type=row["close_type"],
            status=row["status"],
            config=row["config"],
            net_pnl_pct=row["net_pnl_pct"],
            net_pnl_quote=row["net_pnl_quote"],
            cum_fees_quote=row["cum_fees_quote"],
            filled_amount_quote=row["filled_amount_quote"],
            is_active=row["is_active"],
            is_trading=row["is_trading"],
            custom_info=json.loads(row["custom_info"]),
            controller_id=row["controller_id"],
            side=row["side"],
        ))
    return executor_values
