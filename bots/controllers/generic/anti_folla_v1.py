from decimal import Decimal
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from pydantic import Field

from hummingbot.core.data_type.common import OrderType, PositionMode, PriceType, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.position_executor.data_types import (
    PositionExecutorConfig,
    TripleBarrierConfig,
)
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction


class AntiFollaV1Config(ControllerConfigBase):
    """
    Anti-Folla V1 Controller - Crowd-contrarian directional trading.

    This controller replicates the signal logic from Condor's Anti-Folla V1
    analysis, allowing the strategy to run natively in Hummingbot.
    """
    controller_type: str = "directional_trading"
    controller_name: str = "anti_folla_v1"

    # --- Exchange and pair ---
    connector_name: str = Field(
        default="binance_perpetual",
        json_schema_extra={"prompt": "Enter the exchange connector name: "},
    )
    trading_pair: str = Field(
        default="SOL-USDT",
        json_schema_extra={"prompt": "Enter the trading pair: "},
    )
    leverage: int = Field(default=1, json_schema_extra={"prompt": "Leverage: "})
    position_mode: PositionMode = Field(default=PositionMode.HEDGE)

    # --- Capital and risk ---
    total_amount_quote: Decimal = Field(
        default=Decimal("1000"), json_schema_extra={"prompt": "Total amount in quote currency: "}
    )
    max_executors_per_side: int = Field(default=1)
    cooldown_time: int = Field(default=60)
    stop_loss: Decimal = Field(default=Decimal("0.05"))
    take_profit: Decimal = Field(default=Decimal("0.03"))
    trailing_stop: Optional[Dict[str, Decimal]] = Field(
        default={"activation_price": Decimal("0.015"), "trailing_delta": Decimal("0.005")}
    )

    # --- Candles configuration ---
    candles_connector: Optional[str] = Field(default=None)
    candles_trading_pair: Optional[str] = Field(default=None)
    interval: str = Field(default="3m")

    # --- Anti-Folla parameters ---
    vwap_period: int = Field(default=20)
    donchian_period: int = Field(default=20)
    obv_divergence_lookback: int = Field(default=10)
    volume_spike_threshold: float = Field(default=2.5)

    # --- Order book imbalance (OBI) ---
    enable_order_book_imbalance: bool = Field(default=True)
    obi_depth_percentage: float = Field(default=0.02)
    obi_buy_threshold: float = Field(default=1.5)
    obi_sell_threshold: float = Field(default=0.67)

    # --- Score thresholds and weights ---
    score_buy_threshold: float = Field(default=50.0)
    score_sell_threshold: float = Field(default=-50.0)

    weight_vwap: float = Field(default=15)
    weight_donchian: float = Field(default=10)
    weight_obv: float = Field(default=15)
    weight_obi: float = Field(default=20)
    weight_volume_spike: float = Field(default=10)
    weight_trade_flow: float = Field(default=15)
    weight_funding: float = Field(default=15)

    # --- Perpetual flag ---
    is_perpetual: bool = Field(default=False)

    @property
    def triple_barrier_config(self) -> TripleBarrierConfig:
        """Triple barrier configuration for position executors."""
        trailing_stop = None
        if self.trailing_stop:
            trailing_stop = {
                "activation_price": float(self.trailing_stop.get("activation_price", 0)),
                "trailing_delta": float(self.trailing_stop.get("trailing_delta", 0)),
            }
        return TripleBarrierConfig(
            stop_loss=float(self.stop_loss),
            take_profit=float(self.take_profit),
            open_order_type=OrderType.LIMIT_MAKER,
            take_profit_order_type=OrderType.LIMIT_MAKER,
            trailing_stop=trailing_stop,
        )

    def update_markets(self, markets: Dict[str, Any]) -> Dict[str, Any]:
        """Register the trading pair for the connector."""
        if self.connector_name not in markets:
            markets[self.connector_name] = set()
        markets[self.connector_name].add(self.trading_pair)
        return markets


class AntiFollaV1(ControllerBase):
    """
    Anti-Folla V1 execution controller.

    Calculates the composite score using the same logic as the Condor controller
    and generates BUY/SELL signals based on configurable thresholds.
    """

    def __init__(self, config: AntiFollaV1Config, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self._last_timestamp = 0.0

        # Set up candles data provider
        candles_connector = self.config.candles_connector or self.config.connector_name
        candles_pair = self.config.candles_trading_pair or self.config.trading_pair

        self._candles = self.market_data_provider.get_candles_df(
            connector_name=candles_connector,
            trading_pair=candles_pair,
            interval=self.config.interval,
            max_records=500,
        )

        # Configure perpetual connector if needed
        if self.config.is_perpetual or "_perpetual" in self.config.connector_name:
            try:
                connector = self.market_data_provider.get_connector(self.config.connector_name)
                connector.set_position_mode(self.config.position_mode)
                connector.set_leverage(self.config.trading_pair, self.config.leverage)
            except Exception as e:
                self.logger().warning(f"Could not configure connector: {e}")

        # Processed data storage
        self.processed_data = {
            "signal": 0,
            "composite_score": 0.0,
            "funding_rate": None,
            "obi": None,
        }

    # ------------------------------------------------------------------
    # Signal calculation (mirrors Condor's analysis.py)
    # ------------------------------------------------------------------

    def _calculate_rolling_vwap(self, df: pd.DataFrame) -> pd.Series:
        """Calculate rolling VWAP."""
        pv = df["close"] * df["volume"]
        return pv.rolling(self.config.vwap_period).sum() / df["volume"].rolling(self.config.vwap_period).sum()

    def _calculate_donchian(self, df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        """Calculate Donchian channel with shift(1) to exclude current candle."""
        upper = df["high"].shift(1).rolling(self.config.donchian_period).max()
        lower = df["low"].shift(1).rolling(self.config.donchian_period).min()
        return upper, lower

    def _calculate_obv(self, df: pd.DataFrame) -> pd.Series:
        """Calculate On-Balance Volume."""
        direction = np.sign(df["close"].diff().fillna(0))
        return (direction * df["volume"]).cumsum()

    def _detect_obv_divergence(self, df: pd.DataFrame, obv: pd.Series) -> str:
        """Detect OBV divergence."""
        if len(df) < self.config.obv_divergence_lookback:
            return "none"
        price_trend = df["close"].diff(self.config.obv_divergence_lookback).iloc[-1]
        obv_trend = obv.diff(self.config.obv_divergence_lookback).iloc[-1]
        if price_trend < 0 and obv_trend > 0:
            return "bullish"
        if price_trend > 0 and obv_trend < 0:
            return "bearish"
        return "none"

    def _detect_volume_spike(self, df: pd.DataFrame) -> tuple[bool, float]:
        """Detect volume spike."""
        if len(df) < 22:
            return False, 1.0
        avg_vol = df["volume"].iloc[-21:-1].mean()
        if avg_vol == 0:
            return False, 1.0
        multiplier = df["volume"].iloc[-1] / avg_vol
        return multiplier >= self.config.volume_spike_threshold, float(multiplier)

    def _analyze_trade_flow(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Analyze buy/sell pressure from OHLCV."""
        if len(df) < 11:
            return {"whale_buying": False, "whale_selling": False, "buy_pressure": 0.5}

        recent = df.iloc[-10:]
        bull_vol = recent[recent["close"] > recent["open"]]["volume"].sum()
        bear_vol = recent[recent["close"] <= recent["open"]]["volume"].sum()
        total_vol = bull_vol + bear_vol
        buy_pressure = bull_vol / total_vol if total_vol > 0 else 0.5

        # Whale detection: last candle > 3x avg volume and large body
        avg_vol = recent["volume"].mean()
        avg_body = (recent["close"] - recent["open"]).abs().mean()
        last = df.iloc[-1]
        last_body = abs(last["close"] - last["open"])
        last_vol = last["volume"]

        whale_buying = (
            last_vol > avg_vol * 3.0
            and last["close"] > last["open"]
            and last_body > avg_body
        )
        whale_selling = (
            last_vol > avg_vol * 3.0
            and last["close"] < last["open"]
            and last_body > avg_body
        )

        return {
            "whale_buying": whale_buying,
            "whale_selling": whale_selling,
            "buy_pressure": buy_pressure,
        }

    async def _get_funding_rate(self) -> Optional[float]:
        """Get current funding rate for perpetual connectors."""
        if not self.config.is_perpetual and "_perpetual" not in self.config.connector_name:
            return None
        try:
            connector = self.market_data_provider.get_connector(self.config.connector_name)
            funding_info = await connector.get_funding_info(self.config.trading_pair)
            return float(funding_info.rate) * 100  # Convert to percentage
        except Exception as e:
            self.logger().debug(f"Could not fetch funding rate: {e}")
            return None

    async def _get_order_book_imbalance(self) -> Optional[float]:
        """Calculate Order Book Imbalance (OBI)."""
        if not self.config.enable_order_book_imbalance:
            return None
        try:
            connector = self.market_data_provider.get_connector(self.config.connector_name)
            order_book = connector.get_order_book(self.config.trading_pair)

            if not order_book:
                return None

            # Calculate OBI at specified depth
            depth = self.config.obi_depth_percentage
            best_bid = order_book.best_bid[0] if order_book.best_bid else None
            best_ask = order_book.best_ask[0] if order_book.best_ask else None

            if not best_bid or not best_ask:
                return None

            # Bid depth up to (1 + depth)% from best bid
            bid_cutoff = best_bid * (1 - depth)
            ask_cutoff = best_ask * (1 + depth)

            bid_volume = sum(price * amount for price, amount in order_book.bid_entries() if price >= bid_cutoff)
            ask_volume = sum(price * amount for price, amount in order_book.ask_entries() if price <= ask_cutoff)

            if ask_volume == 0:
                return 2.0 if bid_volume > 0 else 1.0

            obi = bid_volume / ask_volume
            return float(obi)
        except Exception as e:
            self.logger().debug(f"Could not calculate OBI: {e}")
            return None

    def _calculate_composite_score(self, signals: Dict[str, Any]) -> float:
        """Calculate weighted composite score."""
        score = 0.0
        total_weight = 0.0

        # VWAP
        if signals.get("vwap_above"):
            score += self.config.weight_vwap
            total_weight += self.config.weight_vwap
        elif signals.get("vwap_below"):
            score -= self.config.weight_vwap
            total_weight += self.config.weight_vwap

        # Donchian breakout
        if signals.get("donchian_breakout_up"):
            score += self.config.weight_donchian
            total_weight += self.config.weight_donchian
        elif signals.get("donchian_breakout_down"):
            score -= self.config.weight_donchian
            total_weight += self.config.weight_donchian

        # OBV divergence
        obv_div = signals.get("obv_divergence", "none")
        if obv_div == "bullish":
            score += self.config.weight_obv
            total_weight += self.config.weight_obv
        elif obv_div == "bearish":
            score -= self.config.weight_obv
            total_weight += self.config.weight_obv

        # OBI
        obi = signals.get("obi")
        if obi is not None:
            if obi >= self.config.obi_buy_threshold:
                score += self.config.weight_obi
                total_weight += self.config.weight_obi
            elif obi <= self.config.obi_sell_threshold:
                score -= self.config.weight_obi
                total_weight += self.config.weight_obi

        # Volume spike
        if signals.get("volume_spike"):
            price_trend = signals.get("price_trend", 0)
            if price_trend > 0:
                score += self.config.weight_volume_spike
            else:
                score -= self.config.weight_volume_spike
            total_weight += self.config.weight_volume_spike

        # Whale activity
        if signals.get("whale_buying"):
            score += self.config.weight_trade_flow
            total_weight += self.config.weight_trade_flow
        elif signals.get("whale_selling"):
            score -= self.config.weight_trade_flow
            total_weight += self.config.weight_trade_flow

        # Funding rate (contrarian)
        funding_rate = signals.get("funding_rate")
        if funding_rate is not None:
            if funding_rate > 0.05:  # Too many longs → contrarian short
                score -= self.config.weight_funding
                total_weight += self.config.weight_funding
            elif funding_rate < -0.05:  # Too many shorts → contrarian long
                score += self.config.weight_funding
                total_weight += self.config.weight_funding

        if total_weight > 0:
            score = (score / total_weight) * 100

        return score

    async def _get_current_signal(self) -> tuple[int, float]:
        """
        Calculate current composite score and generate signal.

        Returns:
            (signal, score) where signal is 1 (BUY), -1 (SELL), or 0 (NEUTRAL)
        """
        # Get fresh candles
        candles_connector = self.config.candles_connector or self.config.connector_name
        candles_pair = self.config.candles_trading_pair or self.config.trading_pair

        df = self.market_data_provider.get_candles_df(
            connector_name=candles_connector,
            trading_pair=candles_pair,
            interval=self.config.interval,
            max_records=200,
        )

        if df.empty or len(df) < 50:
            self.logger().warning("Insufficient candle data for signal calculation")
            return 0, 0.0

        # Calculate indicators
        vwap = self._calculate_rolling_vwap(df)
        donchian_upper, donchian_lower = self._calculate_donchian(df)
        obv = self._calculate_obv(df)

        current_price = df["close"].iloc[-1]
        current_vwap = vwap.iloc[-1]
        current_upper = donchian_upper.iloc[-1]
        current_lower = donchian_lower.iloc[-1]

        # Detect signals
        obv_divergence = self._detect_obv_divergence(df, obv)
        is_spike, _ = self._detect_volume_spike(df)
        trade_flow = self._analyze_trade_flow(df)

        # Price trend (20-candle)
        price_trend = (df["close"].iloc[-1] - df["close"].iloc[-21]) / df["close"].iloc[-21] if len(df) >= 21 else 0

        # Get OBI and funding rate
        obi = await self._get_order_book_imbalance()
        funding_rate = await self._get_funding_rate()

        signals = {
            "vwap_above": current_price > current_vwap,
            "vwap_below": current_price < current_vwap,
            "donchian_breakout_up": not pd.isna(current_upper) and current_price > current_upper,
            "donchian_breakout_down": not pd.isna(current_lower) and current_price < current_lower,
            "obv_divergence": obv_divergence,
            "obi": obi,
            "volume_spike": is_spike,
            "price_trend": price_trend,
            "funding_rate": funding_rate,
            **trade_flow,
        }

        score = self._calculate_composite_score(signals)

        if score >= self.config.score_buy_threshold:
            signal = 1
        elif score <= self.config.score_sell_threshold:
            signal = -1
        else:
            signal = 0

        return signal, score

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def _get_current_position_side(self) -> Optional[TradeType]:
        """Get the side of the current open position."""
        for position in self.positions_held:
            if position.amount > 0:
                return position.side
        return None

    def _create_position_executor(self, side: TradeType, price: Decimal) -> CreateExecutorAction:
        """Create a position executor for the given side."""
        amount = self.config.total_amount_quote / price

        executor_config = PositionExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=side,
            entry_price=price,
            amount=amount,
            triple_barrier_config=self.config.triple_barrier_config,
            leverage=self.config.leverage,
        )

        return CreateExecutorAction(
            controller_id=self.config.id,
            executor_config=executor_config,
        )

    # ------------------------------------------------------------------
    # Main execution
    # ------------------------------------------------------------------

    async def update_processed_data(self):
        """Update processed data with current signal and score."""
        signal, score = await self._get_current_signal()
        self.processed_data.update(
            {
                "signal": signal,
                "composite_score": score,
            }
        )

    def determine_executor_actions(self) -> List[ExecutorAction]:
        """Determine executor actions based on current signal."""
        actions: List[ExecutorAction] = []

        signal = self.processed_data.get("signal", 0)
        current_position_side = self._get_current_position_side()
        current_price = self.market_data_provider.get_price_by_type(
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            price_type=PriceType.MidPrice,
        )

        # Log current state
        self.logger().info(
            f"Signal: {signal:+d} | Score: {self.processed_data.get('composite_score', 0):.1f} | "
            f"Position: {current_position_side if current_position_side else 'NONE'} | "
            f"Price: {current_price:.6f}"
        )

        # No signal or score in neutral zone
        if signal == 0:
            return actions

        # Check if we already have a position in the same direction
        target_side = TradeType.BUY if signal == 1 else TradeType.SELL

        if current_position_side == target_side:
            # Already have position in correct direction
            return actions

        # Close existing position if opposite
        if current_position_side is not None and current_position_side != target_side:
            for position in self.positions_held:
                if position.amount > 0:
                    # Create a stop action to close the position
                    actions.append(
                        StopExecutorAction(
                            controller_id=self.config.id,
                            executor_id=position.executor_id,
                            keep_position=False,
                        )
                    )

        # Create new position if no position or after closing
        if current_position_side != target_side:
            actions.append(self._create_position_executor(target_side, Decimal(str(current_price))))

        return actions

    def to_format_status(self) -> List[str]:
        """Format status for display."""
        d = self.processed_data
        signal = d.get("signal", 0)
        signal_str = "🟢 BUY" if signal == 1 else ("🔴 SELL" if signal == -1 else "⚪ NEUTRAL")

        lines = [
            f"Anti-Folla V1 - {self.config.trading_pair}",
            f"  Signal: {signal_str}",
            f"  Score: {d.get('composite_score', 0):.1f}",
            f"  Thresholds: BUY ≥{self.config.score_buy_threshold:.0f} | SELL ≤{self.config.score_sell_threshold:.0f}",
            f"  Interval: {self.config.interval}",
            "",
            f"  Weights: VWAP={self.config.weight_vwap:.0f} Donchian={self.config.weight_donchian:.0f} "
            f"OBV={self.config.weight_obv:.0f} OBI={self.config.weight_obi:.0f} "
            f"Spike={self.config.weight_volume_spike:.0f} Flow={self.config.weight_trade_flow:.0f} "
            f"Funding={self.config.weight_funding:.0f}",
        ]

        if self.positions_held:
            lines.append("  Positions:")
            for pos in self.positions_held:
                lines.append(
                    f"    {pos.side.name} {pos.amount:.4f} @ {pos.average_entry:.6f} | "
                    f"PnL: {pos.global_pnl_pct:.2%} | Value: ${pos.amount_quote:.2f}"
                )

        return lines

    def get_candles_config(self) -> List[CandlesConfig]:
        """Return candles configuration for the data provider."""
        candles_connector = self.config.candles_connector or self.config.connector_name
        candles_pair = self.config.candles_trading_pair or self.config.trading_pair
        return [
            CandlesConfig(
                connector=candles_connector,
                trading_pair=candles_pair,
                interval=self.config.interval,
                max_records=500,
            )
        ]