import os
import pandas as pd
import json
from typing import List, Dict, Any

from hummingbot.core.data_type.common import TradeType
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo
from sqlalchemy import create_engine, insert, text, MetaData, Table, Column, VARCHAR, INT, FLOAT,  Integer, String, Float
from sqlalchemy.orm import sessionmaker


class HummingbotDatabase:
    def __init__(self, db_path: str):
        self.db_name = os.path.basename(db_path)
        self.db_path = db_path
        self.db_path = f'sqlite:///{os.path.join(db_path)}'
        self.engine = create_engine(self.db_path, connect_args={'check_same_thread': False})
        self.session_maker = sessionmaker(bind=self.engine)

    @staticmethod
    def _get_table_status(table_loader):
        try:
            data = table_loader()
            return "Correct" if len(data) > 0 else f"Error - No records matched"
        except Exception as e:
            return f"Error - {str(e)}"

    @property
    def status(self):
        trade_fill_status = self._get_table_status(self.get_trade_fills)
        orders_status = self._get_table_status(self.get_orders)
        order_status_status = self._get_table_status(self.get_order_status)
        executors_status = self._get_table_status(self.get_executors_data)
        controller_status = self._get_table_status(self.get_controllers_data)
        general_status = all(status == "Correct" for status in
                             [trade_fill_status, orders_status, order_status_status, executors_status, controller_status])
        status = {"db_name": self.db_name,
                  "db_path": self.db_path,
                  "trade_fill": trade_fill_status,
                  "orders": orders_status,
                  "order_status": order_status_status,
                  "executors": executors_status,
                  "general_status": general_status
                  }
        return status

    def get_orders(self):
        with self.session_maker() as session:
            query = "SELECT * FROM 'Order'"
            orders = pd.read_sql_query(text(query), session.connection())
            orders["market"] = orders["market"]
            orders["amount"] = orders["amount"] / 1e6
            orders["price"] = orders["price"] / 1e6
            # orders['creation_timestamp'] = pd.to_datetime(orders['creation_timestamp'], unit="ms")
            # orders['last_update_timestamp'] = pd.to_datetime(orders['last_update_timestamp'], unit="ms")
        return orders

    def get_trade_fills(self):
        groupers = ["config_file_path", "market", "symbol"]
        float_cols = ["amount", "price", "trade_fee_in_quote"]
        with self.session_maker() as session:
            query = "SELECT * FROM TradeFill"
            trade_fills = pd.read_sql_query(text(query), session.connection())
            trade_fills[float_cols] = trade_fills[float_cols] / 1e6
            trade_fills["cum_fees_in_quote"] = trade_fills.groupby(groupers)["trade_fee_in_quote"].cumsum()
            trade_fills["trade_fee"] = trade_fills.groupby(groupers)["cum_fees_in_quote"].diff()
            # trade_fills["timestamp"] = pd.to_datetime(trade_fills["timestamp"], unit="ms")
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


class ETLPerformance:
    def __init__(self,
                 db_path: str):
        self.db_path = f'sqlite:///{os.path.join(db_path)}'
        self.engine = create_engine(self.db_path, connect_args={'check_same_thread': False})
        self.session_maker = sessionmaker(bind=self.engine)
        self.metadata = MetaData()

    @property
    def executors_table(self):
        return Table('executors',
                     MetaData(),
                     Column('id', String),
                     Column('timestamp', Integer),
                     Column('type', String),
                     Column('close_type', Integer),
                     Column('close_timestamp', Integer),
                     Column('status', String),
                     Column('config', String),
                     Column('net_pnl_pct', Float),
                     Column('net_pnl_quote', Float),
                     Column('cum_fees_quote', Float),
                     Column('filled_amount_quote', Float),
                     Column('is_active', Integer),
                     Column('is_trading', Integer),
                     Column('custom_info', String),
                     Column('controller_id', String))

    @property
    def trade_fill_table(self):
        return Table(
            'trades', MetaData(),
            Column('config_file_path', VARCHAR(255)),
            Column('strategy', VARCHAR(255)),
            Column('market', VARCHAR(255)),
            Column('symbol', VARCHAR(255)),
            Column('base_asset', VARCHAR(255)),
            Column('quote_asset', VARCHAR(255)),
            Column('timestamp', INT),
            Column('order_id', VARCHAR(255)),
            Column('trade_type', VARCHAR(255)),
            Column('order_type', VARCHAR(255)),
            Column('price', FLOAT),
            Column('amount', FLOAT),
            Column('leverage', INT),
            Column('trade_fee', VARCHAR(255)),
            Column('trade_fee_in_quote', FLOAT),
            Column('exchange_trade_id', VARCHAR(255)),
            Column('position', VARCHAR(255)),
        )

    @property
    def orders_table(self):
        return Table(
            'orders', MetaData(),
            Column('client_order_id', VARCHAR(255)),
            Column('config_file_path', VARCHAR(255)),
            Column('strategy', VARCHAR(255)),
            Column('market', VARCHAR(255)),
            Column('symbol', VARCHAR(255)),
            Column('base_asset', VARCHAR(255)),
            Column('quote_asset', VARCHAR(255)),
            Column('creation_timestamp', INT),
            Column('order_type', VARCHAR(255)),
            Column('amount', FLOAT),
            Column('leverage', INT),
            Column('price', FLOAT),
            Column('last_status', VARCHAR(255)),
            Column('last_update_timestamp', INT),
            Column('exchange_order_id', VARCHAR(255)),
            Column('position', VARCHAR(255)),
        )

    @property
    def controllers_table(self):
        return Table(
            'controllers', MetaData(),
            Column('id', VARCHAR(255)),
            Column('controller_id', INT),
            Column('timestamp', FLOAT),
            Column('type', VARCHAR(255)),
            Column('config', String),
        )

    @property
    def tables(self):
        return [self.executors_table, self.trade_fill_table, self.orders_table, self.controllers_table]

    def create_tables(self):
        with self.engine.connect():
            for table in self.tables:
                table.create(self.engine)

    def insert_data(self, data):
        if "executors" in data:
            self.insert_executors(data["executors"])
        if "trade_fill" in data:
            self.insert_trade_fill(data["trade_fill"])
        if "orders" in data:
            self.insert_orders(data["orders"])
        if "controllers" in data:
            self.insert_controllers(data["controllers"])

    def insert_executors(self, executors):
        with self.engine.connect() as conn:
            for _, row in executors.iterrows():
                ins = self.executors_table.insert().values(
                    id=row["id"],
                    timestamp=row["timestamp"],
                    type=row["type"],
                    close_type=row["close_type"],
                    close_timestamp=row["close_timestamp"],
                    status=row["status"],
                    config=row["config"],
                    net_pnl_pct=row["net_pnl_pct"],
                    net_pnl_quote=row["net_pnl_quote"],
                    cum_fees_quote=row["cum_fees_quote"],
                    filled_amount_quote=row["filled_amount_quote"],
                    is_active=row["is_active"],
                    is_trading=row["is_trading"],
                    custom_info=row["custom_info"],
                    controller_id=row["controller_id"])
                conn.execute(ins)
                conn.commit()

    def insert_trade_fill(self, trade_fill):
        with self.engine.connect() as conn:
            for _, row in trade_fill.iterrows():
                ins = insert(self.trade_fill_table).values(
                    config_file_path=row["config_file_path"],
                    strategy=row["strategy"],
                    market=row["market"],
                    symbol=row["symbol"],
                    base_asset=row["base_asset"],
                    quote_asset=row["quote_asset"],
                    timestamp=row["timestamp"],
                    order_id=row["order_id"],
                    trade_type=row["trade_type"],
                    order_type=row["order_type"],
                    price=row["price"],
                    amount=row["amount"],
                    leverage=row["leverage"],
                    trade_fee=row["trade_fee"],
                    trade_fee_in_quote=row["trade_fee_in_quote"],
                    exchange_trade_id=row["exchange_trade_id"],
                    position=row["position"],
                )
                conn.execute(ins)
                conn.commit()

    def insert_orders(self, orders):
        with self.engine.connect() as conn:
            for _, row in orders.iterrows():
                ins = insert(self.orders_table).values(
                    client_order_id=row["id"],
                    config_file_path=row["config_file_path"],
                    strategy=row["strategy"],
                    market=row["market"],
                    symbol=row["symbol"],
                    base_asset=row["base_asset"],
                    quote_asset=row["quote_asset"],
                    creation_timestamp=row["creation_timestamp"],
                    order_type=row["order_type"],
                    amount=row["amount"],
                    leverage=row["leverage"],
                    price=row["price"],
                    last_status=row["last_status"],
                    last_update_timestamp=row["last_update_timestamp"],
                    exchange_order_id=row["exchange_order_id"],
                    position=row["position"],
                )
                conn.execute(ins)
                conn.commit()

    def insert_controllers(self, controllers):
        with self.engine.connect() as conn:
            for _, row in controllers.iterrows():
                ins = insert(self.controllers_table).values(
                    id=row["id"],
                    controller_id=row["controller_id"],
                    timestamp=row["timestamp"],
                    type=row["type"],
                    config=row["config"],
                )
                conn.execute(ins)
                conn.commit()

    def load_executors(self):
        with self.session_maker() as session:
            query = "SELECT * FROM executors"
            executors = pd.read_sql_query(text(query), session.connection())
        return executors

    def load_trade_fill(self):
        with self.session_maker() as session:
            query = "SELECT * FROM trades"
            trade_fill = pd.read_sql_query(text(query), session.connection())
            return trade_fill

    def load_orders(self):
        with self.session_maker() as session:
            query = "SELECT * FROM orders"
            orders = pd.read_sql_query(text(query), session.connection())
            return orders

    def load_controllers(self):
        with self.session_maker() as session:
            query = "SELECT * FROM controllers"
            controllers = pd.read_sql_query(text(query), session.connection())
            return controllers


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
        executors["close_price"] = executors["custom_info"].apply(lambda x: x.get("close_price", x["current_position_average_price"]))
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