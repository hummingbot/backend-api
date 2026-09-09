from decimal import Decimal
from typing import List, Optional, Tuple

import numpy as np
from sklearn.linear_model import LinearRegression
from statsmodels.tsa.stattools import adfuller

from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, PriceType, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair, PositionSummary
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction


class StatArbConfig(ControllerConfigBase):
    """
    Statistical arbitrage controller — v2.

    Simplified configuration: a SINGLE connector (exchange + market type) and two trading pairs.
    The strategy trades the spread between two assets on the same exchange.

    YAML fields
    -----------
    controller_name         : stat_arb_v2
    controller_type         : generic
    total_amount_quote      : total capital in quote currency (e.g. 1000)

    connector_name          : exchange connector (e.g. "binance_perpetual")
    trading_pair_dominant   : first asset (e.g. "SOL-USDT")
    trading_pair_hedge      : second asset (e.g. "XRP-USDT")

    interval                : candle interval (1m, 3m, 5m …)
    lookback_period         : number of candles used for regression and z-score (e.g. 300)
    entry_threshold         : z-score level that triggers a signal (e.g. 2.0)

    take_profit             : per-executor TP as fraction of entry (e.g. 0.0008 = 0.08%)
    tp_global               : total pair PnL% to close all positions (e.g. 0.01 = 1%)
    sl_global               : total pair PnL% loss to close all positions (e.g. 0.05 = 5%)

    min_amount_quote        : notional per order on dominant leg in quote (e.g. 10)
    quoter_spread           : offset from mid for limit entries (e.g. 0.0001)
    quoter_cooldown         : seconds before a filled executor is released (e.g. 30)
    quoter_refresh          : seconds before an unfilled order is repriced (e.g. 10)
    max_orders_placed_per_side : max pending (unfilled) orders per leg (e.g. 2)
    max_orders_filled_per_side : max active (filled) orders per leg (e.g. 2)
    max_position_deviation  : imbalance threshold that blocks one leg (e.g. 0.1 = 10%)

    leverage                : leverage for perp connectors (e.g. 20)
    position_mode           : HEDGE or ONEWAY

    # v2 statistical quality filters
    min_r_squared           : minimum OLS R² to allow signals (e.g. 0.70)
    adf_pvalue_threshold    : max ADF p-value to allow signals (e.g. 0.05)
    use_dynamic_hedge_ratio : true = size hedge leg via OLS beta, false = use pos_hedge_ratio
    pos_hedge_ratio         : fallback hedge ratio when use_dynamic_hedge_ratio is false (e.g. 1.0)
    max_dynamic_hedge_ratio : cap on dynamic ratio to avoid extreme sizing (e.g. 3.0)
    min_dynamic_hedge_ratio : floor on dynamic ratio (e.g. 0.2)
    """
    controller_type: str = "generic"
    controller_name: str = "stat_arb_v2"

    # Unified connector (single exchange)
    connector_name: str   # e.g. "binance_perpetual"

    # Two trading pairs
    trading_pair_dominant: str   # e.g. "SOL-USDT"
    trading_pair_hedge: str      # e.g. "XRP-USDT"

    # Candle settings
    interval: str = "1m"
    lookback_period: int = 300

    # Signal
    entry_threshold: Decimal = Decimal("2.0")

    # Exit
    take_profit: Decimal = Decimal("0.0008")
    tp_global: Decimal = Decimal("0.01")
    sl_global: Decimal = Decimal("0.05")

    # Order sizing / quoting
    min_amount_quote: Decimal = Decimal("10")
    quoter_spread: Decimal = Decimal("0.0001")
    quoter_cooldown: int = 30
    quoter_refresh: int = 10
    max_orders_placed_per_side: int = 2
    max_orders_filled_per_side: int = 2
    max_position_deviation: Decimal = Decimal("0.1")

    # Position
    leverage: int = 20
    position_mode: PositionMode = PositionMode.HEDGE

    # v2 — statistical quality filters
    min_r_squared: float = 0.70
    adf_pvalue_threshold: float = 0.05
    use_dynamic_hedge_ratio: bool = True
    pos_hedge_ratio: Decimal = Decimal("1.0")
    max_dynamic_hedge_ratio: Decimal = Decimal("3.0")
    min_dynamic_hedge_ratio: Decimal = Decimal("0.2")

    # --- Derived properties for internal use (compatible with original code) ---
    @property
    def connector_pair_dominant(self) -> ConnectorPair:
        return ConnectorPair(connector_name=self.connector_name, trading_pair=self.trading_pair_dominant)

    @property
    def connector_pair_hedge(self) -> ConnectorPair:
        return ConnectorPair(connector_name=self.connector_name, trading_pair=self.trading_pair_hedge)

    @property
    def triple_barrier_config(self) -> TripleBarrierConfig:
        return TripleBarrierConfig(
            take_profit=self.take_profit,
            open_order_type=OrderType.LIMIT_MAKER,
            take_profit_order_type=OrderType.LIMIT_MAKER,
        )

    def update_markets(self, markets: dict) -> dict:
        """Add both trading pairs under the same connector."""
        if self.connector_name not in markets:
            markets[self.connector_name] = set()
        markets[self.connector_name].add(self.trading_pair_dominant)
        markets[self.connector_name].add(self.trading_pair_hedge)
        return markets


class StatArb(ControllerBase):
    """
    Statistical arbitrage controller — v2 (unified connector version).
    """

    def __init__(self, config: StatArbConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config

        # Theoretical quotes are recomputed dynamically each tick when use_dynamic_hedge_ratio=True.
        self.theoretical_dominant_quote = self.config.total_amount_quote * (
            Decimal("1") / (Decimal("1") + self.config.pos_hedge_ratio)
        )
        self.theoretical_hedge_quote = self.config.total_amount_quote * (
            self.config.pos_hedge_ratio / (Decimal("1") + self.config.pos_hedge_ratio)
        )

        self.processed_data = {
            "dominant_price": None,
            "hedge_price": None,
            "spread": None,
            "z_score": None,
            "hedge_ratio": None,
            "position_dominant": Decimal("0"),
            "position_hedge": Decimal("0"),
            "active_orders_dominant": [],
            "active_orders_hedge": [],
            "pair_pnl": Decimal("0"),
            "signal": 0,
            "r_squared": None,
            "adf_pvalue": None,
            "half_life": None,
            "dynamic_hedge_ratio": self.config.pos_hedge_ratio,
            "signal_blocked_reason": "initializing",
        }

        self.max_records = self.config.lookback_period + 20

        # Configure connector if perpetual
        if "_perpetual" in self.config.connector_name:
            connector = self.market_data_provider.get_connector(self.config.connector_name)
            connector.set_position_mode(self.config.position_mode)
            connector.set_leverage(self.config.trading_pair_dominant, self.config.leverage)
            connector.set_leverage(self.config.trading_pair_hedge, self.config.leverage)

    # -------------------------------------------------------------------------
    # MAIN LOOP (identical to previous v2, but uses config.connector_pair_* properties)
    # -------------------------------------------------------------------------

    def determine_executor_actions(self) -> List[ExecutorAction]:
        actions: List[ExecutorAction] = []
        if self.processed_data["pair_pnl_pct"] > self.config.tp_global or \
                self.processed_data["pair_pnl_pct"] < -self.config.sl_global:
            for position in self.positions_held:
                actions.extend(self.get_executors_to_reduce_position(position))
            return actions
        elif self.processed_data["signal"] != 0:
            actions.extend(self.get_executors_to_quote())
            actions.extend(self.get_executors_to_reduce_position_on_opposite_signal())

        actions.extend(self.get_executors_to_keep_position())
        actions.extend(self.get_executors_to_refresh())
        return actions

    # -------------------------------------------------------------------------
    # SIGNAL / DATA UPDATE
    # -------------------------------------------------------------------------

    async def update_processed_data(self):
        result = self.get_spread_and_z_score()
        if result is None:
            self.processed_data["signal"] = 0
            self.processed_data["signal_blocked_reason"] = "insufficient candle data"
            return

        spread, z_score, beta, r_squared, adf_pvalue, half_life = result

        signal_blocked_reason = None

        if r_squared < self.config.min_r_squared:
            signal = 0
            signal_blocked_reason = f"R²={r_squared:.3f} < min={self.config.min_r_squared}"
            self.logger().warning(f"[StatArb] Signal suppressed: {signal_blocked_reason}")

        elif adf_pvalue > self.config.adf_pvalue_threshold:
            signal = 0
            signal_blocked_reason = f"ADF p-value={adf_pvalue:.3f} > threshold={self.config.adf_pvalue_threshold} (spread not stationary)"
            self.logger().warning(f"[StatArb] Signal suppressed: {signal_blocked_reason}")

        else:
            entry_threshold = float(self.config.entry_threshold)
            if z_score > entry_threshold:
                signal = 1
                dominant_side, hedge_side = TradeType.BUY, TradeType.SELL
            elif z_score < -entry_threshold:
                signal = -1
                dominant_side, hedge_side = TradeType.SELL, TradeType.BUY
            else:
                signal = 0
                dominant_side, hedge_side = None, None

        # Dynamic hedge ratio
        if self.config.use_dynamic_hedge_ratio and beta > 0:
            raw_ratio = Decimal(str(round(1.0 / beta, 6)))
            effective_hedge_ratio = max(
                self.config.min_dynamic_hedge_ratio,
                min(self.config.max_dynamic_hedge_ratio, raw_ratio)
            )
        else:
            effective_hedge_ratio = self.config.pos_hedge_ratio

        theoretical_dominant_quote = self.config.total_amount_quote * (
            Decimal("1") / (Decimal("1") + effective_hedge_ratio)
        )
        theoretical_hedge_quote = self.config.total_amount_quote * (
            effective_hedge_ratio / (Decimal("1") + effective_hedge_ratio)
        )
        self.theoretical_dominant_quote = theoretical_dominant_quote
        self.theoretical_hedge_quote = theoretical_hedge_quote

        dominant_price, hedge_price = self.get_pairs_prices()

        if signal != 0:
            dominant_side_for_lookup = dominant_side
            hedge_side_for_lookup = hedge_side
        else:
            dominant_side_for_lookup = None
            hedge_side_for_lookup = None

        positions_dominant = next(
            (p for p in self.positions_held
             if p.connector_name == self.config.connector_name
             and p.trading_pair == self.config.trading_pair_dominant
             and (p.side == dominant_side_for_lookup or dominant_side_for_lookup is None)),
            None
        )
        positions_hedge = next(
            (p for p in self.positions_held
             if p.connector_name == self.config.connector_name
             and p.trading_pair == self.config.trading_pair_hedge
             and (p.side == hedge_side_for_lookup or hedge_side_for_lookup is None)),
            None
        )

        position_dominant_quote = positions_dominant.amount_quote if positions_dominant else Decimal("0")
        position_hedge_quote = positions_hedge.amount_quote if positions_hedge else Decimal("0")
        position_dominant_pnl_quote = positions_dominant.global_pnl_quote if positions_dominant else Decimal("0")
        position_hedge_pnl_quote = positions_hedge.global_pnl_quote if positions_hedge else Decimal("0")
        pair_pnl_pct = (
            (position_dominant_pnl_quote + position_hedge_pnl_quote)
            / (position_dominant_quote + position_hedge_quote)
            if (position_dominant_quote + position_hedge_quote) != 0
            else Decimal("0")
        )

        executors_dominant_placed, executors_dominant_filled = self.get_executors_dominant()
        executors_hedge_placed, executors_hedge_filled = self.get_executors_hedge()

        min_price_dominant = Decimal(str(min(e.config.entry_price for e in executors_dominant_placed))) if executors_dominant_placed else None
        max_price_dominant = Decimal(str(max(e.config.entry_price for e in executors_dominant_placed))) if executors_dominant_placed else None
        min_price_hedge = Decimal(str(min(e.config.entry_price for e in executors_hedge_placed))) if executors_hedge_placed else None
        max_price_hedge = Decimal(str(max(e.config.entry_price for e in executors_hedge_placed))) if executors_hedge_placed else None

        active_amount_dominant = Decimal(str(sum(e.filled_amount_quote for e in executors_dominant_filled)))
        active_amount_hedge = Decimal(str(sum(e.filled_amount_quote for e in executors_hedge_filled)))

        dominant_gap = theoretical_dominant_quote - position_dominant_quote - active_amount_dominant
        hedge_gap = theoretical_hedge_quote - position_hedge_quote - active_amount_hedge
        imbalance = position_dominant_quote - position_hedge_quote
        imbalance_scaled = position_dominant_quote - position_hedge_quote * effective_hedge_ratio
        imbalance_scaled_pct = (
            imbalance_scaled / position_dominant_quote
            if position_dominant_quote != Decimal("0")
            else Decimal("0")
        )
        filter_connector_pair = None
        if imbalance_scaled_pct > self.config.max_position_deviation:
            filter_connector_pair = self.config.connector_pair_dominant
        elif imbalance_scaled_pct < -self.config.max_position_deviation:
            filter_connector_pair = self.config.connector_pair_hedge

        self.processed_data.update({
            "dominant_price": Decimal(str(dominant_price)),
            "hedge_price": Decimal(str(hedge_price)),
            "spread": Decimal(str(spread)),
            "z_score": Decimal(str(z_score)),
            "dominant_gap": Decimal(str(dominant_gap)),
            "hedge_gap": Decimal(str(hedge_gap)),
            "position_dominant_quote": position_dominant_quote,
            "position_hedge_quote": position_hedge_quote,
            "active_amount_dominant": active_amount_dominant,
            "active_amount_hedge": active_amount_hedge,
            "signal": signal,
            "imbalance": Decimal(str(imbalance)),
            "imbalance_scaled_pct": Decimal(str(imbalance_scaled_pct)),
            "filter_connector_pair": filter_connector_pair,
            "min_price_dominant": min_price_dominant if min_price_dominant is not None else Decimal(str(dominant_price)),
            "max_price_dominant": max_price_dominant if max_price_dominant is not None else Decimal(str(dominant_price)),
            "min_price_hedge": min_price_hedge if min_price_hedge is not None else Decimal(str(hedge_price)),
            "max_price_hedge": max_price_hedge if max_price_hedge is not None else Decimal(str(hedge_price)),
            "executors_dominant_filled": executors_dominant_filled,
            "executors_hedge_filled": executors_hedge_filled,
            "executors_dominant_placed": executors_dominant_placed,
            "executors_hedge_placed": executors_hedge_placed,
            "pair_pnl_pct": pair_pnl_pct,
            "alpha": self.processed_data.get("alpha", 0),
            "beta": beta,
            "r_squared": r_squared,
            "adf_pvalue": adf_pvalue,
            "half_life": half_life,
            "dynamic_hedge_ratio": effective_hedge_ratio,
            "theoretical_dominant_quote": theoretical_dominant_quote,
            "theoretical_hedge_quote": theoretical_hedge_quote,
            "signal_blocked_reason": signal_blocked_reason,
        })

    # -------------------------------------------------------------------------
    # STATISTICAL CORE (unchanged from v2)
    # -------------------------------------------------------------------------

    def get_spread_and_z_score(self) -> Optional[Tuple]:
        dominant_df = self.market_data_provider.get_candles_df(
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair_dominant,
            interval=self.config.interval,
            max_records=self.max_records
        )
        hedge_df = self.market_data_provider.get_candles_df(
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair_hedge,
            interval=self.config.interval,
            max_records=self.max_records
        )

        if dominant_df.empty or hedge_df.empty:
            self.logger().warning("[StatArb] Empty candle data")
            return None

        dominant_prices = dominant_df['close'].values
        hedge_prices = hedge_df['close'].values
        min_length = min(len(dominant_prices), len(hedge_prices))

        if min_length < self.config.lookback_period:
            self.logger().warning(f"[StatArb] Not enough data: need {self.config.lookback_period}, have {min_length}")
            return None

        dominant_prices = np.array(dominant_prices[-self.config.lookback_period:], dtype=float)
        hedge_prices = np.array(hedge_prices[-self.config.lookback_period:], dtype=float)

        dominant_pct = np.diff(dominant_prices) / dominant_prices[:-1]
        hedge_pct = np.diff(hedge_prices) / hedge_prices[:-1]
        dominant_cum = np.cumprod(dominant_pct + 1)
        hedge_cum = np.cumprod(hedge_pct + 1)
        dominant_cum = dominant_cum / dominant_cum[0]
        hedge_cum = hedge_cum / hedge_cum[0]

        reg = LinearRegression().fit(dominant_cum.reshape(-1, 1), hedge_cum)
        alpha = reg.intercept_
        beta = reg.coef_[0]
        r_squared = reg.score(dominant_cum.reshape(-1, 1), hedge_cum)

        y_pred = alpha + beta * dominant_cum
        spread_pct = (hedge_cum - y_pred) / y_pred * 100

        mean_spread = np.mean(spread_pct)
        std_spread = np.std(spread_pct)
        if std_spread == 0:
            self.logger().warning("[StatArb] Spread std is zero")
            return None
        current_spread = spread_pct[-1]
        current_z_score = (current_spread - mean_spread) / std_spread

        try:
            adf_result = adfuller(spread_pct, maxlag=1, autolag=None)
            adf_pvalue = float(adf_result[1])
        except Exception as e:
            self.logger().warning(f"[StatArb] ADF test failed: {e}")
            adf_pvalue = 1.0

        try:
            spread_lag = spread_pct[:-1]
            delta_spread = np.diff(spread_pct)
            ou_reg = LinearRegression().fit(spread_lag.reshape(-1, 1), delta_spread)
            lambda_ou = ou_reg.coef_[0]
            half_life = float(-np.log(2) / lambda_ou) if lambda_ou < 0 else None
        except Exception:
            half_life = None

        self.processed_data["alpha"] = alpha
        return current_spread, current_z_score, beta, r_squared, adf_pvalue, half_life

    # -------------------------------------------------------------------------
    # EXECUTION HELPERS (unchanged)
    # -------------------------------------------------------------------------

    def get_executors_to_reduce_position_on_opposite_signal(self) -> List[ExecutorAction]:
        if self.processed_data["signal"] == 1:
            dominant_side, hedge_side = TradeType.SELL, TradeType.BUY
        elif self.processed_data["signal"] == -1:
            dominant_side, hedge_side = TradeType.BUY, TradeType.SELL
        else:
            return []
        dominant_to_stop = self.filter_executors(
            self.executors_info,
            filter_func=lambda e:
                e.connector_name == self.config.connector_name
                and e.trading_pair == self.config.trading_pair_dominant
                and e.side == dominant_side
        )
        hedge_to_stop = self.filter_executors(
            self.executors_info,
            filter_func=lambda e:
                e.connector_name == self.config.connector_name
                and e.trading_pair == self.config.trading_pair_hedge
                and e.side == hedge_side
        )
        stop_actions = [
            StopExecutorAction(controller_id=self.config.id, executor_id=e.id, keep_position=False)
            for e in dominant_to_stop + hedge_to_stop
        ]
        reduce_actions: List[ExecutorAction] = []
        for position in self.positions_held:
            if (position.connector_name == self.config.connector_name
                    and position.trading_pair == self.config.trading_pair_dominant
                    and position.side == dominant_side):
                reduce_actions.extend(self.get_executors_to_reduce_position(position))
            elif (position.connector_name == self.config.connector_name
                    and position.trading_pair == self.config.trading_pair_hedge
                    and position.side == hedge_side):
                reduce_actions.extend(self.get_executors_to_reduce_position(position))
        return stop_actions + reduce_actions

    def get_executors_to_keep_position(self) -> List[ExecutorAction]:
        stop_actions: List[ExecutorAction] = []
        for executor in (self.processed_data["executors_dominant_filled"]
                         + self.processed_data["executors_hedge_filled"]):
            if self.market_data_provider.time() - executor.timestamp >= self.config.quoter_cooldown:
                stop_actions.append(
                    StopExecutorAction(controller_id=self.config.id, executor_id=executor.id, keep_position=True)
                )
        return stop_actions

    def get_executors_to_refresh(self) -> List[ExecutorAction]:
        refresh_actions: List[ExecutorAction] = []
        for executor in (self.processed_data["executors_dominant_placed"]
                         + self.processed_data["executors_hedge_placed"]):
            if self.market_data_provider.time() - executor.timestamp >= self.config.quoter_refresh:
                refresh_actions.append(
                    StopExecutorAction(controller_id=self.config.id, executor_id=executor.id, keep_position=False)
                )
        return refresh_actions

    def get_executors_to_quote(self) -> List[ExecutorAction]:
        actions: List[ExecutorAction] = []
        trade_type_dominant = TradeType.BUY if self.processed_data["signal"] == 1 else TradeType.SELL
        trade_type_hedge = TradeType.SELL if self.processed_data["signal"] == 1 else TradeType.BUY

        dynamic_hedge_ratio = self.processed_data["dynamic_hedge_ratio"]
        hedge_amount_quote = self.config.min_amount_quote * dynamic_hedge_ratio

        # Dominant leg
        if (self.processed_data["dominant_gap"] > Decimal("0")
                and self.processed_data["filter_connector_pair"] != self.config.connector_pair_dominant
                and len(self.processed_data["executors_dominant_placed"]) < self.config.max_orders_placed_per_side
                and len(self.processed_data["executors_dominant_filled"]) < self.config.max_orders_filled_per_side):
            price = (
                self.processed_data["min_price_dominant"] * (1 - self.config.quoter_spread)
                if trade_type_dominant == TradeType.BUY
                else self.processed_data["max_price_dominant"] * (1 + self.config.quoter_spread)
            )
            actions.append(CreateExecutorAction(
                controller_id=self.config.id,
                executor_config=PositionExecutorConfig(
                    timestamp=self.market_data_provider.time(),
                    connector_name=self.config.connector_name,
                    trading_pair=self.config.trading_pair_dominant,
                    side=trade_type_dominant,
                    entry_price=price,
                    amount=self.config.min_amount_quote / self.processed_data["dominant_price"],
                    triple_barrier_config=self.config.triple_barrier_config,
                    leverage=self.config.leverage,
                )
            ))

        # Hedge leg
        if (self.processed_data["hedge_gap"] > Decimal("0")
                and self.processed_data["filter_connector_pair"] != self.config.connector_pair_hedge
                and len(self.processed_data["executors_hedge_placed"]) < self.config.max_orders_placed_per_side
                and len(self.processed_data["executors_hedge_filled"]) < self.config.max_orders_filled_per_side):
            price = (
                self.processed_data["min_price_hedge"] * (1 - self.config.quoter_spread)
                if trade_type_hedge == TradeType.BUY
                else self.processed_data["max_price_hedge"] * (1 + self.config.quoter_spread)
            )
            actions.append(CreateExecutorAction(
                controller_id=self.config.id,
                executor_config=PositionExecutorConfig(
                    timestamp=self.market_data_provider.time(),
                    connector_name=self.config.connector_name,
                    trading_pair=self.config.trading_pair_hedge,
                    side=trade_type_hedge,
                    entry_price=price,
                    amount=hedge_amount_quote / self.processed_data["hedge_price"],
                    triple_barrier_config=self.config.triple_barrier_config,
                    leverage=self.config.leverage,
                )
            ))
        return actions

    def get_executors_to_reduce_position(self, position: PositionSummary) -> List[ExecutorAction]:
        if position.amount > Decimal("0"):
            config = OrderExecutorConfig(
                timestamp=self.market_data_provider.time(),
                connector_name=position.connector_name,
                trading_pair=position.trading_pair,
                side=TradeType.BUY if position.side == TradeType.SELL else TradeType.SELL,
                amount=position.amount,
                position_action=PositionAction.CLOSE,
                execution_strategy=ExecutionStrategy.MARKET,
                leverage=self.config.leverage,
            )
            return [CreateExecutorAction(controller_id=self.config.id, executor_config=config)]
        return []

    # -------------------------------------------------------------------------
    # HELPERS
    # -------------------------------------------------------------------------

    def get_pairs_prices(self):
        dominant_price = self.market_data_provider.get_price_by_type(
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair_dominant,
            price_type=PriceType.MidPrice
        )
        hedge_price = self.market_data_provider.get_price_by_type(
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair_hedge,
            price_type=PriceType.MidPrice
        )
        return dominant_price, hedge_price

    def get_executors_dominant(self):
        placed = self.filter_executors(
            self.executors_info,
            filter_func=lambda e:
                e.connector_name == self.config.connector_name
                and e.trading_pair == self.config.trading_pair_dominant
                and e.is_active and not e.is_trading and e.type == "position_executor"
        )
        filled = self.filter_executors(
            self.executors_info,
            filter_func=lambda e:
                e.connector_name == self.config.connector_name
                and e.trading_pair == self.config.trading_pair_dominant
                and e.is_active and e.is_trading and e.type == "position_executor"
        )
        return placed, filled

    def get_executors_hedge(self):
        placed = self.filter_executors(
            self.executors_info,
            filter_func=lambda e:
                e.connector_name == self.config.connector_name
                and e.trading_pair == self.config.trading_pair_hedge
                and e.is_active and not e.is_trading and e.type == "position_executor"
        )
        filled = self.filter_executors(
            self.executors_info,
            filter_func=lambda e:
                e.connector_name == self.config.connector_name
                and e.trading_pair == self.config.trading_pair_hedge
                and e.is_active and e.is_trading and e.type == "position_executor"
        )
        return placed, filled

    # -------------------------------------------------------------------------
    # STATUS
    # -------------------------------------------------------------------------

    def to_format_status(self) -> List[str]:
        half_life = self.processed_data.get("half_life")
        half_life_str = f"{half_life:.1f} candles" if half_life is not None else "N/A (not mean-reverting)"
        blocked = self.processed_data.get("signal_blocked_reason")
        blocked_str = f"  ⚠️  SIGNAL BLOCKED: {blocked}" if blocked else "  ✅ Signal active"

        return [f"""
Connector  : {self.config.connector_name}
Dominant   : {self.config.trading_pair_dominant}
Hedge      : {self.config.trading_pair_hedge}
Timeframe  : {self.config.interval} | Lookback: {self.config.lookback_period} | Entry threshold: {self.config.entry_threshold}

── Statistical quality ──────────────────────────────────────────────
R²            : {self.processed_data.get('r_squared', 0):.4f}  (min: {self.config.min_r_squared})
ADF p-value   : {self.processed_data.get('adf_pvalue', 1):.4f}  (max: {self.config.adf_pvalue_threshold})
Half-life     : {half_life_str}
Alpha / Beta  : {self.processed_data.get('alpha', 0):.4f} / {self.processed_data.get('beta', 0):.4f}
{blocked_str}

── Signal ───────────────────────────────────────────────────────────
Signal        : {self.processed_data['signal']}  | Z-Score: {self.processed_data['z_score']:.4f} | Spread: {self.processed_data['spread']:.4f}

── Positions ────────────────────────────────────────────────────────
Dynamic ratio : {self.processed_data['dynamic_hedge_ratio']:.4f}  (use_dynamic={self.config.use_dynamic_hedge_ratio})
Theoretical   : dominant={self.processed_data.get('theoretical_dominant_quote', 0):.2f} | hedge={self.processed_data.get('theoretical_hedge_quote', 0):.2f}
Actual        : dominant={self.processed_data['position_dominant_quote']:.2f} | hedge={self.processed_data['position_hedge_quote']:.2f}
Imbalance     : {self.processed_data['imbalance']:.2f} | Imbalance scaled: {self.processed_data['imbalance_scaled_pct']:.2f}%

── Executors ────────────────────────────────────────────────────────
Placed  : dominant={len(self.processed_data['executors_dominant_placed'])} | hedge={len(self.processed_data['executors_hedge_placed'])}
Filled  : dominant={len(self.processed_data['executors_dominant_filled'])} | hedge={len(self.processed_data['executors_hedge_filled'])}

Pair PnL: {self.processed_data['pair_pnl_pct'] * 100:.3f}%
"""]

    def get_candles_config(self) -> List[CandlesConfig]:
        return [
            CandlesConfig(
                connector=self.config.connector_name,
                trading_pair=self.config.trading_pair_dominant,
                interval=self.config.interval,
                max_records=self.max_records
            ),
            CandlesConfig(
                connector=self.config.connector_name,
                trading_pair=self.config.trading_pair_hedge,
                interval=self.config.interval,
                max_records=self.max_records
            )
        ]