from typing import List, Dict
import os
import yaml
import importlib
import inspect

from hummingbot.strategy_v2.backtesting.backtesting_engine_base import BacktestingEngineBase
from hummingbot.exceptions import InvalidController
from hummingbot.strategy_v2.backtesting.backtesting_data_provider import BacktestingDataProvider
from hummingbot.strategy_v2.controllers import ControllerConfigBase, MarketMakingControllerConfigBase, DirectionalTradingControllerConfigBase
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig

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

    async def initialize_backtesting_data_provider(self):
        for controller in self.controllers:
            backtesting_config = CandlesConfig(
                connector=controller.config.connector_name,
                trading_pair=controller.config.trading_pair,
                interval=self.backtesting_resolution
            )
            await controller.market_data_provider.initialize_candles_feed(backtesting_config)
            for config in controller.config.candles_config:
                await controller.market_data_provider.initialize_candles_feed(config)

    def initialize_controllers(self, controller_configs: List[ControllerConfigBase]):
        self.controllers = []
        for controller_config in controller_configs:
            controller_class = controller_config.get_controller_class()
            controllers = controller_class(config=controller_config, market_data_provider=backtesting_data_provider,
                                           actions_queue=None)
            self.controllers.append(controllers)

    def reset_backtesting_data_provider(self, start: int, end: int, backtesting_resolution: str):
        self.backtesting_resolution = backtesting_resolution
        self.backtesting_data_provider = BacktestingDataProvider(connectors={}, start_time=start, end_time=end)