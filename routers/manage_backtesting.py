import asyncio
from typing import Any, Dict
import datetime

import numpy as np
import pandas as pd
from fastapi import APIRouter
from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory, CandlesConfig
from hummingbot.strategy_v2.controllers.controller_base import ControllerConfigBase
from pydantic import BaseModel

from utils.backtesting_engine import BacktestingEngine

router = APIRouter(tags=["Market Backtesting"])
candles_factory = CandlesFactory()


class BacktestingConfig(BaseModel):
    start_time: int = 1672542000000  # 2023-01-01 00:00:00
    end_time: int = 1672628400000  # 2023-01-01 23:59:00
    config_path: str
    backtesting_resolution: str = "1m"
    trade_cost: float = 0.0006


def get_backtesting_engine(controller_config: ControllerConfigBase):
    controller_type = controller_config.controller_type
    if controller_type == "directional_trading":
        from hummingbot.strategy_v2.backtesting.controllers_backtesting.directional_trading_backtesting import \
            DirectionalTradingBacktesting
        backtesting_engine = DirectionalTradingBacktesting()
    elif controller_type == "market_making":
        from hummingbot.strategy_v2.backtesting.controllers_backtesting.market_making_backtesting import \
            MarketMakingBacktesting
        backtesting_engine = MarketMakingBacktesting()
    else:
        raise ValueError("Invalid controller type")
    return backtesting_engine


@router.post("/run-backtesting")
async def run_backtesting(backtesting_config: BacktestingConfig):
    try:
        controller_config = BacktestingEngine.load_controller_config(backtesting_config.config_path)
        backtesting_engine = get_backtesting_engine(controller_config)
        backtesting_results = await backtesting_engine.run_backtesting(
            controller_config=controller_config, trade_cost=backtesting_config.trade_cost,
            start=int(backtesting_config.start_time), end=int(backtesting_config.end_time),
            backtesting_resolution=backtesting_config.backtesting_resolution)
        backtesting_results["processed_data"] = backtesting_results["processed_data"]["features"]
        return backtesting_results
    except Exception as e:
        return {"error": str(e)}
