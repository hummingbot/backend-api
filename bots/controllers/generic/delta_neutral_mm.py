from decimal import Decimal
from typing import Dict, List, Optional

import pandas_ta as ta  # noqa: F401
from pydantic import Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, PriceType, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction


def is_perpetual_connector(connector_name: str) -> bool:
    """Detect if connector is perpetual by name conventions."""
    name_lower = connector_name.lower()
    return "_perpetual" in name_lower or "perp" in name_lower


class DeltaNeutralMMConfig(ControllerConfigBase):
    """
    Delta Neutral Market Making.
    """
    controller_type: str = "generic"
    controller_name: str = "delta_neutral_mm"

    # --- Exchanges ---
    connector_pair_maker: ConnectorPair = ConnectorPair(
        connector_name="kucoin",
        trading_pair="SOL-USDT"
    )
    connector_pair_hedge: ConnectorPair = ConnectorPair(
        connector_name="hyperliquid_perpetual",
        trading_pair="SOL-USDT"
    )

    # --- Candles ---
    candles_connector: Optional[str] = Field(
        default=None,
        json_schema_extra={"prompt": "Candles connector (leave empty = maker exchange): ",
                           "prompt_on_new": True}
    )
    candles_trading_pair: Optional[str] = Field(
        default=None,
        json_schema_extra={"prompt": "Candles pair (leave empty = maker pair): ",
                           "prompt_on_new": True}
    )
    interval: str = Field(default="3m")

    # --- MACD parameters ---
    macd_fast: int = Field(default=21)
    macd_slow: int = Field(default=42)
    macd_signal: int = Field(default=9)

    # --- NATR parameters ---
    natr_length: int = Field(default=14)

    # --- Market making levels ---
    buy_spreads: str = Field(
        default="1.0,2.0,3.0",
        json_schema_extra={"prompt": "Buy spreads as comma-separated values (e.g., 1.0,2.0,3.0): ",
                           "prompt_on_new": True}
    )
    sell_spreads: str = Field(
        default="1.0,2.0,3.0",
        json_schema_extra={"prompt": "Sell spreads as comma-separated values (e.g., 1.0,2.0,3.0): ",
                           "prompt_on_new": True}
    )

    order_amount_quote: Decimal = Field(
        default=Decimal("15"),
        json_schema_extra={"prompt": "Order amount in quote currency per level: ",
                           "prompt_on_new": True}
    )
    order_refresh_time: int = Field(
        default=30,
        json_schema_extra={"prompt": "Refresh unfilled orders after (seconds): ",
                           "prompt_on_new": True}
    )

    # --- Delta / hedge parameters ---
    hedge_threshold_quote: Decimal = Field(
        default=Decimal("10"),
        json_schema_extra={"prompt": "Hedge when delta exceeds (USDT): ",
                           "prompt_on_new": True}
    )
    max_delta_quote: Decimal = Field(
        default=Decimal("50"),
        json_schema_extra={"prompt": "Maximum unhedged delta before emergency (USDT): ",
                           "prompt_on_new": True}
    )

    # --- Hedge perp settings ---
    leverage: int = Field(
        default=1,
        json_schema_extra={"prompt": "Leverage for hedge positions (1x recommended): ",
                           "prompt_on_new": True}
    )
    position_mode: PositionMode = PositionMode.HEDGE

    # --- Global safety ---
    sl_global: Decimal = Field(
        default=Decimal("0.03"),
        json_schema_extra={"prompt": "Global stop loss (e.g., 0.03 = 3%): ",
                           "prompt_on_new": True}
    )
    tp_global: Decimal = Field(
        default=Decimal("0.05"),
        json_schema_extra={"prompt": "Global take profit (e.g., 0.05 = 5%): ",
                           "prompt_on_new": True}
    )

    # --- Hedge position timeout ---
    hedge_position_timeout: int = Field(
        default=3600,
        json_schema_extra={"prompt": "Close hedge positions after (seconds, 0 = disabled): ",
                           "prompt_on_new": True}
    )

    # --- Take profit multiplier for maker orders ---
    maker_tp_multiplier: Decimal = Field(
        default=Decimal("1.0"),
        json_schema_extra={"prompt": "Take profit multiplier for maker orders (1.0 = spread × 1): ",
                           "prompt_on_new": True}
    )

    @field_validator("candles_connector", mode="before")
    @classmethod
    def set_candles_connector(cls, v, validation_info: ValidationInfo):
        if v is None or v == "":
            cp = validation_info.data.get("connector_pair_maker")
            if cp and hasattr(cp, "connector_name"):
                return cp.connector_name
            return "kucoin"
        return v
    @field_validator("buy_spreads", "sell_spreads", mode="before")
    @classmethod
    def parse_spreads_string(cls, v):
        if isinstance(v, str):
            # Restituisci la stringa così com'è (per la serializzazione)
            return v
        # Se arriva già come lista (es. da vecchie config), converti in stringa
        if isinstance(v, list):
            return ",".join(str(x) for x in v)
        return v

    # Aggiungi property per ottenere la lista (usata nel resto del controller)
    @property
    def buy_spreads_list(self) -> List[float]:
        return [float(x.strip()) for x in self.buy_spreads.split(",")]

    @property
    def sell_spreads_list(self) -> List[float]:
        return [float(x.strip()) for x in self.sell_spreads.split(",")]
    @field_validator("candles_trading_pair", mode="before")
    @classmethod
    def set_candles_trading_pair(cls, v, validation_info: ValidationInfo):
        if v is None or v == "":
            cp = validation_info.data.get("connector_pair_maker")
            if cp and hasattr(cp, "trading_pair"):
                return cp.trading_pair
            return "SOL-USDT"
        return v

    @field_validator("buy_spreads", "sell_spreads", mode="before")
    @classmethod
    def parse_spreads(cls, v):
        if isinstance(v, str):
            return [float(x.strip()) for x in v.split(",")]
        return v

    def update_markets(self, markets: dict) -> dict:
        for cp in [self.connector_pair_maker, self.connector_pair_hedge]:
            if cp.connector_name not in markets:
                markets[cp.connector_name] = set()
            markets[cp.connector_name].add(cp.trading_pair)
        return markets


class DeltaNeutralMM(ControllerBase):
    """Delta Neutral MM execution engine."""

    def __init__(self, config: DeltaNeutralMMConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self.max_records = max(
            config.macd_slow, config.macd_fast,
            config.macd_signal, config.natr_length
        ) + 100

        # Configure hedge perp connector
        if is_perpetual_connector(config.connector_pair_hedge.connector_name):
            try:
                connector = self.market_data_provider.get_connector(
                    config.connector_pair_hedge.connector_name
                )
                connector.set_position_mode(config.position_mode)
                connector.set_leverage(config.connector_pair_hedge.trading_pair, config.leverage)
            except Exception as e:
                self.logger().warning(f"Could not configure hedge connector: {e}")

        self.processed_data = {
            "reference_price": None,
            "spread_multiplier": None,
            "natr": None,
            "macd_signal_value": None,
            "net_delta_quote": Decimal("0"),
            "hedge_position_quote": Decimal("0"),
            "combined_pnl_pct": Decimal("0"),
            "active_maker_orders": [],
            "active_hedge_positions": [],
        }

        # Track hedge positions with their creation timestamp
        # Chiave = executor_id (string), valore = timestamp (float)
        self._hedge_positions_timestamp: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # MAIN LOGIC
    # ------------------------------------------------------------------

    def determine_executor_actions(self) -> List[ExecutorAction]:
        actions: List[ExecutorAction] = []

        if self.processed_data["reference_price"] is None:
            return actions

        # 1. Emergency exit
        if self._should_emergency_exit():
            self.logger().warning(
                f"Emergency exit — combined PnL: "
                f"{self.processed_data['combined_pnl_pct']:.4%}"
            )
            actions.extend(self._close_all())
            return actions

        # 2. Check hedge position timeout (traccia executor esistenti)
        if self.config.hedge_position_timeout > 0:
            self._track_existing_hedge_positions()
            actions.extend(self._check_hedge_timeout())

        # 3. Emergency delta cap
        net_delta = self.processed_data["net_delta_quote"]
        if abs(net_delta) > self.config.max_delta_quote:
            self.logger().warning(
                f"Delta cap breached: {net_delta:.2f} USDT — force hedging"
            )
            actions.extend(self._place_hedge_order(net_delta))
            return actions

        # 4. Normal hedge
        if abs(net_delta) > self.config.hedge_threshold_quote:
            actions.extend(self._place_hedge_order(net_delta))

        # 5. Refresh stale maker orders
        actions.extend(self._refresh_stale_maker_orders())

        # 6. Place new maker orders
        actions.extend(self._place_maker_orders())

        return actions

    def _track_existing_hedge_positions(self):
        """Traccia gli executor hedge esistenti che non sono ancora nel dizionario."""
        for executor in self.executors_info:
            if (executor.connector_name == self.config.connector_pair_hedge.connector_name and
                executor.id not in self._hedge_positions_timestamp and
                executor.is_active):
                self._hedge_positions_timestamp[executor.id] = self.market_data_provider.time()
                self.logger().debug(f"Tracking hedge position {executor.id}")

    def _check_hedge_timeout(self) -> List[ExecutorAction]:
        """Close hedge positions that have been open too long."""
        actions = []
        now = self.market_data_provider.time()

        for executor_id, timestamp in list(self._hedge_positions_timestamp.items()):
            if now - timestamp > self.config.hedge_position_timeout:
                executor = next(
                    (e for e in self.executors_info if e.id == executor_id),
                    None
                )
                if executor and executor.is_active:
                    self.logger().info(f"Closing hedge position {executor_id} due to timeout ({now - timestamp:.0f}s > {self.config.hedge_position_timeout}s)")
                    actions.append(StopExecutorAction(
                        controller_id=self.config.id,
                        executor_id=executor_id,
                        keep_position=False
                    ))
                # Rimuovi dal tracking anche se l'executor non esiste più
                del self._hedge_positions_timestamp[executor_id]

        return actions

    async def update_processed_data(self):
        candles = self.market_data_provider.get_candles_df(
            connector_name=self.config.candles_connector,
            trading_pair=self.config.candles_trading_pair,
            interval=self.config.interval,
            max_records=self.max_records
        )
        if candles.empty:
            return

        # NATR
        natr = ta.natr(
            candles["high"], candles["low"], candles["close"],
            length=self.config.natr_length
        ) / 100

        # MACD
        macd_output = ta.macd(
            candles["close"],
            fast=self.config.macd_fast,
            slow=self.config.macd_slow,
            signal=self.config.macd_signal
        )
        macd_col = f"MACD_{self.config.macd_fast}_{self.config.macd_slow}_{self.config.macd_signal}"
        macdh_col = f"MACDh_{self.config.macd_fast}_{self.config.macd_slow}_{self.config.macd_signal}"

        macd = macd_output[macd_col]
        if macd.std() != 0:
            macd_norm = -(macd - macd.mean()) / macd.std()
        else:
            macd_norm = macd * 0

        macdh = macd_output[macdh_col]
        macdh_signal = macdh.apply(lambda x: 1 if x > 0 else -1)

        # Price shift
        max_shift = natr / 2
        price_multiplier = ((0.5 * macd_norm + 0.5 * macdh_signal) * max_shift).iloc[-1]

        reference_price = Decimal(str(candles["close"].iloc[-1])) * (
            Decimal("1") + Decimal(str(price_multiplier))
        )
        spread_multiplier = Decimal(str(natr.iloc[-1]))

        net_delta_quote = self._compute_net_delta_quote()
        combined_pnl_pct = self._compute_combined_pnl_pct()

        active_maker = self.filter_executors(
            self.executors_info,
            filter_func=lambda e: (
                e.connector_name == self.config.connector_pair_maker.connector_name
                and e.is_active
            )
        )
        active_hedge = self.filter_executors(
            self.executors_info,
            filter_func=lambda e: (
                e.connector_name == self.config.connector_pair_hedge.connector_name
                and e.is_active
            )
        )

        self.processed_data.update({
            "reference_price": reference_price,
            "spread_multiplier": spread_multiplier,
            "natr": spread_multiplier,
            "macd_signal_value": float(price_multiplier),
            "net_delta_quote": net_delta_quote,
            "combined_pnl_pct": combined_pnl_pct,
            "active_maker_orders": active_maker,
            "active_hedge_positions": active_hedge,
        })

    # ------------------------------------------------------------------
    # MAKER ORDERS
    # ------------------------------------------------------------------

    def _place_maker_orders(self) -> List[ExecutorAction]:
        actions = []
        ref_price = self.processed_data["reference_price"]
        spread_mult = self.processed_data["spread_multiplier"]

        active_maker = self.processed_data["active_maker_orders"]
        active_buy_levels = sum(
            1 for e in active_maker
            if hasattr(e, 'config') and e.config.side == TradeType.BUY and not e.is_trading
        )
        active_sell_levels = sum(
            1 for e in active_maker
            if hasattr(e, 'config') and e.config.side == TradeType.SELL and not e.is_trading
        )

        # Buy levels
        for i, spread in enumerate(self.config.buy_spreads_list):
            if active_buy_levels >= len(self.config.buy_spreads_list):
                break
            price = ref_price * (Decimal("1") - Decimal(str(spread)) * spread_mult)
            amount = self.config.order_amount_quote / price

            actions.append(CreateExecutorAction(
                controller_id=self.config.id,
                executor_config=PositionExecutorConfig(
                    timestamp=self.market_data_provider.time(),
                    level_id=f"buy_{i}",
                    connector_name=self.config.connector_pair_maker.connector_name,
                    trading_pair=self.config.connector_pair_maker.trading_pair,
                    side=TradeType.BUY,
                    entry_price=price,
                    amount=amount,
                    triple_barrier_config=TripleBarrierConfig(
                        open_order_type=OrderType.LIMIT_MAKER,
                        take_profit_order_type=OrderType.LIMIT_MAKER,
                        take_profit=Decimal(str(spread)) * spread_mult * self.config.maker_tp_multiplier,
                    ),
                    leverage=1,
                )
            ))

        # Sell levels
        for i, spread in enumerate(self.config.sell_spreads_list):
            if active_sell_levels >= len(self.config.sell_spreads_list):
                break
            price = ref_price * (Decimal("1") + Decimal(str(spread)) * spread_mult)
            amount = self.config.order_amount_quote / price

            actions.append(CreateExecutorAction(
                controller_id=self.config.id,
                executor_config=PositionExecutorConfig(
                    timestamp=self.market_data_provider.time(),
                    level_id=f"sell_{i}",
                    connector_name=self.config.connector_pair_maker.connector_name,
                    trading_pair=self.config.connector_pair_maker.trading_pair,
                    side=TradeType.SELL,
                    entry_price=price,
                    amount=amount,
                    triple_barrier_config=TripleBarrierConfig(
                        open_order_type=OrderType.LIMIT_MAKER,
                        take_profit_order_type=OrderType.LIMIT_MAKER,
                        take_profit=Decimal(str(spread)) * spread_mult * self.config.maker_tp_multiplier,
                    ),
                    leverage=1,
                )
            ))

        return actions

    def _refresh_stale_maker_orders(self) -> List[ExecutorAction]:
        now = self.market_data_provider.time()
        stale = self.filter_executors(
            self.executors_info,
            filter_func=lambda e: (
                e.connector_name == self.config.connector_pair_maker.connector_name
                and e.is_active
                and not e.is_trading
                and (now - e.timestamp) > self.config.order_refresh_time
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

    # ------------------------------------------------------------------
    # HEDGE ORDERS
    # ------------------------------------------------------------------

    def _place_hedge_order(self, net_delta_quote: Decimal) -> List[ExecutorAction]:
        hedge_side = TradeType.SELL if net_delta_quote > Decimal("0") else TradeType.BUY
        hedge_price = self.market_data_provider.get_price_by_type(
            connector_name=self.config.connector_pair_hedge.connector_name,
            trading_pair=self.config.connector_pair_hedge.trading_pair,
            price_type=PriceType.MidPrice
        )
        hedge_amount = abs(net_delta_quote) / hedge_price

        self.logger().info(
            f"Hedging delta: {net_delta_quote:+.2f} USDT → "
            f"{hedge_side.name} {hedge_amount:.4f} on "
            f"{self.config.connector_pair_hedge.connector_name}"
        )

        config = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_pair_hedge.connector_name,
            trading_pair=self.config.connector_pair_hedge.trading_pair,
            side=hedge_side,
            amount=hedge_amount,
            position_action=PositionAction.OPEN,
            execution_strategy=ExecutionStrategy.MARKET,
            leverage=self.config.leverage,
        )
        
        action = CreateExecutorAction(controller_id=self.config.id, executor_config=config)
        
        # NOTA: Non possiamo registrare il timestamp qui perché l'executor_id non esiste ancora.
        # Il tracking avverrà in _track_existing_hedge_positions() nel prossimo ciclo.
        
        return [action]

    # ------------------------------------------------------------------
    # UTILS
    # ------------------------------------------------------------------

    def _compute_net_delta_quote(self) -> Decimal:
        maker_delta = Decimal("0")
        for pos in self.positions_held:
            if pos.connector_name == self.config.connector_pair_maker.connector_name:
                if pos.side == TradeType.BUY:
                    maker_delta += pos.amount_quote
                else:
                    maker_delta -= pos.amount_quote

        hedge_delta = Decimal("0")
        for pos in self.positions_held:
            if pos.connector_name == self.config.connector_pair_hedge.connector_name:
                if pos.side == TradeType.BUY:
                    hedge_delta += pos.amount_quote
                else:
                    hedge_delta -= pos.amount_quote

        return maker_delta + hedge_delta

    def _compute_combined_pnl_pct(self) -> Decimal:
        total_value = sum(p.amount_quote for p in self.positions_held)
        total_pnl = sum(p.global_pnl_quote for p in self.positions_held)
        if total_value == Decimal("0"):
            return Decimal("0")
        return total_pnl / total_value

    def _should_emergency_exit(self) -> bool:
        pnl = self.processed_data.get("combined_pnl_pct", Decimal("0"))
        return pnl < -self.config.sl_global or pnl > self.config.tp_global

    def _close_all(self) -> List[ExecutorAction]:
        actions = []
        for pos in self.positions_held:
            if pos.amount <= Decimal("0"):
                continue
            config = OrderExecutorConfig(
                timestamp=self.market_data_provider.time(),
                connector_name=pos.connector_name,
                trading_pair=pos.trading_pair,
                side=TradeType.BUY if pos.side == TradeType.SELL else TradeType.SELL,
                amount=pos.amount,
                position_action=PositionAction.CLOSE,
                execution_strategy=ExecutionStrategy.MARKET,
                leverage=self.config.leverage,
            )
            actions.append(CreateExecutorAction(
                controller_id=self.config.id,
                executor_config=config
            ))
        return actions

    def to_format_status(self) -> List[str]:
        d = self.processed_data
        ref = d.get("reference_price") or Decimal("0")
        natr = d.get("natr") or Decimal("0")
        macd_v = d.get("macd_signal_value") or 0.0
        active_maker = d.get("active_maker_orders", [])
        active_hedge = d.get("active_hedge_positions", [])

        placed_buys = sum(1 for e in active_maker if hasattr(e, 'config') and e.config.side == TradeType.BUY and not e.is_trading)
        placed_sells = sum(1 for e in active_maker if hasattr(e, 'config') and e.config.side == TradeType.SELL and not e.is_trading)
        filled_maker = sum(1 for e in active_maker if e.is_trading)

        # Mostra timeout info
        timeout_info = f" (timeout: {self.config.hedge_position_timeout}s)" if self.config.hedge_position_timeout > 0 else " (timeout disabled)"

        return [f"""
Delta Neutral Market Making
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Maker  : {self.config.connector_pair_maker.connector_name}
Hedge  : {self.config.connector_pair_hedge.connector_name}

MACD signal  : {macd_v:+.6f}  │  NATR spread: {natr:.4%}
Reference px : {ref:.4f}

Maker orders : {placed_buys} buys placed  │  {placed_sells} sells placed  │  {filled_maker} filled
Active hedge : {len(active_hedge)} positions{timeout_info}

Net delta    : {d['net_delta_quote']:+.2f} USDT
  threshold  : ±{self.config.hedge_threshold_quote} USDT
  max cap    : ±{self.config.max_delta_quote} USDT

Combined PnL : {d['combined_pnl_pct']:.4%}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""]

    def get_candles_config(self) -> List[CandlesConfig]:
        return [CandlesConfig(
            connector=self.config.candles_connector,
            trading_pair=self.config.candles_trading_pair,
            interval=self.config.interval,
            max_records=self.max_records
        )]
