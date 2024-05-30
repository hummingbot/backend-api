from typing import List, Dict, Tuple, Union

import numpy as np
import pandas as pd
import pandas_ta as ta  # noqa: F401
from pydantic import Field, validator

from hummingbot.client.config.config_data_types import ClientFieldData
from hummingbot.core.data_type.common import TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.directional_trading_controller_base import (
    DirectionalTradingControllerBase,
    DirectionalTradingControllerConfigBase,
)
from hummingbot.strategy_v2.models.executor_actions import StopExecutorAction


class TrendSimpleV1ControllerConfig(DirectionalTradingControllerConfigBase):
    controller_name = "trend_simple_v1"
    candles_config: List[CandlesConfig] = []
    candles_connector: str = Field(default=None, client_data=ClientFieldData(prompt_on_new=True,prompt=lambda mi: "Enter the connector for the candles data, leave empty to use the same exchange as the connector: ", ))
    candles_trading_pair: str = Field(default=None, client_data=ClientFieldData(prompt_on_new=True,prompt=lambda mi: "Enter the trading pair for the candles data, leave empty to use the same trading pair as the connector: ", ))
    interval: str = Field(default="1h", client_data=ClientFieldData(prompt=lambda mi: "Enter the candle interval (e.g., 1m, 5m, 1h, 1d): ", prompt_on_new=False))
    ma_dict: Dict[str, Tuple[int, int]] = Field(default={"ma1": (2, 3), "ma2": (72, 168)})
    thresholds: Dict[str, float] = Field(default={"small": 1.5, "big": 3})
    vol_model: Dict[str, Union[float, int]] = Field(default={"window": 168, "weight_lt": 0.5, "lt_vol": 0.01102})
    coeffs: Dict[str, Tuple[float, float, float, float]] = Field(default={"ma1": (0.001, 0.0183, 0.0076, -0.0402),
                                                                          "ma2": (0.001, 0.0234, 0.0199, 0.0249)})
    trx_cost: float = 0.00025
    ann_sharpe_threshold: float = 2.5

    @validator("candles_connector", pre=True, always=True)
    def set_candles_connector(cls, v, values):
        if v is None or v == "":
            return values.get("connector_name")
        return v

    @validator("candles_trading_pair", pre=True, always=True)
    def set_candles_trading_pair(cls, v, values):
        if v is None or v == "":
            return values.get("trading_pair")
        return v


class TrendSimpleV1Controller(DirectionalTradingControllerBase):
    ANN_FACTOR = {'1h': 24 * 365}

    def __init__(self, config: TrendSimpleV1ControllerConfig, *args, **kwargs):
        self.config = config
        self.max_records = 500
        if len(self.config.candles_config) == 0:
            self.config.candles_config = [CandlesConfig(
                connector=config.candles_connector,
                trading_pair=config.candles_trading_pair,
                interval=config.interval,
                max_records=self.max_records
            )]
        super().__init__(config, *args, **kwargs)


    def forecast(self, df_in: pd.DataFrame) -> pd.DataFrame:
        """ Calculates the signal.  Adds the signal and all intermediate
            columns to the df dataframe
        """
        # Calculate returns
        df = df_in.copy()
        df.sort_index(inplace=True)
        df['r'] = df['close'].pct_change()

        # Rolling volatility estimate
        vol_model = self.config.vol_model
        weight_lt = vol_model['weight_lt']
        st_vol = df['r'].rolling(vol_model['window']).std()
        df['vol'] = vol_model['lt_vol'] * weight_lt + st_vol * (1 - weight_lt)

        # Calculate indicators
        df['r_n'] = df['r'] / df['vol']  # Normalized returns
        ma_dict, thresholds = self.config.ma_dict, self.config.thresholds

        # Moving averages
        for code, ma_range in ma_dict.items():
            first_lag, n_lags = ma_range[1] - ma_range[0] + 1, ma_range[0]
            df[code] = df.r_n.rolling(n_lags).mean().shift(first_lag)
            df[code] *= np.sqrt(n_lags)

        # Calculate signal as a function of the moving averages
        # Sum contributions of each moving average
        df['sig_n'] = 0
        for m, v in ma_dict.items():
            ma = df[m]
            is_small = np.abs(ma) <= thresholds['small']
            is_big = np.abs(ma) >= thresholds['big']
            is_med = 1 - is_small - is_big
            df[m + '_sml'] = ma * is_small
            df[m + '_med'] = ma * is_med
            df[m + '_big'] = ma * is_big
            b0, b1, b2, b3 = self.config.coeffs[m]
            df[m + '_sig_n'] = b0 + b1 * df[m + '_sml'] \
                                 + b2 * df[m + '_med'] + b3 * df[m + '_big']
            df['sig_n'] += df[m + '_sig_n']

        # Scale up
        ann_factor = self.ANN_FACTOR[self.config.interval]

        df['sig'] = df['sig_n'] * df['vol']
        df['ann_sharpe'] = df['sig_n'] * np.sqrt(ann_factor)

        # Subtract expected transactions costs
        df['trade_length'] = 1
        for m, v in ma_dict.items():
            ma_length = v[1] - v[0] + 1
            df['trade_length'] = np.maximum(df.trade_length, ma_length
                                              ).where(np.sign(df.sig * df[m + "_sig_n"]) > 0,
                                                      df.trade_length)
        df['trx_per_day'] = self.config.trx_cost / df['trade_length'] * 2
        df['sig_net'] = np.sign(df.sig) * np.maximum(np.abs(df.sig) - df.trx_per_day, 0)
        df['ann_sharpe_net'] = df.sig_net / df['vol'] * np.sqrt(ann_factor)
        return df

    async def update_processed_data(self):
        df = self.market_data_provider.get_candles_df(connector_name=self.config.candles_connector,
                                                      trading_pair=self.config.candles_trading_pair,
                                                      interval=self.config.interval,
                                                      max_records=self.max_records)
        # Add indicators
        df = self.forecast(df)

        # Generate signal
        long_condition = df["ann_sharpe_net"] > self.config.ann_sharpe_threshold
        short_condition = df["ann_sharpe_net"] < -self.config.ann_sharpe_threshold

        # Generate signal
        df["signal"] = 0
        df.loc[long_condition, "signal"] = 1
        df.loc[short_condition, "signal"] = -1

        # Update processed data
        self.processed_data["signal"] = df["signal"].iloc[-1]
        self.processed_data["features"] = df
