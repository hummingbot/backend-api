import os
from decimal import Decimal
from typing import Dict

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionSide
from hummingbot.data_feed.candles_feed.candles_factory import CandlesConfig
from hummingbot.smart_components.controllers.dman_v1 import DManV1, DManV1Config
from hummingbot.smart_components.strategy_frameworks.data_types import (
    ExecutorHandlerStatus,
    TripleBarrierConf,
)
from hummingbot.smart_components.strategy_frameworks.market_making.market_making_executor_handler import (
    MarketMakingExecutorHandler,
)
from hummingbot.smart_components.utils.distributions import Distributions
from hummingbot.smart_components.utils.order_level_builder import OrderLevelBuilder
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from pydantic import Field, BaseModel


class DManV1ScriptConfig(BaseModel):
    script_file_name: str = Field(default_factory=lambda: os.path.basename(__file__))

    # Account configuration
    exchange: str = Field(
        "binance_perpetual", description="The exchange to run the bot on.")



class DManV1MultiplePairs(ScriptStrategyBase):
    @classmethod
    def init_markets(cls, config: DManV1ScriptConfig):
        cls.markets = {config.exchange: set(config.trading_pairs.split(","))}

    def __init__(
        self, connectors: Dict[str, ConnectorBase], config: DManV1ScriptConfig
    ):
        super().__init__(connectors)
        self.config = config

        # Initialize order level builder
        order_level_builder = OrderLevelBuilder(n_levels=config.n_levels)
        order_levels = order_level_builder.build_order_levels(
            amounts=config.order_amount,
            spreads=Distributions.arithmetic(
                n_levels=config.n_levels,
                start=config.start_spread,
                step=config.step_between_orders,
            ),
            triple_barrier_confs=TripleBarrierConf(
                stop_loss=config.stop_loss,
                take_profit=config.take_profit,
                time_limit=config.time_limit,
                trailing_stop_activation_price_delta=config.trailing_stop_activation_price_delta,
                trailing_stop_trailing_delta=config.trailing_stop_trailing_delta,
            ),
            order_refresh_time=config.order_refresh_time,
            cooldown_time=config.cooldown_time,
        )

        # Initialize controllers and executor handlers
        self.controllers = {}
        self.executor_handlers = {}
        self.markets = {}
        candles_max_records = (
            config.natr_length + 100
        )  # We need to get more candles than the indicators need

        for trading_pair in config.trading_pairs.split(","):
            # Configure the strategy for each trading pair
            dman_config = DManV1Config(
                exchange=config.exchange,
                trading_pair=trading_pair,
                order_levels=order_levels,
                candles_config=[
                    CandlesConfig(
                        connector=config.candles_exchange,
                        trading_pair=trading_pair,
                        interval=config.candles_interval,
                        max_records=candles_max_records,
                    ),
                ],
                leverage=config.leverage,
                natr_length=config.natr_length,
            )

            # Instantiate the controller for each trading pair
            controller = DManV1(config=dman_config)
            self.markets = controller.update_strategy_markets_dict(self.markets)
            self.controllers[trading_pair] = controller

            # Create and store the executor handler for each trading pair
            self.executor_handlers[trading_pair] = MarketMakingExecutorHandler(
                strategy=self, controller=controller
            )

    @property
    def is_perpetual(self):
        """
        Checks if the exchange is a perpetual market.
        """
        return "perpetual" in self.config.exchange

    def on_stop(self):
        if self.is_perpetual:
            self.close_open_positions()

    def close_open_positions(self):
        # we are going to close all the open positions when the bot stops
        for connector_name, connector in self.connectors.items():
            for trading_pair, position in connector.account_positions.items():
                if trading_pair in connector.trading_pairs:
                    if position.position_side == PositionSide.LONG:
                        self.sell(
                            connector_name=connector_name,
                            trading_pair=position.trading_pair,
                            amount=abs(position.amount),
                            order_type=OrderType.MARKET,
                            price=connector.get_mid_price(position.trading_pair),
                            position_action=PositionAction.CLOSE,
                        )
                    elif position.position_side == PositionSide.SHORT:
                        self.buy(
                            connector_name=connector_name,
                            trading_pair=position.trading_pair,
                            amount=abs(position.amount),
                            order_type=OrderType.MARKET,
                            price=connector.get_mid_price(position.trading_pair),
                            position_action=PositionAction.CLOSE,
                        )

    def on_tick(self):
        """
        This shows you how you can start meta controllers. You can run more than one at the same time and based on the
        market conditions, you can orchestrate from this script when to stop or start them.
        """
        for executor_handler in self.executor_handlers.values():
            if executor_handler.status == ExecutorHandlerStatus.NOT_STARTED:
                executor_handler.start()

    def format_status(self) -> str:
        if not self.ready_to_trade:
            return "Market connectors are not ready."
        lines = []
        for trading_pair, executor_handler in self.executor_handlers.items():
            lines.extend(
                [
                    f"Strategy: {executor_handler.controller.config.strategy_name} | Trading Pair: {trading_pair}",
                    executor_handler.to_format_status(),
                ]
            )
        return "\n".join(lines)
