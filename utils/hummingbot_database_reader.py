import json
import os
from typing import Any, Dict, List

import pandas as pd
from hummingbot.core.data_type.common import TradeType
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


class HummingbotDatabase:
    def __init__(self, db_path: str):
        if not os.path.isfile(db_path):
            raise FileNotFoundError(f"Database file '{db_path}' not found")
        self.db_name = os.path.basename(db_path)
        self.db_path = db_path
        self.db_path = f'sqlite:///{os.path.join(db_path)}'
        self.engine = create_engine(self.db_path, connect_args={'check_same_thread': False})
        self.session_maker = sessionmaker(bind=self.engine)

    @staticmethod
    def _get_table_status(table_loader):
        try:
            data = table_loader()
            return "Correct" if len(data) > 0 else "Error - No records matched"
        except Exception as e:
            return f"Error - {str(e)}"

    @property
    def status(self):
        trade_fill_status = self._get_table_status(self.get_trade_fills)
        orders_status = self._get_table_status(self.get_orders)
        order_status_status = self._get_table_status(self.get_order_status)
        executors_status = self._get_table_status(self.get_executors_data)
        controller_status = self._get_table_status(self.get_controllers_data)
        positions_status = self._get_table_status(self.get_positions)
        general_status = all(status == "Correct" for status in
                             [trade_fill_status, orders_status, order_status_status,
                              executors_status, controller_status, positions_status])
        status = {"db_name": self.db_name,
                  "db_path": self.db_path,
                  "trade_fill": trade_fill_status,
                  "orders": orders_status,
                  "order_status": order_status_status,
                  "executors": executors_status,
                  "controllers": controller_status,
                  "positions": positions_status,
                  "general_status": general_status
                  }
        return status

    @staticmethod
    def _as_numeric(df: pd.DataFrame, columns: List[str]) -> pd.DataFrame:
        """Force the given columns to a numeric dtype, in place.

        A table with zero rows comes back from read_sql_query with `object` columns --
        pandas has nothing to infer a dtype from -- and object dtype is not merely
        cosmetic downstream: `.cumsum()` raises `TypeError: cumsum is not supported for
        object dtype` on it however empty the frame is. That is a real archived bot, not a
        synthetic one: a bot whose order size sits below the exchange minimum has every
        order rejected and archives with an empty TradeFill table, which turned
        /archived-bots/{db}/performance and /summary into 500s.

        Coercing per column rather than with DataFrame.apply is deliberate: apply never
        calls the function on a frame with no rows, so it leaves exactly the case this
        exists for untouched.
        """
        for column in columns:
            if column in df.columns:
                df[column] = pd.to_numeric(df[column], errors="coerce")
        return df

    def get_orders(self):
        with self.session_maker() as session:
            query = "SELECT * FROM 'Order'"
            orders = pd.read_sql_query(text(query), session.connection())
            self._as_numeric(orders, ["amount", "price"])
            orders["amount"] = orders["amount"] / 1e6
            orders["price"] = orders["price"] / 1e6
            orders.rename(columns={"market": "connector_name", "symbol": "trading_pair"}, inplace=True)
        return orders

    def get_trade_fills(self):
        groupers = ["config_file_path", "connector_name", "trading_pair"]
        float_cols = ["amount", "price", "trade_fee_in_quote"]
        with self.session_maker() as session:
            query = "SELECT * FROM TradeFill"
            trade_fills = pd.read_sql_query(text(query), session.connection())
            trade_fills.rename(columns={"market": "connector_name", "symbol": "trading_pair"}, inplace=True)
            self._as_numeric(trade_fills, float_cols)
            trade_fills[float_cols] = trade_fills[float_cols] / 1e6
            trade_fills["cum_fees_in_quote"] = trade_fills.groupby(groupers)["trade_fee_in_quote"].cumsum()
            trade_fills["trade_fee"] = trade_fills.groupby(groupers)["cum_fees_in_quote"].diff()
        return trade_fills

    def get_order_status(self):
        with self.session_maker() as session:
            query = "SELECT * FROM OrderStatus"
            order_status = pd.read_sql_query(text(query), session.connection())
        return order_status

    def get_executors_data(self) -> pd.DataFrame:
        with self.session_maker() as session:
            query = "SELECT * FROM Executors"
            executors = pd.read_sql_query(text(query), session.connection())
        return executors

    def get_controllers_data(self) -> pd.DataFrame:
        with self.session_maker() as session:
            query = "SELECT * FROM Controllers"
            controllers = pd.read_sql_query(text(query), session.connection())
        return controllers

    def get_positions(self) -> pd.DataFrame:
        with self.session_maker() as session:
            query = "SELECT * FROM Position"
            positions = pd.read_sql_query(text(query), session.connection())
            # Convert decimal fields from stored format (divide by 1e6)
            decimal_cols = ["volume_traded_quote", "amount", "breakeven_price", "unrealized_pnl_quote", "cum_fees_quote"]
            self._as_numeric(positions, decimal_cols)
            positions[decimal_cols] = positions[decimal_cols] / 1e6
        return positions

    def calculate_trade_based_performance(self) -> pd.DataFrame:
        """
        Calculate trade-based performance metrics using vectorized pandas operations.

        Returns:
            DataFrame with rolling performance metrics calculated per trading pair.
        """
        # Get trade fills data
        trades = self.get_trade_fills()

        if len(trades) == 0:
            return pd.DataFrame()

        # Sort by timestamp to ensure proper rolling calculation
        trades = trades.sort_values(['trading_pair', 'connector_name', 'timestamp']).copy()

        # Create buy/sell indicator columns
        trades['is_buy'] = (trades['trade_type'].str.upper() == 'BUY').astype(int)
        trades['is_sell'] = (trades['trade_type'].str.upper() == 'SELL').astype(int)

        # Calculate buy and sell amounts and values vectorized
        trades['buy_amount'] = trades['amount'] * trades['is_buy']
        trades['sell_amount'] = trades['amount'] * trades['is_sell']
        trades['buy_value'] = trades['price'] * trades['amount'] * trades['is_buy']
        trades['sell_value'] = trades['price'] * trades['amount'] * trades['is_sell']

        # Group by trading_pair and connector_name for rolling calculations
        grouper = ['trading_pair', 'connector_name']

        # Calculate cumulative volumes and values
        trades['buy_volume'] = trades.groupby(grouper)['buy_amount'].cumsum()
        trades['sell_volume'] = trades.groupby(grouper)['sell_amount'].cumsum()
        trades['buy_value_cum'] = trades.groupby(grouper)['buy_value'].cumsum()
        trades['sell_value_cum'] = trades.groupby(grouper)['sell_value'].cumsum()

        # Calculate average prices (avoid division by zero)
        trades['buy_avg_price'] = trades['buy_value_cum'] / trades['buy_volume'].replace(0, pd.NA)
        trades['sell_avg_price'] = trades['sell_value_cum'] / trades['sell_volume'].replace(0, pd.NA)

        # Forward fill average prices within each group to handle NaN values
        trades['buy_avg_price'] = trades.groupby(grouper)['buy_avg_price'].ffill().fillna(0)
        trades['sell_avg_price'] = trades.groupby(grouper)['sell_avg_price'].ffill().fillna(0)

        # Calculate net position
        trades['net_position'] = trades['buy_volume'] - trades['sell_volume']

        # Calculate realized PnL
        trades['realized_trade_pnl_pct'] = (
            (trades['sell_avg_price'] - trades['buy_avg_price']) / trades['buy_avg_price']
        ).fillna(0)

        # Matched volume for realized PnL (minimum of buy and sell volumes)
        trades['matched_volume'] = pd.concat([trades['buy_volume'], trades['sell_volume']], axis=1).min(axis=1)
        trades['realized_trade_pnl_quote'] = trades['realized_trade_pnl_pct'] * trades['matched_volume'] * trades['buy_avg_price']

        # Calculate unrealized PnL based on position direction
        # For long positions (net_position > 0): use current price vs buy_avg_price
        # For short positions (net_position < 0): use sell_avg_price vs current price
        trades['unrealized_trade_pnl_pct'] = 0.0

        # Long positions
        long_mask = trades['net_position'] > 0
        trades.loc[long_mask, 'unrealized_trade_pnl_pct'] = (
            (trades.loc[long_mask, 'price'] - trades.loc[long_mask, 'buy_avg_price']) /
            trades.loc[long_mask, 'buy_avg_price']
        ).fillna(0)

        # Short positions
        short_mask = trades['net_position'] < 0
        trades.loc[short_mask, 'unrealized_trade_pnl_pct'] = (
            (trades.loc[short_mask, 'sell_avg_price'] - trades.loc[short_mask, 'price']) /
            trades.loc[short_mask, 'sell_avg_price']
        ).fillna(0)

        # Calculate unrealized PnL in quote currency
        trades['unrealized_trade_pnl_quote'] = 0.0

        # Long positions: use buy_avg_price as reference
        long_mask = trades['net_position'] > 0
        trades.loc[long_mask, 'unrealized_trade_pnl_quote'] = (
            trades.loc[long_mask, 'unrealized_trade_pnl_pct'] *
            trades.loc[long_mask, 'net_position'].abs() *
            trades.loc[long_mask, 'buy_avg_price']
        )

        # Short positions: use sell_avg_price as reference
        short_mask = trades['net_position'] < 0
        trades.loc[short_mask, 'unrealized_trade_pnl_quote'] = (
            trades.loc[short_mask, 'unrealized_trade_pnl_pct'] *
            trades.loc[short_mask, 'net_position'].abs() *
            trades.loc[short_mask, 'sell_avg_price']
        )

        # Fees are already in trade_fee_in_quote column
        trades['fees_quote'] = trades['trade_fee_in_quote']

        # Calculate net PnL
        trades['net_pnl_quote'] = (
            trades['realized_trade_pnl_quote'] +
            trades['unrealized_trade_pnl_quote'] -
            trades['fees_quote']
        )

        # Calculate cumulative volume in quote currency
        trades['volume_quote'] = trades['price'] * trades['amount']
        trades['cum_volume_quote'] = trades.groupby(grouper)['volume_quote'].cumsum()

        # Select and return relevant columns
        result_columns = [
            'timestamp', 'price', 'amount', 'trade_type', 'trading_pair', 'connector_name',
            'buy_avg_price', 'buy_volume', 'sell_avg_price', 'sell_volume',
            'net_position', 'realized_trade_pnl_pct', 'realized_trade_pnl_quote',
            'unrealized_trade_pnl_pct', 'unrealized_trade_pnl_quote',
            'fees_quote', 'net_pnl_quote', 'volume_quote', 'cum_volume_quote'
        ]

        return trades[result_columns].sort_values('timestamp')


class PerformanceDataSource:
    def __init__(self, executors_dict: Dict[str, Any]):
        self.executors_dict = executors_dict

    @property
    def executors_df(self):
        executors = pd.DataFrame(self.executors_dict)
        executors["custom_info"] = executors["custom_info"].apply(
            lambda x: json.loads(x) if isinstance(x, str) else x)
        executors["config"] = executors["config"].apply(lambda x: json.loads(x) if isinstance(x, str) else x)
        executors["timestamp"] = executors["timestamp"].apply(lambda x: self.ensure_timestamp_in_seconds(x))
        executors["close_timestamp"] = executors["close_timestamp"].apply(
            lambda x: self.ensure_timestamp_in_seconds(x))
        executors["trading_pair"] = executors["config"].apply(lambda x: x["trading_pair"])
        executors["exchange"] = executors["config"].apply(lambda x: x["connector_name"])
        executors["level_id"] = executors["config"].apply(lambda x: x.get("level_id"))
        executors["bep"] = executors["custom_info"].apply(lambda x: x["current_position_average_price"])
        executors["order_ids"] = executors["custom_info"].apply(lambda x: x.get("order_ids"))
        executors["close_price"] = executors["custom_info"].apply(
            lambda x: x.get("close_price", x["current_position_average_price"]))
        executors["sl"] = executors["config"].apply(lambda x: x.get("stop_loss")).fillna(0)
        executors["tp"] = executors["config"].apply(lambda x: x.get("take_profit")).fillna(0)
        executors["tl"] = executors["config"].apply(lambda x: x.get("time_limit")).fillna(0)
        return executors

    @property
    def executor_info_list(self) -> List[ExecutorInfo]:
        executors = self.apply_special_data_types(self.executors_df)
        executor_values = []
        for index, row in executors.iterrows():
            executor_to_append = ExecutorInfo(
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
                custom_info=row["custom_info"],
                controller_id=row["controller_id"]
            )
            executor_to_append.custom_info["side"] = row["side"]
            executor_values.append(executor_to_append)
        return executor_values

    def apply_special_data_types(self, executors):
        executors["status"] = executors["status"].apply(lambda x: self.get_enum_by_value(RunnableStatus, int(x)))
        executors["side"] = executors["config"].apply(lambda x: self.get_enum_by_value(TradeType, int(x["side"])))
        executors["close_type"] = executors["close_type"].apply(lambda x: self.get_enum_by_value(CloseType, int(x)))
        executors["close_type_name"] = executors["close_type"].apply(lambda x: x.name)
        executors["datetime"] = pd.to_datetime(executors.timestamp, unit="s")
        executors["close_datetime"] = pd.to_datetime(executors["close_timestamp"], unit="s")
        return executors

    @staticmethod
    def get_enum_by_value(enum_class, value):
        for member in enum_class:
            if member.value == value:
                return member
        raise ValueError(f"No enum member with value {value}")

    @staticmethod
    def ensure_timestamp_in_seconds(timestamp: float) -> float:
        """
        Ensure the given timestamp is in seconds.
        Args:
        - timestamp (int): The input timestamp which could be in seconds, milliseconds, or microseconds.
        Returns:
        - int: The timestamp in seconds.
        Raises:
        - ValueError: If the timestamp is not in a recognized format.
        """
        timestamp_int = int(float(timestamp))
        if timestamp_int >= 1e18:  # Nanoseconds
            return timestamp_int / 1e9
        elif timestamp_int >= 1e15:  # Microseconds
            return timestamp_int / 1e6
        elif timestamp_int >= 1e12:  # Milliseconds
            return timestamp_int / 1e3
        elif timestamp_int >= 1e9:  # Seconds
            return timestamp_int
        else:
            raise ValueError(
                "Timestamp is not in a recognized format. Must be in seconds, milliseconds, microseconds or nanoseconds.")
