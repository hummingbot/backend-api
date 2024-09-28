from typing import Dict, Union

from fastapi import APIRouter, Depends
from fastapi_walletauth import JWTWalletAuthDep
from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory
from hummingbot.strategy_v2.backtesting.backtesting_engine_base import BacktestingEngineBase
from hummingbot.strategy_v2.backtesting.controllers_backtesting.directional_trading_backtesting import (
    DirectionalTradingBacktesting,
)
from hummingbot.strategy_v2.backtesting.controllers_backtesting.market_making_backtesting import MarketMakingBacktesting
from pydantic import BaseModel

from config import CONTROLLERS_MODULE, CONTROLLERS_PATH

router = APIRouter(tags=["Market Backtesting"])
candles_factory = CandlesFactory()
directional_trading_backtesting = DirectionalTradingBacktesting()
market_making_backtesting = MarketMakingBacktesting()

BACKTESTING_ENGINES = {
    "directional_trading": directional_trading_backtesting,
    "market_making": market_making_backtesting
}


class BacktestingConfig(BaseModel):
    start_time: int = 1672542000  # 2023-01-01 00:00:00
    end_time: int = 1672628400  # 2023-01-01 23:59:00
    backtesting_resolution: str = "1m"
    trade_cost: float = 0.0006
    config: Union[Dict, str]


@router.post("/run-backtesting")
async def run_backtesting(backtesting_config: BacktestingConfig, wa: JWTWalletAuthDep):
    try:
        if isinstance(backtesting_config.config, str):
            controller_config = BacktestingEngineBase.get_controller_config_instance_from_yml(
                config_path=backtesting_config.config,
                controllers_conf_dir_path=CONTROLLERS_PATH,
                controllers_module=CONTROLLERS_MODULE
            )
        else:
            controller_config = BacktestingEngineBase.get_controller_config_instance_from_dict(
                config_data=backtesting_config.config,
                controllers_module=CONTROLLERS_MODULE
            )
        backtesting_engine = BACKTESTING_ENGINES.get(controller_config.controller_type)
        if not backtesting_engine:
            raise ValueError(f"Backtesting engine for controller type {controller_config.controller_type} not found.")
        backtesting_results = await backtesting_engine.run_backtesting(
            controller_config=controller_config, trade_cost=backtesting_config.trade_cost,
            start=int(backtesting_config.start_time), end=int(backtesting_config.end_time),
            backtesting_resolution=backtesting_config.backtesting_resolution)
        processed_data = backtesting_results["processed_data"]["features"].fillna(0)
        executors_info = [e.to_dict() for e in backtesting_results["executors"]]
        backtesting_results["processed_data"] = processed_data.to_dict()
        results = backtesting_results["results"]
        results["sharpe_ratio"] = results["sharpe_ratio"] if results["sharpe_ratio"] is not None else 0
        return {
            "executors": executors_info,
            "processed_data": backtesting_results["processed_data"],
            "results": backtesting_results["results"],
        }
    except Exception as e:
        return {"error": str(e)}
