from decimal import Decimal
from typing import List, Dict, Optional
from pydantic import Field, field_validator

from hummingbot.core.data_type.common import MarketDict, OrderType, PriceType, TradeType
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction


class LMMultiPairDEXConfig(ControllerConfigBase):
    """
    Configurazione per Liquidity Mining multi-coppia su DEX con order book.

    Supporta:
    - XRPL DEX (latenza 3-5s, fee ~$0.00001)
    - Hyperliquid (latenza 0.2ms, maker rebate -0.01%)

    I parametri si adattano automaticamente in base al connector_name.
    """
    controller_type: str = "generic"
    controller_name: str = "lm_multi_pair_dex"

    # Exchange - decide automaticamente i parametri ottimali
    connector_name: str = Field(
        default="xrpl",
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Exchange connector (xrpl or hyperliquid):"
        }
    )

    # Markets
    markets: List[str] = Field(
        default=["XRP-RLUSD"],
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Trading pairs (comma-separated). XRPL: XRP-RLUSD, BTC-XRP | Hyperliquid: SOL-USDC, ETH-USDC:"
        }
    )

    # Token unico
    token: str = Field(
        default="XRP",
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Unified token (XRPL: XRP or RLUSD | Hyperliquid: USDC recommended):"
        }
    )

    # Allocazione capitale
    portfolio_allocation: Decimal = Field(default=Decimal("0.1"), gt=0, le=1)
    total_amount_quote: Decimal = Field(default=Decimal("1000"), gt=0)
    # Spread - valori base (verranno automaticamente scalati in base al DEX)
    # Su XRPL: più larghi, su Hyperliquid: più stretti
    buy_spreads: List[float] = Field(default="0.005,0.01,0.02", validate_default=True)
    sell_spreads: List[float] = Field(default="0.005,0.01,0.02", validate_default=True)

    # Timing - valori base
    order_refresh_time: int = Field(default=45)
    cooldown_time: int = Field(default=20)
    order_refresh_tolerance_pct: Decimal = Field(default=Decimal("0.01"), ge=-1, le=1)

    # Skew parameters
    target_base_pct: Decimal = Field(default=Decimal("0.5"), ge=0, le=1)
    min_base_pct: Decimal = Field(default=Decimal("0.3"), ge=0, le=1)
    max_base_pct: Decimal = Field(default=Decimal("0.7"), ge=0, le=1)
    max_skew: Decimal = Field(default=Decimal("0.2"), ge=0, le=1)

    leverage: int = Field(default=1)
    take_profit: Optional[Decimal] = Field(default=None)
    use_dynamic_spreads: bool = Field(default=True)
    atr_length: int = Field(default=14)
    atr_multiplier_min: float = Field(default=0.5)
    atr_multiplier_max: float = Field(default=2.0)
    min_liquidity_score: float = Field(default=0.3)
    min_volume_usd: float = Field(default=10000)
    max_spread_multiplier: float = Field(default=3.0)
    min_spread_multiplier: float = Field(default=0.3)
    @field_validator('markets', mode='before')
    def parse_markets(cls, v):
        if isinstance(v, str):
            return [x.strip() for x in v.split(',')]
        return v

    @field_validator('buy_spreads', 'sell_spreads', mode='before')
    def parse_spreads(cls, v):
        if isinstance(v, str):
            return [float(x.strip()) for x in v.split(',')]
        return v

    def update_markets(self, markets: MarketDict) -> MarketDict:
        for pair in self.markets:
            markets.add_or_update(self.connector_name, pair)
        return markets


class LMMultiPairDEX(ControllerBase):
    def __init__(self, config: LMMultiPairDEXConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config

        # === AUTO-OTTIMIZZAZIONE in base al DEX ===
        self._dex_type = self._detect_dex_type()
        self._apply_dex_optimizations()

        # Verifica configurazione specifica per DEX
        self._validate_dex_config()

        self.market_data_provider.initialize_rate_sources([
            ConnectorPair(config.connector_name, pair) for pair in config.markets
        ])

        self._pair_last_fill: Dict[str, float] = {pair: 0 for pair in config.markets}
        self._active_executor_ids_per_pair: Dict[str, set] = {pair: set() for pair in config.markets}

    def _detect_dex_type(self) -> str:
        """Rileva automaticamente il tipo di DEX dal connector_name."""
        connector = self.config.connector_name.lower()
        if "xrpl" in connector:
            return "xrpl"
        elif "hyperliquid" in connector:
            return "hyperliquid"
        else:
            self.logger().warning(f"Connector {connector} non riconosciuto. Usando parametri default.")
            return "unknown"

    def _apply_dex_optimizations(self):
        """Applica ottimizzazioni specifiche per il DEX rilevato."""
        if self._dex_type == "xrpl":
            self.logger().info("🔵 RILEVATO XRPL DEX - Applicando ottimizzazioni: spread larghi, refresh lento")

            # XRPL: allarga spread se sono troppo stretti
            min_recommended_spread = 0.003  # 0.3% minimo raccomandato
            for i, spread in enumerate(self.config.buy_spreads):
                if spread < min_recommended_spread:
                    self.config.buy_spreads[i] = min_recommended_spread
                    self.logger().warning(f"Spread buy livello {i} aumentato a {min_recommended_spread}% (minimo per XRPL)")

            # XRPL: rallenta refresh time (latenza 3-5 secondi)
            if self.config.order_refresh_time < 60:
                old = self.config.order_refresh_time
                self.config.order_refresh_time = 60
                self.logger().info(f"order_refresh_time aumentato da {old}s a 60s per XRPL")

            if self.config.cooldown_time < 30:
                old = self.config.cooldown_time
                self.config.cooldown_time = 30
                self.logger().info(f"cooldown_time aumentato da {old}s a 30s per XRPL")

            # XRPL: tolleranza più alta (prezzi meno precisi)
            if self.config.order_refresh_tolerance_pct < Decimal("0.01"):
                old = self.config.order_refresh_tolerance_pct
                self.config.order_refresh_tolerance_pct = Decimal("0.01")
                self.logger().info(f"order_refresh_tolerance_pct aumentato da {old} a 1% per XRPL")

        elif self._dex_type == "hyperliquid":
            self.logger().info("🟣 RILEVATO HYPERLIQUID - Applicando ottimizzazioni: spread medi, refresh veloce")

            # Hyperliquid: restringi spread (rebate maker permette spread più stretti)
            max_recommended_spread = 0.01  # 1% massimo raccomandato
            for i, spread in enumerate(self.config.buy_spreads):
                if spread > max_recommended_spread:
                    self.config.buy_spreads[i] = max_recommended_spread
                    self.logger().warning(f"Spread buy livello {i} ridotto a {max_recommended_spread}% (massimo per Hyperliquid)")

            # Hyperliquid: refresh più veloce (latenza 0.2ms)
            if self.config.order_refresh_time > 45:
                old = self.config.order_refresh_time
                self.config.order_refresh_time = 30
                self.logger().info(f"order_refresh_time ridotto da {old}s a 30s per Hyperliquid")

            if self.config.cooldown_time > 25:
                old = self.config.cooldown_time
                self.config.cooldown_time = 15
                self.logger().info(f"cooldown_time ridotto da {old}s a 15s per Hyperliquid")

            # Hyperliquid: tolleranza più bassa (prezzi molto precisi)
            if self.config.order_refresh_tolerance_pct > Decimal("0.005") and self.config.order_refresh_tolerance_pct != Decimal("-1"):
                old = self.config.order_refresh_tolerance_pct
                self.config.order_refresh_tolerance_pct = Decimal("0.005")
                self.logger().info(f"order_refresh_tolerance_pct ridotto da {old} a 0.5% per Hyperliquid")

    def _validate_dex_config(self):
        """Verifica che la configurazione sia valida per il DEX."""
        if self._dex_type == "xrpl":
            # Verifica token nativi
            if self.config.token not in ["XRP", "RLUSD"]:
                self.logger().warning(
                    f"Token {self.config.token} non è nativo di XRPL. "
                    f"Assicurati di aver stabilito una trust line."
                )

            # Verifica fee XRP per transazioni
            xrp_balance = self.market_data_provider.get_balance(self.config.connector_name, "XRP")
            if xrp_balance < Decimal("10"):
                self.logger().warning(
                    f"Hai solo {xrp_balance} XRP per le fee. Minimo raccomandato: 10 XRP."
                )

        elif self._dex_type == "hyperliquid":
            # Verifica token USDC
            if self.config.token != "USDC":
                self.logger().warning(
                    f"Token {self.config.token} non è USDC. "
                    f"Le migliori fee su Hyperliquid sono con USDC."
                )

            # Verifica che si usi spot, non perp
            if "perpetual" in self.config.connector_name.lower():
                self.logger().error(
                    "Questo controller è per spot market making. "
                    "Usa 'hyperliquid' (spot), non 'hyperliquid_perpetual'."
                )

    async def update_processed_data(self):
        ref_prices = {}
        for pair in self.config.markets:
            price = self.market_data_provider.get_price_by_type(
                self.config.connector_name, pair, PriceType.MidPrice
            )
            ref_prices[pair] = Decimal(str(price)) if price else Decimal("0")

            # Calcola percentuale di base per lo skew
            base, quote = pair.split("-")
            base_balance = self.market_data_provider.get_balance(self.config.connector_name, base)
            quote_balance = self.market_data_provider.get_balance(self.config.connector_name, quote)
            price_ = ref_prices[pair]
            total_value = base_balance * price_ + quote_balance if price_ > 0 else Decimal("0")
            base_pct = (base_balance * price_) / total_value if total_value > 0 else Decimal("0")
            self.processed_data[f"base_pct_{pair}"] = base_pct

        self.processed_data["ref_prices"] = ref_prices

    def _calculate_skew(self, base_pct: Decimal) -> tuple[Decimal, Decimal]:
        min_pct = self.config.min_base_pct
        max_pct = self.config.max_base_pct
        if max_pct > min_pct:
            buy_skew = (max_pct - base_pct) / (max_pct - min_pct)
            sell_skew = (base_pct - min_pct) / (max_pct - min_pct)
            buy_skew = max(min(buy_skew, Decimal("1.0")), self.config.max_skew)
            sell_skew = max(min(sell_skew, Decimal("1.0")), self.config.max_skew)
        else:
            buy_skew = sell_skew = Decimal("1.0")
        return buy_skew, sell_skew

    def _is_within_tolerance(self, current_price: Decimal, theoretical_price: Decimal) -> bool:
        if self.config.order_refresh_tolerance_pct < 0:
            return False
        if current_price == 0 or theoretical_price == 0:
            return False
        diff = abs(current_price - theoretical_price) / current_price
        return diff <= self.config.order_refresh_tolerance_pct

    def determine_executor_actions(self) -> List[ExecutorAction]:
        actions = []
        num_pairs = len(self.config.markets)
        if num_pairs == 0:
            return actions

        total_quote = self.config.total_amount_quote * self.config.portfolio_allocation
        quote_per_pair = total_quote / Decimal(num_pairs)
        current_time = self.market_data_provider.time()

        # Aggiorna mappa executor attivi
        for pair in self.config.markets:
            self._active_executor_ids_per_pair[pair] = set()
        for ex in self.executors_info:
            if ex.is_active:
                pair = ex.custom_info.get("trading_pair")
                if pair in self._active_executor_ids_per_pair:
                    self._active_executor_ids_per_pair[pair].add(ex.id)

        for pair in self.config.markets:
            ref_price = self.processed_data["ref_prices"].get(pair, Decimal("0"))
            if ref_price <= 0:
                continue

            base_pct = self.processed_data.get(f"base_pct_{pair}", Decimal("0"))
            buy_skew, sell_skew = self._calculate_skew(base_pct)

            active_executors = [
                e for e in self.executors_info
                if e.is_active and e.custom_info.get("trading_pair") == pair
            ]

            # Refresh executor scaduti e fuori tolleranza
            for ex in active_executors:
                if ex.is_trading:
                    continue
                age = current_time - ex.timestamp
                if age <= self.config.order_refresh_time:
                    continue

                level_id = ex.custom_info.get("level_id")
                if not level_id or not hasattr(ex.config, 'price'):
                    continue

                trade_type = TradeType.BUY if level_id.startswith("buy") else TradeType.SELL
                level = int(level_id.split('_')[1])
                if trade_type == TradeType.BUY:
                    spread = Decimal(self.config.buy_spreads[level])
                    theoretical = ref_price * (Decimal("1") - spread)
                else:
                    spread = Decimal(self.config.sell_spreads[level])
                    theoretical = ref_price * (Decimal("1") + spread)

                current_price = Decimal(str(ex.config.price))
                if not self._is_within_tolerance(current_price, theoretical):
                    actions.append(StopExecutorAction(
                        controller_id=self.config.id,
                        executor_id=ex.id,
                        keep_position=True
                    ))

            # Determina livelli mancanti
            active_level_ids = {e.custom_info.get("level_id") for e in active_executors if e.custom_info.get("level_id")}
            buy_levels_needed = [f"buy_{i}" for i in range(len(self.config.buy_spreads)) if f"buy_{i}" not in active_level_ids]
            sell_levels_needed = [f"sell_{i}" for i in range(len(self.config.sell_spreads)) if f"sell_{i}" not in active_level_ids]

            # Cooldown
            last_fill = self._pair_last_fill.get(pair, 0)
            if current_time - last_fill < self.config.cooldown_time:
                continue

            # Crea executor per buy
            for level_id in buy_levels_needed:
                level = int(level_id.split('_')[1])
                spread = Decimal(self.config.buy_spreads[level])
                price = ref_price * (Decimal("1") - spread)
                num_buy_levels = len(self.config.buy_spreads)
                amount_quote = quote_per_pair / Decimal(num_buy_levels)
                amount = amount_quote / price
                amount = amount * buy_skew
                amount = self.market_data_provider.quantize_order_amount(self.config.connector_name, pair, amount)
                if amount <= 0:
                    continue

                executor_config = PositionExecutorConfig(
                    timestamp=current_time,
                    level_id=level_id,
                    connector_name=self.config.connector_name,
                    trading_pair=pair,
                    entry_price=price,
                    amount=amount,
                    triple_barrier_config=TripleBarrierConfig(
                        take_profit=self.config.take_profit,
                        stop_loss=self.config.stop_loss,   # <-- questa riga
                        open_order_type=OrderType.LIMIT_MAKER,
                        take_profit_order_type=OrderType.LIMIT_MAKER,
                    ),
                    leverage=self.config.leverage,
                    side=TradeType.BUY,
                )
                executor_config.custom_info = {"trading_pair": pair, "level_id": level_id}
                actions.append(CreateExecutorAction(controller_id=self.config.id, executor_config=executor_config))

            # Crea executor per sell
            for level_id in sell_levels_needed:
                level = int(level_id.split('_')[1])
                spread = Decimal(self.config.sell_spreads[level])
                price = ref_price * (Decimal("1") + spread)
                num_sell_levels = len(self.config.sell_spreads)
                amount_quote = quote_per_pair / Decimal(num_sell_levels)
                amount = amount_quote / price
                amount = amount * sell_skew
                amount = self.market_data_provider.quantize_order_amount(self.config.connector_name, pair, amount)
                if amount <= 0:
                    continue

                executor_config = PositionExecutorConfig(
                    timestamp=current_time,
                    level_id=level_id,
                    connector_name=self.config.connector_name,
                    trading_pair=pair,
                    entry_price=price,
                    amount=amount,
                    triple_barrier_config=TripleBarrierConfig(
                        take_profit=self.config.take_profit,
                        stop_loss=self.config.stop_loss,   # <-- questa riga
                        open_order_type=OrderType.LIMIT_MAKER,
                        take_profit_order_type=OrderType.LIMIT_MAKER,
                    ),
                    leverage=self.config.leverage,
                    side=TradeType.SELL,
                )
                executor_config.custom_info = {"trading_pair": pair, "level_id": level_id}
                actions.append(CreateExecutorAction(controller_id=self.config.id, executor_config=executor_config))

        return actions

    def did_fill_order(self, event):
        pair = event.trading_pair
        if pair in self._pair_last_fill:
            self._pair_last_fill[pair] = self.market_data_provider.time()
        super().did_fill_order(event)

    def to_format_status(self) -> List[str]:
        dex_icon = "🔵 XRPL" if self._dex_type == "xrpl" else "🟣 HYPERLIQUID" if self._dex_type == "hyperliquid" else "⚪ UNKNOWN"

        lines = [
            f"{dex_icon} Liquidity Mining | Token: {self.config.token}",
            f"Allocazione: {self.config.portfolio_allocation:.1%} | Livelli: buy={self.config.buy_spreads} sell={self.config.sell_spreads}",
            f"Refresh: {self.config.order_refresh_time}s | Cooldown: {self.config.cooldown_time}s | Tolleranza: {self.config.order_refresh_tolerance_pct:.2%}"
        ]

        # Aggiungi info specifiche per DEX
        if self._dex_type == "hyperliquid":
            lines.append(f"💰 Maker rebate attivo: -0.01% (ti pagano per ogni ordine eseguito)")
        elif self._dex_type == "xrpl":
            lines.append(f"💰 Fee per ordine: ~0.000012 XRP (quasi zero)")

        for pair in self.config.markets:
            active = len(self._active_executor_ids_per_pair.get(pair, set()))
            base_pct = self.processed_data.get(f"base_pct_{pair}", Decimal("0"))
            lines.append(f"  {pair}: {active} attivi | base%: {base_pct:.1%}")
        return lines
