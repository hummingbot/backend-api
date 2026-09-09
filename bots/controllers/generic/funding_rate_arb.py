from decimal import Decimal
from typing import Dict, List, Optional

from pydantic import Field

from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, PriceType, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair, PositionSummary
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction


def is_spot_connector(connector_name: str) -> bool:
    """Detect if connector is spot by checking name conventions."""
    name_lower = connector_name.lower()
    # Se contiene "_perpetual" o "perp" è perpetual
    if "_perpetual" in name_lower or "perp" in name_lower:
        return False
    # Altrimenti è spot (o margin, ma margin ha funding)
    # Per margin bisognerebbe distinguere, ma di default trattiamo come perp
    return "margin" not in name_lower


class FundingRateArbConfig(ControllerConfigBase):
    """
    Funding Rate Arbitrage — multi-exchange, any connector combination.

    Supports:
      • Perp ↔ Perp  : full delta neutral
      • Spot ↔ Perp  : cash-and-carry
    """
    controller_type: str = "generic"
    controller_name: str = "funding_rate_arb"

    connector_pair_a: ConnectorPair = ConnectorPair(
        connector_name="kucoin_perpetual",
        trading_pair="SOL-USDT"
    )
    connector_pair_b: ConnectorPair = ConnectorPair(
        connector_name="hyperliquid_perpetual",
        trading_pair="SOL-USDT"
    )

    # ========== RENDERE CONFIGURABILE VIA YAML ==========
    # Funding intervals in hours (se non specificato, usa default 8)
    funding_interval_a_hours: Optional[int] = Field(
        default=None,
        json_schema_extra={"prompt": "Funding interval for exchange A in hours (leave empty for auto-detect): ",
                           "prompt_on_new": True}
    )
    funding_interval_b_hours: Optional[int] = Field(
        default=None,
        json_schema_extra={"prompt": "Funding interval for exchange B in hours (leave empty for auto-detect): ",
                           "prompt_on_new": True}
    )
    # ====================================================

    entry_threshold: Decimal = Decimal("0.000025")
    exit_threshold: Decimal = Decimal("0.000005")

    total_amount_quote: Decimal = Decimal("100")
    leverage: int = 1
    position_mode: PositionMode = PositionMode.HEDGE

    sl_global: Decimal = Decimal("0.03")
    tp_global: Decimal = Decimal("0.05")

    funding_check_interval: int = 300
    executor_refresh_time: int = 60

    @property
    def position_amount_quote(self) -> Decimal:
        return self.total_amount_quote / Decimal("2")

    @property
    def mode(self) -> str:
        if is_spot_connector(self.connector_pair_a.connector_name) or \
           is_spot_connector(self.connector_pair_b.connector_name):
            return "spot+perp (cash-and-carry)"
        return "perp+perp (delta neutral)"

    def update_markets(self, markets: dict) -> dict:
        for cp in [self.connector_pair_a, self.connector_pair_b]:
            if cp.connector_name not in markets:
                markets[cp.connector_name] = set()
            markets[cp.connector_name].add(cp.trading_pair)
        return markets


class FundingRateArb(ControllerBase):
    """Funding Rate Arbitrage execution engine."""

    def __init__(self, config: FundingRateArbConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config

        # Cache per funding intervals (ottenuti dinamicamente dall'exchange)
        self._funding_interval_cache_a: Optional[int] = None
        self._funding_interval_cache_b: Optional[int] = None

        for cp in [self.config.connector_pair_a, self.config.connector_pair_b]:
            if not is_spot_connector(cp.connector_name):
                try:
                    connector = self.market_data_provider.get_connector(cp.connector_name)
                    connector.set_position_mode(self.config.position_mode)
                    connector.set_leverage(cp.trading_pair, self.config.leverage)
                except Exception as e:
                    self.logger().warning(f"Could not configure {cp.connector_name}: {e}")

        self.processed_data = {
            "funding_rate_a_raw": Decimal("0"),
            "funding_rate_b_raw": Decimal("0"),
            "funding_rate_a_hourly": Decimal("0"),
            "funding_rate_b_hourly": Decimal("0"),
            "net_rate_hourly": Decimal("0"),
            "apy_estimate_pct": Decimal("0"),
            "signal": 0,
            "pair_pnl_pct": Decimal("0"),
            "last_check_time": 0,
        }

    # ------------------------------------------------------------------
    # FUNDING INTERVAL - OTTENUTO DINAMICAMENTE DALL'EXCHANGE
    # ------------------------------------------------------------------

    async def _get_funding_interval_hours(self, connector_pair: ConnectorPair) -> int:
        """
        Get funding interval from exchange dynamically.
        Returns hours between funding payments.
        """
        # Usa valore configurato se presente
        if connector_pair == self.config.connector_pair_a:
            if self.config.funding_interval_a_hours is not None:
                return self.config.funding_interval_a_hours
            if self._funding_interval_cache_a is not None:
                return self._funding_interval_cache_a
        else:
            if self.config.funding_interval_b_hours is not None:
                return self.config.funding_interval_b_hours
            if self._funding_interval_cache_b is not None:
                return self._funding_interval_cache_b

        try:
            connector = self.market_data_provider.get_connector(connector_pair.connector_name)
            # Prova a ottenere funding info
            funding_info = await connector.get_funding_info(connector_pair.trading_pair)
            # Alcuni exchange forniscono l'intervallo, altri no
            if hasattr(funding_info, 'interval_hours') and funding_info.interval_hours:
                interval = int(funding_info.interval_hours)
            elif hasattr(funding_info, 'rate_interval') and funding_info.rate_interval:
                # Es: "8 hours" → 8
                interval = int(funding_info.rate_interval.split()[0])
            else:
                # Default: 8 ore per la maggior parte degli exchange
                interval = 8
                # Hyperliquid è 1 ora
                if "hyperliquid" in connector_pair.connector_name.lower():
                    interval = 1
        except Exception as e:
            self.logger().warning(f"Cannot get funding interval for {connector_pair}: {e}")
            # Fallback basato su nome
            if "hyperliquid" in connector_pair.connector_name.lower():
                interval = 1
            else:
                interval = 8

        # Cache
        if connector_pair == self.config.connector_pair_a:
            self._funding_interval_cache_a = interval
        else:
            self._funding_interval_cache_b = interval

        return interval

    # ------------------------------------------------------------------
    # MAIN LOGIC
    # ------------------------------------------------------------------

    def determine_executor_actions(self) -> List[ExecutorAction]:
        actions: List[ExecutorAction] = []

        if self._should_emergency_exit():
            self.logger().warning(
                f"Emergency exit — PnL: {self.processed_data['pair_pnl_pct']:.4%}"
            )
            for position in self.positions_held:
                actions.extend(self._close_position(position))
            return actions

        actions.extend(self._refresh_stale_executors())

        signal = self.processed_data["signal"]
        current_signal = self._get_current_position_signal()

        if current_signal != 0 and (signal == 0 or signal != current_signal):
            self.logger().info(
                f"Closing — net/h: {self.processed_data['net_rate_hourly']:.6%} "
                f"| signal {current_signal} → {signal}"
            )
            for position in self.positions_held:
                actions.extend(self._close_position(position))
            return actions

        if signal != 0 and current_signal == 0 and len(self.positions_held) == 0:
            actions.extend(self._open_position(signal))

        return actions

    def _refresh_stale_executors(self) -> List[ExecutorAction]:
        now = self.market_data_provider.time()
        stale = self.filter_executors(
            self.executors_info,
            filter_func=lambda e: (
                e.is_active
                and not e.is_trading
                and (now - e.timestamp) > self.config.executor_refresh_time
            )
        )
        return [
            StopExecutorAction(
                controller_id=self.config.id,
                executor_id=e.id,
                keep_position=False
            )
            for e in stale
        ]

    async def update_processed_data(self):
        now = self.market_data_provider.time()
        if now - self.processed_data.get("last_check_time", 0) < self.config.funding_check_interval:
            return

        raw_a = await self._get_funding_rate_raw(self.config.connector_pair_a)
        raw_b = await self._get_funding_rate_raw(self.config.connector_pair_b)

        interval_a = await self._get_funding_interval_hours(self.config.connector_pair_a)
        interval_b = await self._get_funding_interval_hours(self.config.connector_pair_b)

        hourly_a = self._normalize_to_hourly(raw_a, interval_a)
        hourly_b = self._normalize_to_hourly(raw_b, interval_b)
        net = hourly_a - hourly_b

        apy = net * Decimal("24") * Decimal("365") * Decimal("100")

        if net > self.config.entry_threshold:
            signal = 1
        elif net < -self.config.entry_threshold:
            signal = -1
        elif abs(net) < self.config.exit_threshold:
            signal = 0
        else:
            signal = self.processed_data.get("signal", 0)

        self.processed_data.update({
            "funding_rate_a_raw": raw_a,
            "funding_rate_b_raw": raw_b,
            "funding_rate_a_hourly": hourly_a,
            "funding_rate_b_hourly": hourly_b,
            "interval_a_hours": interval_a,
            "interval_b_hours": interval_b,
            "net_rate_hourly": net,
            "apy_estimate_pct": apy,
            "signal": signal,
            "pair_pnl_pct": self._compute_pair_pnl_pct(),
            "last_check_time": now,
        })

        self.logger().info(
            f"[{self.config.mode}] "
            f"A({self.config.connector_pair_a.connector_name}): "
            f"raw={raw_a:.6%} interval={interval_a}h → {hourly_a:.6%}/h | "
            f"B({self.config.connector_pair_b.connector_name}): "
            f"raw={raw_b:.6%} interval={interval_b}h → {hourly_b:.6%}/h | "
            f"net={net:+.6%}/h | APY={apy:.1f}% | signal={signal:+d}"
        )

    # ------------------------------------------------------------------
    # POSITION HELPERS
    # ------------------------------------------------------------------

    def _open_position(self, signal: int) -> List[ExecutorAction]:
        actions = []
        a_spot = is_spot_connector(self.config.connector_pair_a.connector_name)
        b_spot = is_spot_connector(self.config.connector_pair_b.connector_name)

        if a_spot or b_spot:
            spot_cp = self.config.connector_pair_a if a_spot else self.config.connector_pair_b
            perp_cp = self.config.connector_pair_b if a_spot else self.config.connector_pair_a
            legs = [(spot_cp, TradeType.BUY), (perp_cp, TradeType.SELL)]
        elif signal == 1:
            legs = [
                (self.config.connector_pair_a, TradeType.SELL),
                (self.config.connector_pair_b, TradeType.BUY),
            ]
        else:
            legs = [
                (self.config.connector_pair_a, TradeType.BUY),
                (self.config.connector_pair_b, TradeType.SELL),
            ]

        for cp, side in legs:
            price = self._get_mid_price(cp)
            amount = self.config.position_amount_quote / price

            config = PositionExecutorConfig(
                timestamp=self.market_data_provider.time(),
                connector_name=cp.connector_name,
                trading_pair=cp.trading_pair,
                side=side,
                entry_price=price,
                amount=amount,
                triple_barrier_config=TripleBarrierConfig(
                    open_order_type=OrderType.LIMIT_MAKER,
                    take_profit_order_type=OrderType.LIMIT_MAKER,
                ),
                leverage=self.config.leverage,
            )
            actions.append(CreateExecutorAction(
                controller_id=self.config.id,
                executor_config=config
            ))

        self.logger().info(
            f"Opening [{self.config.mode}] | signal={signal:+d} | "
            f"APY={self.processed_data['apy_estimate_pct']:.1f}% | "
            f"size/leg={self.config.position_amount_quote} USDT"
        )
        return actions

    def _close_position(self, position: PositionSummary) -> List[ExecutorAction]:
        if position.amount <= Decimal("0"):
            return []
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

    # ------------------------------------------------------------------
    # RATE UTILITIES
    # ------------------------------------------------------------------

    async def _get_funding_rate_raw(self, connector_pair: ConnectorPair) -> Decimal:
        if is_spot_connector(connector_pair.connector_name):
            return Decimal("0")
        try:
            connector = self.market_data_provider.get_connector(connector_pair.connector_name)
            funding_info = await connector.get_funding_info(connector_pair.trading_pair)
            return Decimal(str(funding_info.rate))
        except Exception as e:
            self.logger().warning(f"Cannot fetch funding rate for {connector_pair}: {e}")
            return Decimal("0")

    def _normalize_to_hourly(self, raw_rate: Decimal, interval_hours: int) -> Decimal:
        if interval_hours <= 0:
            return Decimal("0")
        return raw_rate / Decimal(str(interval_hours))

    # ------------------------------------------------------------------
    # UTILS
    # ------------------------------------------------------------------

    def _get_current_position_signal(self) -> int:
        pos_a = next(
            (p for p in self.positions_held
             if p.connector_name == self.config.connector_pair_a.connector_name
             and p.trading_pair == self.config.connector_pair_a.trading_pair),
            None
        )
        if pos_a is None:
            return 0
        return 1 if pos_a.side == TradeType.SELL else -1

    def _should_emergency_exit(self) -> bool:
        pnl = self.processed_data.get("pair_pnl_pct", Decimal("0"))
        return pnl < -self.config.sl_global or pnl > self.config.tp_global

    def _compute_pair_pnl_pct(self) -> Decimal:
        total_value = sum(p.amount_quote for p in self.positions_held)
        total_pnl = sum(p.global_pnl_quote for p in self.positions_held)
        if total_value == Decimal("0"):
            return Decimal("0")
        return total_pnl / total_value

    def _get_mid_price(self, connector_pair: ConnectorPair) -> Decimal:
        return self.market_data_provider.get_price_by_type(
            connector_name=connector_pair.connector_name,
            trading_pair=connector_pair.trading_pair,
            price_type=PriceType.MidPrice
        )

    def to_format_status(self) -> List[str]:
        d = self.processed_data
        return [f"""
Funding Rate Arbitrage  [{self.config.mode}]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Exchange A : {self.config.connector_pair_a.connector_name}  (interval: {d.get('interval_a_hours', '?')}h)
             raw={d['funding_rate_a_raw']:.6%}  →  {d['funding_rate_a_hourly']:.6%}/h
Exchange B : {self.config.connector_pair_b.connector_name}  (interval: {d.get('interval_b_hours', '?')}h)
             raw={d['funding_rate_b_raw']:.6%}  →  {d['funding_rate_b_hourly']:.6%}/h
Net rate   : {d['net_rate_hourly']:+.6%}/h  │  APY: {d['apy_estimate_pct']:.1f}%
Signal     : {d['signal']:+d}  │  Combined PnL: {d['pair_pnl_pct']:.4%}
Thresholds : entry={self.config.entry_threshold:.6%}/h  exit={self.config.exit_threshold:.6%}/h
Capital    : {self.config.total_amount_quote} USDT total  ({self.config.position_amount_quote} per leg)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""]

    def get_candles_config(self) -> List[CandlesConfig]:
        return []