from typing import List, Dict
import os

import pandas as pd
from decimal import Decimal
import yaml
import importlib
import inspect

from hummingbot.strategy_v2.backtesting.backtesting_engine_base import BacktestingEngineBase
from hummingbot.exceptions import InvalidController
from hummingbot.strategy_v2.backtesting.backtesting_data_provider import BacktestingDataProvider
from hummingbot.strategy_v2.controllers import ControllerConfigBase, MarketMakingControllerConfigBase, DirectionalTradingControllerConfigBase

import config


class BacktestingEngine(BacktestingEngineBase):

    @classmethod
    def load_controller_config(cls, config_path: str) -> Dict:
        full_path = os.path.join(config.CONTROLLERS_PATH, config_path)
        with open(full_path, 'r') as file:
            config_data = yaml.safe_load(file)
        return config_data

    @classmethod
    def get_controller_config_instance_from_yml(cls, config_path: str) -> ControllerConfigBase:
        config_data = cls.load_controller_config(config_path)
        return cls.get_controller_config_instance_from_dict(config_data)

    @classmethod
    def get_controller_config_instance_from_dict(cls, config_data: Dict) -> ControllerConfigBase:
        controller_type = config_data.get('controller_type')
        controller_name = config_data.get('controller_name')

        if not controller_type or not controller_name:
            raise ValueError(f"Missing controller_type or controller_name")

        module_path = f"{config.CONTROLLERS_MODULE}.{controller_type}.{controller_name}"
        module = importlib.import_module(module_path)

        config_class = next((member for member_name, member in inspect.getmembers(module)
                             if inspect.isclass(member) and member not in [ControllerConfigBase,
                                                                           MarketMakingControllerConfigBase,
                                                                           DirectionalTradingControllerConfigBase]
                             and (issubclass(member, ControllerConfigBase))), None)
        if not config_class:
            raise InvalidController(f"No configuration class found in the module {controller_name}.")

        return config_class(**config_data)

    def prepare_market_data(self) -> pd.DataFrame:
        """
        Prepares market data by merging candle data with strategy features, filling missing values.

        Returns:
            pd.DataFrame: The prepared market data with necessary features.
        """
        backtesting_candles = self.controller.market_data_provider.get_candles_df(
            connector_name=self.controller.config.connector_name,
            trading_pair=self.controller.config.trading_pair,
            interval=self.backtesting_resolution
        ).add_suffix("_bt")

        if "features" not in self.controller.processed_data:
            backtesting_candles["reference_price"] = backtesting_candles["close_bt"]
            backtesting_candles["spread_multiplier"] = 1
            backtesting_candles["signal"] = 0
        else:
            backtesting_candles = pd.merge_asof(backtesting_candles, self.controller.processed_data["features"],
                                                left_on="timestamp_bt", right_on="timestamp",
                                                direction="backward")
        backtesting_candles["timestamp"] = backtesting_candles["timestamp_bt"]
        backtesting_candles["open"] = backtesting_candles["open_bt"]
        backtesting_candles["high"] = backtesting_candles["high_bt"]
        backtesting_candles["low"] = backtesting_candles["low_bt"]
        backtesting_candles["close"] = backtesting_candles["close_bt"]
        backtesting_candles["volume"] = backtesting_candles["volume_bt"]
        # TODO: Apply this changes in the Base class to avoid code duplication
        backtesting_candles.dropna(inplace=True)
        self.controller.processed_data["features"] = backtesting_candles
        return backtesting_candles


class MarketMakingBacktesting(BacktestingEngine):
    def update_processed_data(self, row: pd.Series):
        self.controller.processed_data["reference_price"] = Decimal(row["reference_price"])
        self.controller.processed_data["spread_multiplier"] = Decimal(row["spread_multiplier"])


class DirectionalTradingBacktesting(BacktestingEngine):
    def update_processed_data(self, row: pd.Series):
        self.controller.processed_data["signal"] = row["signal"]
