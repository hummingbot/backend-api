"""
Ticker source adapters.

Each adapter fetches a single bulk "all tickers" payload from one exchange and normalizes
it into ``{hb_trading_pair -> Ticker}``. The request goes through the connector's own
``_api_get``/``_api_post`` so the exchange base URL, domain, authentication wrapper and
rate-limit throttling are reused (unlike raw HTTP calls). The raw exchange symbol is mapped
to the Hummingbot ``BASE-QUOTE`` format via the connector's trading pair symbol map, which
initializes itself lazily from a public endpoint -- so every adapter here works on a keyless,
never-started data connector.

Most exchanges are described declaratively in ``TICKER_SPECS`` (one table row per exchange:
request path, row shaper and field names). Exchanges whose payload needs real logic to join
against the symbol map get a hand-written adapter in ``TICKER_ADAPTERS``. Connectors in
neither table fall back to the price-only ``_generic`` adapter.

Volume is reported in both units. ``base_volume`` and ``quote_volume`` are stored exactly as
the exchange reports them, and whichever side the exchange omits is derived from the mid
price. Getting this right matters twice: callers see comparable numbers, and
``MarketDataService._rebuild_price_pool`` picks the most liquid market for a pair by comparing
quote volume across exchanges.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Tuple,
    Union,
)

from config import settings

logger = logging.getLogger(__name__)

# Per-connector fetch timeout (seconds). One slow exchange must not stall the whole cycle.
# This budget covers the symbol map load plus the ticker request.
_FETCH_TIMEOUT = 30.0

# Symbol map load timeout, kept inside _FETCH_TIMEOUT. Some exchange-info endpoints are slow:
# gate_io_perpetual alone takes ~6s, and more when many connectors refresh concurrently.
_SYMBOL_MAP_TIMEOUT = 15.0

# The base get_last_traded_prices implementation issues ONE HTTP REQUEST PER PAIR. Above this
# many pairs the generic fallback refuses rather than firing hundreds of requests.
_MAX_LAST_TRADED_PAIRS = 30


class TickerFetchError(RuntimeError):
    """Ticker retrieval failed for a connector (network, parse, or empty symbol map)."""


class TickerUnsupportedError(TickerFetchError):
    """This connector cannot serve public tickers through a keyless data connector."""


class Ticker:
    """A single normalized ticker: mid price plus 24h base and quote volume."""

    __slots__ = ("price", "base_volume", "quote_volume", "timestamp")

    def __init__(
        self,
        price: Decimal,
        base_volume: Optional[Decimal] = None,
        quote_volume: Optional[Decimal] = None,
        timestamp: float = 0.0,
    ):
        self.price = price
        self.base_volume = base_volume
        self.quote_volume = quote_volume
        self.timestamp = timestamp

        # Whichever side the exchange omits is derived from the mid price, so that exchanges
        # reporting only one unit still expose both. Exchange-reported values are never
        # overwritten, so a derived number only ever fills a gap.
        if price is not None and price > 0:
            try:
                if self.quote_volume is None and self.base_volume is not None:
                    self.quote_volume = self.base_volume * price
                elif self.base_volume is None and self.quote_volume is not None:
                    self.base_volume = self.quote_volume / price
            except (InvalidOperation, DivisionByZero, ArithmeticError):
                pass

    def to_dict(self) -> Dict[str, Any]:
        return {
            "price": float(self.price),
            "base_volume": float(self.base_volume) if self.base_volume is not None else None,
            "quote_volume": float(self.quote_volume) if self.quote_volume is not None else None,
            "timestamp": self.timestamp,
        }


def _to_decimal(value: Any) -> Optional[Decimal]:
    """Parse a value to a Decimal, returning None on empty or invalid input."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _mid(bid: Any, ask: Any, last: Any) -> Optional[Decimal]:
    """Mid price from bid/ask when both are positive, otherwise fall back to last price."""
    bid_d = _to_decimal(bid)
    ask_d = _to_decimal(ask)
    if bid_d is not None and ask_d is not None and bid_d > 0 and ask_d > 0:
        return (bid_d + ask_d) / Decimal("2")
    last_d = _to_decimal(last)
    if last_d is not None and last_d > 0:
        return last_d
    return None


# ==================== Declarative spec plumbing ====================

# A field location inside a ticker row: a plain key, or a path mixing keys and list indices
# (e.g. ``("bid", 0)`` for AscendEx's ``[price, size]`` pairs, ``("v", 1)`` for Kraken's
# ``[today, last_24h]`` volume pairs).
FieldPath = Union[str, Tuple[Any, ...], None]


def _get(row: Dict[str, Any], path: FieldPath) -> Any:
    """Read a value from a row by key or nested path. Returns None if any step is missing."""
    if path is None:
        return None
    if isinstance(path, str):
        return row.get(path)
    node: Any = row
    for step in path:
        if isinstance(step, int):
            if not isinstance(node, (list, tuple)) or len(node) <= step:
                return None
            node = node[step]
        else:
            if not isinstance(node, dict):
                return None
            node = node.get(step)
        if node is None:
            return None
    return node


def _rows_list(payload: Any) -> List[Dict[str, Any]]:
    """The payload is already a flat list of rows."""
    return [r for r in payload if isinstance(r, dict)] if isinstance(payload, list) else []


def _rows_at(*keys: str) -> Callable[[Any], List[Dict[str, Any]]]:
    """The rows live at a nested key path, e.g. ``data`` or ``result.list``."""

    def shaper(payload: Any) -> List[Dict[str, Any]]:
        node = payload
        for key in keys:
            if not isinstance(node, dict):
                return []
            node = node.get(key)
        return [r for r in node if isinstance(r, dict)] if isinstance(node, list) else []

    return shaper


def _rows_keyed_dict(*keys: str, key_field: str = "_key") -> Callable[[Any], List[Dict[str, Any]]]:
    """The rows are a symbol-keyed dict-of-dicts; the key is injected as ``key_field``."""

    def shaper(payload: Any) -> List[Dict[str, Any]]:
        node = payload
        for key in keys:
            if not isinstance(node, dict):
                return []
            node = node.get(key)
        if not isinstance(node, dict):
            return []
        return [{**value, key_field: key} for key, value in node.items() if isinstance(value, dict)]

    return shaper


@dataclass(frozen=True)
class TickerSpec:
    """Declarative description of one exchange's bulk ticker endpoint.

    ``limit_id`` is deliberately absent. ExchangePyBase._api_request falls back to
    ``throttler_limit_id = limit_id or path_url`` and every path below is registered as a
    RateLimit id under exactly that name. The connectors that override _api_request
    (okx_perpetual, bybit_perpetual) compute their own limit id only when limit_id is None,
    so passing one would bypass their throttling.
    """

    # --- request ---
    path: Union[str, Dict[str, str], Callable[[], Any]]
    method: str = "GET"  # "GET" or "POST"
    params: Optional[Dict[str, Any]] = None
    data: Optional[Dict[str, Any]] = None  # POST body
    headers: Optional[Dict[str, str]] = None
    # --- response shape ---
    rows: Callable[[Any], List[Dict[str, Any]]] = field(default=_rows_list)
    # --- field mapping ---
    symbol: FieldPath = "symbol"
    bid: FieldPath = None
    ask: FieldPath = None
    last: FieldPath = None
    base_volume: FieldPath = None  # 24h volume denominated in the BASE asset
    quote_volume: FieldPath = None  # 24h volume denominated in the QUOTE asset


def _bybit_perpetual_path() -> Dict[str, str]:
    """Bybit perpetual's _api_request indexes the endpoint by market, so it needs the dict."""
    from hummingbot.connector.derivative.bybit_perpetual import bybit_perpetual_constants

    return bybit_perpetual_constants.LATEST_SYMBOL_INFORMATION_ENDPOINT


# Bulk ticker endpoints, with every price and volume field verified against the live payload.
# Adding an exchange is normally just one more row here.
TICKER_SPECS: Dict[str, TickerSpec] = {
    "binance": TickerSpec(
        path="/ticker/24hr",
        symbol="symbol", bid="bidPrice", ask="askPrice", last="lastPrice",
        base_volume="volume", quote_volume="quoteVolume",
    ),
    "bing_x": TickerSpec(
        path="/openApi/spot/v1/ticker/24hr", rows=_rows_at("data"),
        symbol="symbol", bid="bidPrice", ask="askPrice", last="lastPrice",
        base_volume="volume", quote_volume="quoteVolume",
    ),
    # The futures 24hr ticker carries no bid/ask, so the price falls back to lastPrice.
    "binance_perpetual": TickerSpec(
        path="v1/ticker/24hr",  # the base URL already carries /fapi/
        symbol="symbol", last="lastPrice",
        base_volume="volume", quote_volume="quoteVolume",
    ),
    "gate_io": TickerSpec(
        path="spot/tickers",
        symbol="currency_pair", bid="highest_bid", ask="lowest_ask", last="last",
        base_volume="base_volume", quote_volume="quote_volume",
    ),
    "gate_io_perpetual": TickerSpec(
        path="futures/usdt/tickers",
        symbol="contract", bid="highest_bid", ask="lowest_ask", last="last",
        base_volume="volume_24h_base", quote_volume="volume_24h_quote",
    ),
    "okx": TickerSpec(
        path="/api/v5/market/tickers", params={"instType": "SPOT"}, rows=_rows_at("data"),
        symbol="instId", bid="bidPx", ask="askPx", last="last",
        base_volume="vol24h", quote_volume="volCcy24h",
    ),
    # Unit trap: for instType=SWAP, volCcy24h is BASE-currency volume (unlike SPOT, where it is
    # quote volume) and vol24h counts contracts. Only the base side is trustworthy here, so the
    # quote volume is derived from it.
    "okx_perpetual": TickerSpec(
        path="/api/v5/market/tickers", params={"instType": "SWAP"}, rows=_rows_at("data"),
        symbol="instId", bid="bidPx", ask="askPx", last="last",
        base_volume="volCcy24h",
    ),
    "kucoin": TickerSpec(
        path="/api/v1/market/allTickers", rows=_rows_at("data", "ticker"),
        symbol="symbol", bid="buy", ask="sell", last="last",
        base_volume="vol", quote_volume="volValue",
    ),
    "bybit": TickerSpec(
        path="/v5/market/tickers", params={"category": "spot"}, rows=_rows_at("result", "list"),
        symbol="symbol", bid="bid1Price", ask="ask1Price", last="lastPrice",
        base_volume="volume24h", quote_volume="turnover24h",
    ),
    "bybit_perpetual": TickerSpec(
        path=_bybit_perpetual_path, params={"category": "linear"},
        rows=_rows_at("result", "list"),
        symbol="symbol", bid="bid1Price", ask="ask1Price", last="lastPrice",
        base_volume="volume24h", quote_volume="turnover24h",
    ),
    "mexc": TickerSpec(
        path="/ticker/24hr", headers={"Content-Type": "application/json"},
        symbol="symbol", bid="bidPrice", ask="askPrice", last="lastPrice",
        base_volume="volume", quote_volume="quoteVolume",
    ),
    "htx": TickerSpec(
        path="/market/tickers", rows=_rows_at("data"),
        symbol="symbol", bid="bid", ask="ask", last="close",
        base_volume="amount", quote_volume="vol",
    ),
    # Unit fix: AscendEx's `volume` is BASE volume. It used to be stored as quote volume.
    "ascend_ex": TickerSpec(
        path="spot/ticker", rows=_rows_at("data"),
        symbol="symbol", bid=("bid", 0), ask=("ask", 0),
        base_volume="volume",
    ),
    # Kraken returns a symbol-keyed dict of dicts, already unwrapped from `result` by the
    # connector's REST post-processor. `v` and `p` are [today, last_24h] pairs, so the 24h
    # base volume is v[1]. Kraken reports no quote volume, so it is derived.
    "kraken": TickerSpec(
        path="/0/public/Ticker", rows=_rows_keyed_dict(),
        symbol="_key", bid=("b", 0), ask=("a", 0), last=("c", 0),
        base_volume=("v", 1),
    ),
}

# Mainnet connector variants that share another connector's spec/adapter. Testnets are
# deliberately left out: their prices and volumes are not real market data, so they have no
# business in the pool that backs cross-rate pricing.
_SPEC_ALIASES: Dict[str, str] = {
    "kucoin_hft": "kucoin",
}

# Known connectors that cannot serve public tickers without credentials or a running data
# source. The reason is surfaced verbatim to the caller, since retrying will never help.
UNSUPPORTED_TICKER_CONNECTORS: Dict[str, str] = {
    "coinbase_advanced_trade": "requires API credentials even for public product data",
    "decibel_perpetual": "requires API credentials on every endpoint",
    "architect_perpetual": "requires API credentials for its public endpoints",
    "injective_v2": "needs a running Injective data source rather than a REST ticker endpoint",
    "injective_v2_perpetual": "needs a running Injective data source rather than a REST ticker endpoint",
    "xrpl": "needs a running XRPL node pool rather than a REST ticker endpoint",
    "dydx_v4_perpetual": "needs a running indexer data source rather than a REST ticker endpoint",
}


def _canonical(connector_name: str) -> str:
    """Resolve a connector name through the alias table."""
    return _SPEC_ALIASES.get(connector_name, connector_name)


# ==================== Normalization ====================

def _normalize(
    symbol_map: Mapping[str, str],
    rows: Iterable[Dict[str, Any]],
    extract: Callable[
        [Dict[str, Any]],
        Tuple[Any, Optional[Decimal], Optional[Decimal], Optional[Decimal]],
    ],
    connector_name: str,
    timestamp: Optional[float] = None,
) -> Dict[str, Ticker]:
    """
    Map raw rows to ``{hb_pair -> Ticker}``.

    ``extract`` returns ``(raw_symbol, price, base_volume, quote_volume)`` for a row. Rows whose
    symbol the connector does not track, or with no usable price, are skipped; each row is
    guarded so one malformed entry cannot abort the whole exchange.

    ``timestamp`` overrides the collection time, for rows served from a slower-refreshing
    snapshot; it defaults to now.

    Raises TickerFetchError when rows were present but none matched the symbol map, which is
    what a payload/symbol-map key mismatch looks like. Silently returning {} there would show
    up as an empty 200 with no clue as to why.
    """
    now = time.time() if timestamp is None else timestamp
    out: Dict[str, Ticker] = {}
    seen = 0
    unmapped = 0
    sample_unmapped: List[str] = []

    for row in rows:
        seen += 1
        try:
            raw_symbol, price, base_volume, quote_volume = extract(row)
            if price is None or price <= 0:
                continue
            pair = symbol_map.get(raw_symbol) if isinstance(raw_symbol, str) else None
            if pair is None:
                unmapped += 1
                if len(sample_unmapped) < 3:
                    sample_unmapped.append(str(raw_symbol))
                continue
            if base_volume is not None and base_volume < 0:
                base_volume = None
            if quote_volume is not None and quote_volume < 0:
                quote_volume = None
        except Exception as e:  # noqa: BLE001 - never let one bad row kill the batch
            logger.debug(f"[{connector_name}] skipping ticker row {row!r}: {e}")
            continue
        out[pair] = Ticker(
            price=price, base_volume=base_volume, quote_volume=quote_volume, timestamp=now
        )

    if seen and not out:
        raise TickerFetchError(
            f"'{connector_name}': none of {seen} ticker rows matched the symbol map "
            f"({len(symbol_map)} entries). Sample payload symbols={sample_unmapped}, "
            f"sample map keys={list(symbol_map)[:3]}"
        )
    if unmapped:
        logger.info(
            f"[{connector_name}] tickers: mapped {len(out)}/{seen} rows "
            f"({unmapped} symbols not in the symbol map, e.g. {sample_unmapped})"
        )
    return out


async def _load_symbol_map(connector, connector_name: str) -> Dict[str, str]:
    """
    Force the connector's ``{exchange symbol -> hb pair}`` map to initialize, once per fetch.

    ``trading_pair_symbol_map()`` initializes itself lazily from a public endpoint, so this
    works on a keyless, never-started data connector. It is loaded up front rather than per
    row because ``_initialize_trading_pair_symbol_map`` swallows its exceptions: on failure the
    map stays empty and every per-row lookup would retry the failed request under a lock and
    then raise KeyError, leaving no trace of the real cause.
    """
    try:
        mapping = await asyncio.wait_for(
            connector.trading_pair_symbol_map(), timeout=_SYMBOL_MAP_TIMEOUT
        )
    except asyncio.TimeoutError as e:
        raise TickerFetchError(
            f"'{connector_name}': timed out loading the trading pair symbol map"
        ) from e
    except TickerFetchError:
        raise
    except Exception as e:  # noqa: BLE001
        raise TickerFetchError(
            f"'{connector_name}': failed to load the trading pair symbol map: {e}"
        ) from e

    if not mapping:
        # An empty map is the only signal that the exchange-info request failed, since
        # _initialize_trading_pair_symbol_map logs and swallows the underlying exception.
        raise TickerFetchError(
            f"'{connector_name}': trading pair symbol map is empty. The exchange-info request "
            f"failed (check the connector log for 'There was an error requesting exchange info')"
        )
    return dict(mapping)


# ==================== Spec-driven adapter ====================

async def _request(connector, spec: TickerSpec) -> List[Dict[str, Any]]:
    """Issue the spec's request through the connector and shape the payload into rows."""
    path = spec.path() if callable(spec.path) else spec.path
    kwargs: Dict[str, Any] = {"path_url": path, "is_auth_required": False}
    if spec.params:
        kwargs["params"] = dict(spec.params)
    if spec.data:
        kwargs["data"] = dict(spec.data)
    if spec.headers:
        kwargs["headers"] = dict(spec.headers)
    call = connector._api_post if spec.method == "POST" else connector._api_get
    return spec.rows(await call(**kwargs))


def _spec_extract(spec: TickerSpec):
    """Build the per-row extractor described by a spec."""

    def extract(row: Dict[str, Any]):
        return (
            _get(row, spec.symbol),
            _mid(_get(row, spec.bid), _get(row, spec.ask), _get(row, spec.last)),
            _to_decimal(_get(row, spec.base_volume)),
            _to_decimal(_get(row, spec.quote_volume)),
        )

    return extract


# ==================== Hand-written adapters ====================
# Signature: async def adapter(connector, symbol_map) -> Dict[str, Ticker]

async def _hyperliquid(connector, symbol_map: Mapping[str, str]) -> Dict[str, Ticker]:
    """Hyperliquid spot.

    ``spotMetaAndAssetCtxs`` returns ``[meta, assetCtxs]``, and the two are NOT the same length
    (universe holds the tradable pairs, assetCtxs is longer), so they must not be zipped. Each
    ctx carries ``coin``, which is exactly the key the connector's symbol map is built on.
    """
    payload = await connector._api_post(path_url="/info", data={"type": "spotMetaAndAssetCtxs"})
    ctxs = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
    rows = [c for c in ctxs if isinstance(c, dict)]

    def extract(row):
        return (
            row.get("coin"),
            _mid(None, None, row.get("midPx")) or _to_decimal(row.get("markPx")),
            _to_decimal(row.get("dayBaseVlm")),
            _to_decimal(row.get("dayNtlVlm")),
        )

    return _normalize(symbol_map, rows, extract, "hyperliquid")


def _hyperliquid_perp_extract(row):
    """Price/volume out of a Hyperliquid perp asset context (main dex or HIP-3)."""
    return (
        row.get("_name"),
        _mid(None, None, row.get("midPx")) or _to_decimal(row.get("markPx")),
        _to_decimal(row.get("dayBaseVlm")),
        _to_decimal(row.get("dayNtlVlm")),
    )


# HIP-3 snapshots per connector name: {name: (fetched_at, rows)}. Shared exchange-wide data,
# so the connector instance it came from does not matter.
_hip3_snapshots: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}


async def _hyperliquid_hip3_tickers(
    connector, symbol_map: Mapping[str, str], connector_name: str
) -> Dict[str, Ticker]:
    """
    Tickers for HIP-3 builder-deployed perp dexes (``xyz:TSLA``, ``flx:...``, ...).

    These live on separate dexes and are absent from ``metaAndAssetCtxs``; collecting them
    costs one ``allPerpMetas`` call plus one ``metaAndAssetCtxs`` per dex (~10 requests, ~4s).
    That is too expensive for every cycle, so the snapshot is refreshed on its own interval
    and reused in between -- HIP-3 prices are therefore up to that interval old, which the
    returned tickers report honestly via their timestamp.

    Never raises: HIP-3 is supplementary, so any failure leaves the main dex tickers intact.
    """
    interval = settings.market_data.hyperliquid_hip3_interval
    if interval <= 0:
        return {}
    if not hasattr(connector, "_fetch_and_cache_hip3_market_data"):
        return {}  # hummingbot build without HIP-3 support

    now = time.time()
    snapshot = _hip3_snapshots.get(connector_name)
    if snapshot is None or now - snapshot[0] > interval:
        try:
            # Loading the symbol map already fetched and cached the dex markets, so reuse
            # those on the first pass instead of paying for the same ~10 requests twice.
            cached = getattr(connector, "_dex_markets", None)
            dex_markets = cached if (snapshot is None and cached) else \
                await connector._fetch_and_cache_hip3_market_data()
            rows = [
                {**row, "_name": row.get("name")}
                for row in connector._iter_hip3_merged_markets(dex_markets=dex_markets)
                if isinstance(row, dict)
            ]
            snapshot = (now, rows)
            _hip3_snapshots[connector_name] = snapshot
        except Exception as e:  # noqa: BLE001
            if snapshot is None:
                logger.warning(f"[{connector_name}] HIP-3 market fetch failed: {e}")
                return {}
            logger.warning(f"[{connector_name}] HIP-3 refresh failed, serving last snapshot: {e}")

    fetched_at, rows = snapshot
    if not rows:
        return {}
    try:
        return _normalize(
            symbol_map, rows, _hyperliquid_perp_extract, f"{connector_name}:hip3",
            timestamp=fetched_at,
        )
    except TickerFetchError as e:
        logger.warning(f"[{connector_name}] HIP-3 markets could not be mapped: {e}")
        return {}


async def _hyperliquid_perpetual(connector, symbol_map: Mapping[str, str]) -> Dict[str, Ticker]:
    """Hyperliquid perpetuals, main dex plus HIP-3 builder dexes.

    ``metaAndAssetCtxs`` returns ``[meta, assetCtxs]`` of equal length, index-aligned, and the
    ctxs carry no symbol of their own -- zipping against ``meta["universe"]`` is the only join.
    A length mismatch would silently attach every price to the wrong market, so it raises.
    """
    payload = await connector._api_post(path_url="/info", data={"type": "metaAndAssetCtxs"})
    meta = payload[0].get("universe", []) if isinstance(payload, list) and payload else []
    ctxs = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
    if len(meta) != len(ctxs):
        raise TickerFetchError(
            f"hyperliquid_perpetual: universe/assetCtxs length mismatch "
            f"({len(meta)} vs {len(ctxs)}); refusing to zip misaligned prices"
        )
    rows = [
        {**ctx, "_name": entry.get("name")}
        for entry, ctx in zip(meta, ctxs)
        if isinstance(entry, dict) and isinstance(ctx, dict)
    ]

    tickers = _normalize(symbol_map, rows, _hyperliquid_perp_extract, "hyperliquid_perpetual")
    tickers.update(await _hyperliquid_hip3_tickers(connector, symbol_map, "hyperliquid_perpetual"))
    return tickers


TICKER_ADAPTERS: Dict[
    str, Callable[[Any, Mapping[str, str]], Awaitable[Dict[str, Ticker]]]
] = {
    "hyperliquid": _hyperliquid,
    "hyperliquid_perpetual": _hyperliquid_perpetual,
}


# ==================== Generic adapter (price-only, any connector) ====================

# Candidate field names, ordered by preference, used to parse arbitrary exchange ticker rows.
_SYMBOL_KEYS = ("symbol", "currency_pair", "instId", "symbolName", "trading_pair", "market", "pair", "s")
_LAST_KEYS = ("last", "lastPrice", "lastPr", "close", "price", "c", "lastTradeRate")
_BID_KEYS = ("bidPrice", "highest_bid", "bidPx", "buy", "bestBid", "bid", "b")
_ASK_KEYS = ("askPrice", "lowest_ask", "askPx", "sell", "bestAsk", "ask", "a")


def _first(row: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    """Return the first present, non-empty value among ``keys`` (unwrapping [price, size] lists)."""
    for key in keys:
        if key in row and row[key] not in (None, ""):
            value = row[key]
            return value[0] if isinstance(value, (list, tuple)) and value else value
    return None


def _heuristic_rows(raw: Any) -> List[Dict[str, Any]]:
    """Coerce an arbitrary 'all tickers' payload into a flat list of row dicts."""
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict) and isinstance(data.get("ticker"), list):
            return [r for r in data["ticker"] if isinstance(r, dict)]
        # Symbol-keyed dict-of-dicts (e.g. Kraken's {"result": {SYMBOL: {...}}}); inject the key.
        container = raw.get("result") if isinstance(raw.get("result"), dict) else raw
        rows = []
        for key, value in container.items():
            if isinstance(value, dict):
                rows.append({**value, "_key": key})
        return rows
    return []


def _has_bulk_last_traded_prices(connector) -> bool:
    """True when the connector overrides get_last_traded_prices with a single bulk call."""
    try:
        from hummingbot.connector.exchange_base import ExchangeBase

        return getattr(type(connector), "get_last_traded_prices", None) is not getattr(
            ExchangeBase, "get_last_traded_prices", None
        )
    except Exception:  # noqa: BLE001
        return False


async def _generic(connector, connector_name: str, symbol_map: Mapping[str, str]) -> Dict[str, Ticker]:
    """
    Price-only adapter for any connector without a spec or dedicated adapter.

    Prefers the single bulk ``get_all_pairs_prices()`` call, heuristically parsed and mapped
    through the symbol map. If that endpoint is unavailable, falls back to
    ``get_last_traded_prices()`` over the connector's known pairs.

    Volume is deliberately left unset on this path: guessing a volume field name risks silently
    mixing base and quote units, which is worse than reporting nothing.
    """
    # 1. Bulk all-pairs endpoint with heuristic parsing.
    try:
        rows = _heuristic_rows(await connector.get_all_pairs_prices())
        if rows:
            tickers = _normalize(
                symbol_map, rows,
                lambda r: (
                    _first(r, _SYMBOL_KEYS) or r.get("_key"),
                    _mid(_first(r, _BID_KEYS), _first(r, _ASK_KEYS), _first(r, _LAST_KEYS)),
                    None,
                    None,
                ),
                connector_name,
            )
            if tickers:
                return tickers
    except (NotImplementedError, AttributeError):
        pass
    except TickerFetchError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Generic all-pairs fetch failed for '{connector_name}': {e}")

    # 2. Fallback: last traded prices over the known pairs (keys are already BASE-QUOTE).
    #    The pairs come from the symbol map, not connector.trading_rules, which stays empty
    #    forever on a keyless connector because _update_trading_rules is never awaited.
    pairs = sorted(set(symbol_map.values()))
    if not pairs:
        raise TickerFetchError(f"'{connector_name}': no trading pairs known")
    if not _has_bulk_last_traded_prices(connector) and len(pairs) > _MAX_LAST_TRADED_PAIRS:
        raise TickerUnsupportedError(
            f"'{connector_name}' has no bulk ticker endpoint; pricing its {len(pairs)} pairs "
            f"would take one HTTP request each. Add a TickerSpec for this connector."
        )

    prices = await connector.get_last_traded_prices(pairs)
    now = time.time()
    return {
        pair: Ticker(price=dec, timestamp=now)
        for pair, price in prices.items()
        if (dec := _to_decimal(price)) is not None and dec > 0
    }


# ==================== Entry point ====================

async def _fetch(connector, connector_name: str) -> Dict[str, Ticker]:
    """Load the symbol map, then dispatch to the adapter, spec or generic path."""
    symbol_map = await _load_symbol_map(connector, connector_name)
    name = _canonical(connector_name)

    adapter = TICKER_ADAPTERS.get(name)
    if adapter is not None:
        return await adapter(connector, symbol_map)

    spec = TICKER_SPECS.get(name)
    if spec is not None:
        rows = await _request(connector, spec)
        return _normalize(symbol_map, rows, _spec_extract(spec), connector_name)

    return await _generic(connector, connector_name, symbol_map)


async def fetch_tickers(
    connector, connector_name: str, *, raise_on_error: bool = False
) -> Dict[str, Ticker]:
    """
    Fetch and normalize tickers for one connector. Returns ``{hb_pair -> Ticker}``.

    Uses the dedicated adapter or spec when one is registered (price plus 24h base/quote
    volume), otherwise the generic price-only adapter.

    Args:
        raise_on_error: False (background collection) logs failures and returns an empty dict so
            one exchange cannot break the collection cycle. True (on-demand requests) raises
            TickerUnsupportedError or TickerFetchError so the caller can report why.
    """
    try:
        reason = UNSUPPORTED_TICKER_CONNECTORS.get(_canonical(connector_name))
        if reason:
            raise TickerUnsupportedError(f"'{connector_name}' {reason}")
        return await asyncio.wait_for(_fetch(connector, connector_name), timeout=_FETCH_TIMEOUT)
    except asyncio.TimeoutError:
        if raise_on_error:
            raise TickerFetchError(f"Ticker fetch timed out for '{connector_name}'")
        logger.warning(f"Ticker fetch timed out for '{connector_name}'")
        return {}
    except Exception as e:  # noqa: BLE001 - one exchange failing must not break the cycle
        if raise_on_error:
            raise e if isinstance(e, TickerFetchError) else TickerFetchError(str(e)) from e
        logger.warning(f"Failed to fetch tickers for '{connector_name}': {e}")
        return {}
