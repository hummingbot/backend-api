"""
Tests for the ticker source adapters and the on-demand ticker fetch.

The volume-unit assertions here are regressions: `ascend_ex` and `okx_perpetual` both report
BASE volume in a field that used to be stored as quote volume, which made cross-exchange
liquidity comparison meaningless.

Run with: pytest test/test_ticker_sources.py -v
"""
from decimal import Decimal
from typing import Any, Dict

import pytest
from hummingbot.connector.exchange_base import ExchangeBase

from services.ticker_sources import (
    TICKER_SPECS,
    Ticker,
    TickerFetchError,
    TickerUnsupportedError,
    _generic,
    _normalize,
    _request,
    _spec_extract,
    fetch_tickers,
)


class FakeConnector:
    """Minimal stand-in for a hummingbot connector: canned payloads plus a symbol map."""

    def __init__(self, symbol_map: Dict[str, str], get_payload=None, post_payload=None):
        self._symbol_map = symbol_map
        self._get_payload = get_payload
        self._post_payload = post_payload
        self.get_calls = []
        self.post_calls = []

    async def trading_pair_symbol_map(self):
        return self._symbol_map

    async def _api_get(self, **kwargs):
        self.get_calls.append(kwargs)
        payload = self._get_payload
        return payload(kwargs) if callable(payload) else payload

    async def _api_post(self, **kwargs):
        self.post_calls.append(kwargs)
        payload = self._post_payload
        return payload(kwargs) if callable(payload) else payload


async def run_spec(connector_name: str, connector: FakeConnector) -> Dict[str, Ticker]:
    """Drive one spec end to end, the way _fetch does."""
    spec = TICKER_SPECS[connector_name]
    rows = await _request(connector, spec)
    return _normalize(connector.symbol_map_for_test, rows, _spec_extract(spec), connector_name)


# Attach the map used by run_spec without threading it through every call site.
FakeConnector.symbol_map_for_test = property(lambda self: self._symbol_map)


# ==================== Ticker volume derivation ====================

def test_quote_volume_derived_from_base():
    t = Ticker(price=Decimal("100"), base_volume=Decimal("5"), timestamp=1.0)
    assert t.quote_volume == Decimal("500")


def test_base_volume_derived_from_quote():
    t = Ticker(price=Decimal("100"), quote_volume=Decimal("500"), timestamp=1.0)
    assert t.base_volume == Decimal("5")


def test_reported_volumes_are_never_overwritten():
    t = Ticker(
        price=Decimal("100"), base_volume=Decimal("5"), quote_volume=Decimal("999"), timestamp=1.0
    )
    assert t.base_volume == Decimal("5")
    assert t.quote_volume == Decimal("999")


# ==================== Spec-driven adapters ====================

@pytest.mark.asyncio
async def test_binance_reports_both_volumes():
    connector = FakeConnector(
        {"BTCUSDT": "BTC-USDT"},
        get_payload=[{
            "symbol": "BTCUSDT", "bidPrice": "100.0", "askPrice": "102.0",
            "lastPrice": "101.5", "volume": "10", "quoteVolume": "1010",
        }],
    )
    tickers = await run_spec("binance", connector)
    ticker = tickers["BTC-USDT"]
    assert ticker.price == Decimal("101")  # mid of bid/ask, not lastPrice
    assert ticker.base_volume == Decimal("10")
    assert ticker.quote_volume == Decimal("1010")


@pytest.mark.asyncio
async def test_binance_perpetual_falls_back_to_last_price():
    # The futures 24hr ticker carries no bid/ask.
    connector = FakeConnector(
        {"BTCUSDT": "BTC-USDT"},
        get_payload=[{
            "symbol": "BTCUSDT", "lastPrice": "63457.9",
            "volume": "162487.091", "quoteVolume": "10447087916.01",
        }],
    )
    tickers = await run_spec("binance_perpetual", connector)
    assert tickers["BTC-USDT"].price == Decimal("63457.9")
    assert tickers["BTC-USDT"].quote_volume == Decimal("10447087916.01")


@pytest.mark.asyncio
async def test_ascend_ex_volume_is_base_not_quote():
    """Regression: `volume` is BASE volume; it used to be stored as the quote volume."""
    connector = FakeConnector(
        {"BTC/USDT": "BTC-USDT"},
        get_payload={"data": [{
            "symbol": "BTC/USDT", "bid": ["100.0", "3"], "ask": ["102.0", "4"], "volume": "10",
        }]},
    )
    tickers = await run_spec("ascend_ex", connector)
    ticker = tickers["BTC-USDT"]
    assert ticker.base_volume == Decimal("10")
    assert ticker.quote_volume == Decimal("1010")  # 10 * mid(101)


@pytest.mark.asyncio
async def test_okx_perpetual_volccy_is_base_and_vol24h_is_ignored():
    """Regression: for instType=SWAP, volCcy24h is BASE volume and vol24h counts contracts."""
    connector = FakeConnector(
        {"BTC-USDT-SWAP": "BTC-USDT"},
        get_payload={"data": [{
            "instId": "BTC-USDT-SWAP", "bidPx": "100.0", "askPx": "102.0", "last": "101.5",
            "vol24h": "9999999", "volCcy24h": "10",
        }]},
    )
    tickers = await run_spec("okx_perpetual", connector)
    ticker = tickers["BTC-USDT"]
    assert ticker.base_volume == Decimal("10")  # volCcy24h, never vol24h
    assert ticker.quote_volume == Decimal("1010")


@pytest.mark.asyncio
async def test_okx_spot_volccy_is_quote_volume():
    """The same field means quote volume on SPOT, which is why the two specs differ."""
    connector = FakeConnector(
        {"BTC-USDT": "BTC-USDT"},
        get_payload={"data": [{
            "instId": "BTC-USDT", "bidPx": "100.0", "askPx": "102.0", "last": "101.5",
            "vol24h": "10", "volCcy24h": "1010",
        }]},
    )
    ticker = (await run_spec("okx", connector))["BTC-USDT"]
    assert ticker.base_volume == Decimal("10")
    assert ticker.quote_volume == Decimal("1010")


@pytest.mark.asyncio
async def test_bybit_perpetual_passes_dict_path_and_no_limit_id():
    """bybit_perpetual's _api_request indexes the endpoint by market, so it needs the dict."""
    connector = FakeConnector(
        {"BTCUSDT": "BTC-USDT"},
        get_payload={"result": {"list": [{
            "symbol": "BTCUSDT", "bid1Price": "100.0", "ask1Price": "102.0",
            "lastPrice": "101.5", "volume24h": "10", "turnover24h": "1010",
        }]}},
    )
    await run_spec("bybit_perpetual", connector)
    call = connector.get_calls[0]
    assert isinstance(call["path_url"], dict)
    assert call["params"] == {"category": "linear"}
    # The connector computes its own throttler id when limit_id is absent.
    assert "limit_id" not in call


@pytest.mark.asyncio
async def test_kraken_keyed_dict_and_24h_base_volume():
    # `v` and `p` are [today, last_24h] pairs, so the 24h figure is index 1.
    connector = FakeConnector(
        {"XBTUSDT": "BTC-USDT"},
        get_payload={"XBTUSDT": {
            "a": ["102.0", "1", "1.0"], "b": ["100.0", "1", "1.0"], "c": ["101.0", "0.5"],
            "v": ["3.0", "10.0"],
        }},
    )
    ticker = (await run_spec("kraken", connector))["BTC-USDT"]
    assert ticker.price == Decimal("101")
    assert ticker.base_volume == Decimal("10.0")
    assert ticker.quote_volume == Decimal("1010.0")


# ==================== Hyperliquid ====================

@pytest.mark.asyncio
async def test_hyperliquid_spot_keys_on_coin_not_index():
    """assetCtxs is longer than universe, so the two must not be zipped."""
    payload = [
        {"universe": [{"name": "PURR/USDC"}]},
        [
            {"coin": "@1", "midPx": "2.0", "dayBaseVlm": "5", "dayNtlVlm": "10"},
            {"coin": "PURR/USDC", "midPx": "0.065", "dayBaseVlm": "100", "dayNtlVlm": "6.5"},
        ],
    ]
    connector = FakeConnector(
        {"PURR/USDC": "PURR-USDC", "@1": "UBTC-USDC"}, post_payload=payload
    )
    tickers = await fetch_tickers(connector, "hyperliquid", raise_on_error=True)
    assert set(tickers) == {"PURR-USDC", "UBTC-USDC"}
    assert tickers["PURR-USDC"].price == Decimal("0.065")
    assert tickers["PURR-USDC"].base_volume == Decimal("100")
    assert tickers["PURR-USDC"].quote_volume == Decimal("6.5")
    assert connector.post_calls[0]["data"] == {"type": "spotMetaAndAssetCtxs"}


@pytest.mark.asyncio
async def test_hyperliquid_perpetual_zips_universe_with_ctxs():
    payload = [
        {"universe": [{"name": "BTC"}, {"name": "ETH"}]},
        [
            {"midPx": "63460.5", "dayBaseVlm": "35116.8", "dayNtlVlm": "2259842755.8"},
            {"midPx": "3000.0", "dayBaseVlm": "1000", "dayNtlVlm": "3000000"},
        ],
    ]
    connector = FakeConnector({"BTC": "BTC-USD", "ETH": "ETH-USD"}, post_payload=payload)
    tickers = await fetch_tickers(connector, "hyperliquid_perpetual", raise_on_error=True)
    assert tickers["BTC-USD"].price == Decimal("63460.5")
    assert tickers["BTC-USD"].quote_volume == Decimal("2259842755.8")
    assert tickers["ETH-USD"].price == Decimal("3000.0")
    assert connector.post_calls[0]["data"] == {"type": "metaAndAssetCtxs"}


class Hip3Connector(FakeConnector):
    """Hyperliquid perp connector exposing the HIP-3 dex-market hooks."""

    def __init__(self, symbol_map, post_payload, hip3_rows, fail=False):
        super().__init__(symbol_map, post_payload=post_payload)
        self._hip3_rows = hip3_rows
        self._dex_markets = None  # force a fetch rather than reusing the symbol-map cache
        self._fail = fail
        self.hip3_fetches = 0

    async def _fetch_and_cache_hip3_market_data(self):
        self.hip3_fetches += 1
        if self._fail:
            raise RuntimeError("allPerpMetas unavailable")
        return [{"name": "xyz"}]

    def _iter_hip3_merged_markets(self, dex_markets=None):
        return iter(self._hip3_rows)


PERP_PAYLOAD = [
    {"universe": [{"name": "BTC"}]},
    [{"midPx": "63460.5", "dayBaseVlm": "35116.8", "dayNtlVlm": "2259842755.8"}],
]
HIP3_ROWS = [{"name": "xyz:TSLA", "midPx": "400.0", "dayBaseVlm": "10", "dayNtlVlm": "4000"}]
HIP3_MAP = {"BTC": "BTC-USD", "xyz:TSLA": "XYZ:TSLA-USD"}


@pytest.fixture(autouse=True)
def _clear_hip3_snapshots():
    from services import ticker_sources

    ticker_sources._hip3_snapshots.clear()
    yield
    ticker_sources._hip3_snapshots.clear()


@pytest.mark.asyncio
async def test_hip3_markets_are_included():
    """HIP-3 builder dexes are absent from metaAndAssetCtxs and were being dropped entirely."""
    connector = Hip3Connector(HIP3_MAP, PERP_PAYLOAD, HIP3_ROWS)
    tickers = await fetch_tickers(connector, "hyperliquid_perpetual", raise_on_error=True)
    assert set(tickers) == {"BTC-USD", "XYZ:TSLA-USD"}
    assert tickers["XYZ:TSLA-USD"].price == Decimal("400.0")
    assert tickers["XYZ:TSLA-USD"].base_volume == Decimal("10")
    assert tickers["XYZ:TSLA-USD"].quote_volume == Decimal("4000")


@pytest.mark.asyncio
async def test_hip3_snapshot_is_reused_within_the_interval():
    """~10 requests per refresh is too expensive to repeat on every cycle."""
    connector = Hip3Connector(HIP3_MAP, PERP_PAYLOAD, HIP3_ROWS)
    for _ in range(3):
        tickers = await fetch_tickers(connector, "hyperliquid_perpetual", raise_on_error=True)
        assert "XYZ:TSLA-USD" in tickers
    assert connector.hip3_fetches == 1


@pytest.mark.asyncio
async def test_hip3_snapshot_refreshes_after_the_interval():
    from services import ticker_sources

    connector = Hip3Connector(HIP3_MAP, PERP_PAYLOAD, HIP3_ROWS)
    await fetch_tickers(connector, "hyperliquid_perpetual", raise_on_error=True)
    _, rows = ticker_sources._hip3_snapshots["hyperliquid_perpetual"]
    ticker_sources._hip3_snapshots["hyperliquid_perpetual"] = (0.0, rows)  # expire it
    await fetch_tickers(connector, "hyperliquid_perpetual", raise_on_error=True)
    assert connector.hip3_fetches == 2


@pytest.mark.asyncio
async def test_hip3_failure_leaves_main_dex_intact():
    """HIP-3 is supplementary; losing it must not lose the 232 main-dex markets."""
    connector = Hip3Connector(HIP3_MAP, PERP_PAYLOAD, HIP3_ROWS, fail=True)
    tickers = await fetch_tickers(connector, "hyperliquid_perpetual", raise_on_error=True)
    assert set(tickers) == {"BTC-USD"}


@pytest.mark.asyncio
async def test_hip3_can_be_disabled(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings.market_data, "hyperliquid_hip3_interval", 0)
    connector = Hip3Connector(HIP3_MAP, PERP_PAYLOAD, HIP3_ROWS)
    tickers = await fetch_tickers(connector, "hyperliquid_perpetual", raise_on_error=True)
    assert set(tickers) == {"BTC-USD"}
    assert connector.hip3_fetches == 0


@pytest.mark.asyncio
async def test_hyperliquid_perpetual_refuses_misaligned_payload():
    """A length mismatch would attach every price to the wrong market."""
    payload = [{"universe": [{"name": "BTC"}, {"name": "ETH"}]}, [{"midPx": "1"}]]
    connector = FakeConnector({"BTC": "BTC-USD"}, post_payload=payload)
    with pytest.raises(TickerFetchError, match="length mismatch"):
        await fetch_tickers(connector, "hyperliquid_perpetual", raise_on_error=True)


# ==================== Error paths ====================

@pytest.mark.asyncio
async def test_empty_symbol_map_raises_with_a_usable_message():
    connector = FakeConnector({}, get_payload=[])
    with pytest.raises(TickerFetchError, match="symbol map is empty"):
        await fetch_tickers(connector, "binance", raise_on_error=True)


@pytest.mark.asyncio
async def test_rows_present_but_none_mapped_raises_with_samples():
    connector = FakeConnector(
        {"BTCUSDT": "BTC-USDT"},
        get_payload=[{"symbol": "NOPE", "lastPrice": "1", "quoteVolume": "1"}],
    )
    with pytest.raises(TickerFetchError) as excinfo:
        await fetch_tickers(connector, "binance", raise_on_error=True)
    message = str(excinfo.value)
    assert "NOPE" in message and "BTCUSDT" in message


@pytest.mark.asyncio
async def test_unsupported_connector_is_rejected_before_any_request():
    connector = FakeConnector({"X": "X-USD"})
    with pytest.raises(TickerUnsupportedError, match="requires API credentials"):
        await fetch_tickers(connector, "coinbase_advanced_trade", raise_on_error=True)
    assert connector.get_calls == [] and connector.post_calls == []


@pytest.mark.asyncio
async def test_background_mode_swallows_errors():
    connector = FakeConnector({}, get_payload=[])
    assert await fetch_tickers(connector, "binance") == {}


# ==================== Generic fallback ====================

class NoBulkConnector(FakeConnector):
    """A connector using the base one-request-per-pair get_last_traded_prices."""

    # Binding the base implementation makes _has_bulk_last_traded_prices report False.
    get_last_traded_prices = ExchangeBase.get_last_traded_prices

    async def get_all_pairs_prices(self):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_generic_refuses_to_fan_out_one_request_per_pair():
    symbol_map = {f"SYM{i}": f"SYM{i}-USDT" for i in range(500)}
    connector = NoBulkConnector(symbol_map)
    with pytest.raises(TickerUnsupportedError, match="one HTTP request each"):
        await _generic(connector, "some_exchange", symbol_map)


@pytest.mark.asyncio
async def test_generic_uses_symbol_map_not_trading_rules():
    """trading_rules stays empty forever on a keyless connector, so pairs come from the map."""
    calls = {}

    class BulkConnector(FakeConnector):
        trading_rules: Dict[str, Any] = {}

        async def get_all_pairs_prices(self):
            raise NotImplementedError

        async def get_last_traded_prices(self, pairs):
            calls["pairs"] = pairs
            return {pair: "10" for pair in pairs}

    symbol_map = {"BTCUSDT": "BTC-USDT", "ETHUSDT": "ETH-USDT"}
    tickers = await _generic(BulkConnector(symbol_map), "some_exchange", symbol_map)
    assert sorted(calls["pairs"]) == ["BTC-USDT", "ETH-USDT"]
    assert tickers["BTC-USDT"].price == Decimal("10")
    assert tickers["BTC-USDT"].quote_volume is None  # never guessed on the generic path


# ==================== On-demand fetching in MarketDataService ====================

class StubConnectorService:
    """Enough of UnifiedConnectorService for the on-demand ticker path."""

    def __init__(self, known=("bybit",)):
        self._known = set(known)
        self._data_connectors: Dict[str, Any] = {}

    def is_known_connector(self, connector_name):
        return connector_name in self._known

    def get_best_connector_for_market(self, connector_name, account_name=None):
        self._data_connectors.setdefault(connector_name, object())
        return self._data_connectors[connector_name]

    def get_all_trading_connectors(self):
        return {}


def make_service(monkeypatch, fetch_impl, known=("bybit",)):
    from services import market_data_service as mds

    monkeypatch.setattr(mds, "fetch_tickers", fetch_impl)
    return mds.MarketDataService(connector_service=StubConnectorService(known))


@pytest.mark.asyncio
async def test_concurrent_requests_trigger_a_single_upstream_fetch(monkeypatch):
    import asyncio

    calls = []

    async def slow_fetch(connector, connector_name, *, raise_on_error=False):
        calls.append(connector_name)
        await asyncio.sleep(0.05)
        return {"BTC-USDT": Ticker(price=Decimal("100"), timestamp=1.0)}

    service = make_service(monkeypatch, slow_fetch)
    results = await asyncio.gather(
        *[service.fetch_connector_tickers("bybit") for _ in range(10)]
    )
    assert len(calls) == 1
    assert all(r["BTC-USDT"].price == Decimal("100") for r in results)


@pytest.mark.asyncio
async def test_fresh_cache_is_served_and_force_refetches(monkeypatch):
    calls = []

    async def fetch(connector, connector_name, *, raise_on_error=False):
        calls.append(connector_name)
        return {"BTC-USDT": Ticker(price=Decimal("100"), timestamp=1.0)}

    service = make_service(monkeypatch, fetch)
    await service.fetch_connector_tickers("bybit")
    await service.fetch_connector_tickers("bybit")  # served from cache
    assert len(calls) == 1

    await service.fetch_connector_tickers("bybit", force=True)
    assert len(calls) == 2

    await service.fetch_connector_tickers("bybit", max_age=0)  # stale immediately
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_unknown_connector_raises(monkeypatch):
    from services.unified_connector_service import UnknownConnectorError

    async def fetch(connector, connector_name, *, raise_on_error=False):
        raise AssertionError("must not reach the connector")

    service = make_service(monkeypatch, fetch)
    with pytest.raises(UnknownConnectorError):
        await service.fetch_connector_tickers("not_a_real_exchange")


def test_paper_trade_and_testnet_are_not_market_data_connectors():
    from services.market_data_service import is_market_data_connector

    assert is_market_data_connector("binance") is True
    assert is_market_data_connector("hyperliquid_perpetual") is True
    assert is_market_data_connector("kucoin_hft") is True
    for excluded in (
        "binance_paper_trade", "hyperliquid_testnet", "bybit_perpetual_testnet",
        "architect_perpetual_sandbox",
    ):
        assert is_market_data_connector(excluded) is False, excluded


@pytest.mark.asyncio
async def test_testnet_request_is_refused_and_never_enters_the_pool(monkeypatch):
    """Serving a testnet would cache it and pull it into _rebuild_price_pool."""
    async def fetch(connector, connector_name, *, raise_on_error=False):
        raise AssertionError("must not fetch a testnet")

    service = make_service(monkeypatch, fetch, known=("bybit_testnet",))
    with pytest.raises(TickerUnsupportedError, match="not real market data"):
        await service.fetch_connector_tickers("bybit_testnet")
    assert service.get_tickers() == {}
    assert service.prices == {}


def test_collection_set_skips_paper_trade_and_testnet(monkeypatch):
    from services import market_data_service as mds

    service = mds.MarketDataService(connector_service=StubConnectorService())
    service._connector_service._data_connectors = {
        "binance": object(), "bybit_testnet": object(),
        "binance_paper_trade": object(), "okx": object(),
    }
    assert sorted(service._connected_connector_names()) == ["binance", "okx"]


@pytest.mark.asyncio
async def test_connector_construction_failure_is_a_clean_unsupported_error(monkeypatch):
    """A missing optional dependency must not surface as a raw 500."""
    async def fetch(connector, connector_name, *, raise_on_error=False):
        raise AssertionError("must not be reached")

    service = make_service(monkeypatch, fetch)

    def boom(connector_name, account_name=None):
        raise ModuleNotFoundError("No module named 'v4_proto'")

    monkeypatch.setattr(service._connector_service, "get_best_connector_for_market", boom)
    with pytest.raises(TickerUnsupportedError, match="cannot be instantiated"):
        await service.fetch_connector_tickers("bybit")


@pytest.mark.asyncio
async def test_on_demand_connector_joins_background_collection(monkeypatch):
    async def fetch(connector, connector_name, *, raise_on_error=False):
        return {"BTC-USDT": Ticker(price=Decimal("100"), timestamp=1.0)}

    service = make_service(monkeypatch, fetch)
    await service.fetch_connector_tickers("bybit")
    assert "bybit" in service._connected_connector_names()


def test_is_more_liquid_prefers_known_and_larger_quote_volume():
    from services.market_data_service import MarketDataService as MDS

    high = Ticker(price=Decimal("1"), quote_volume=Decimal("100"), timestamp=1.0)
    low = Ticker(price=Decimal("1"), quote_volume=Decimal("10"), timestamp=1.0)
    unknown_old = Ticker(price=Decimal("1"), timestamp=1.0)
    unknown_new = Ticker(price=Decimal("1"), timestamp=2.0)

    assert MDS._is_more_liquid(high, low) is True
    assert MDS._is_more_liquid(low, high) is False
    # A base-only exchange still participates instead of always ranking as zero.
    assert MDS._is_more_liquid(low, unknown_old) is True
    assert MDS._is_more_liquid(unknown_old, low) is False
    assert MDS._is_more_liquid(unknown_new, unknown_old) is True


# ==================== Merged /market-data/tickers endpoint ====================

def test_connector_filter_parsing():
    from routers.market_data import _requested_connectors

    assert _requested_connectors(None) == []
    assert _requested_connectors(["binance,okx"]) == ["binance", "okx"]      # comma-separated
    assert _requested_connectors(["binance", "okx"]) == ["binance", "okx"]   # repeated param
    assert _requested_connectors([" binance , okx "]) == ["binance", "okx"]  # whitespace
    assert _requested_connectors(["binance,,okx", "binance"]) == ["binance", "okx"]  # blanks + dupes


class StubMarketDataService:
    """Stands in for MarketDataService at the router boundary."""

    def __init__(self, pool=None, results=None, errors=None):
        self._pool = pool or {}
        self._results = results or {}
        self._errors = errors or {}
        self.fetch_calls = []

    def get_tickers(self):
        return self._pool

    def ticker_updated_at(self, connector_name):
        return 123.0

    def collected_connector_names(self):
        return sorted(self._pool)

    async def fetch_tickers_for(self, names, *, max_age=None, force=False):
        from services.unified_connector_service import UnknownConnectorError

        self.fetch_calls.append((tuple(names), force))
        unknown = [n for n in names if n not in self._results and n not in self._errors]
        if unknown:
            raise UnknownConnectorError(f"Connectors not found: {', '.join(unknown)}")
        return (
            {n: self._results[n] for n in names if n in self._results},
            {n: self._errors[n] for n in names if n in self._errors},
        )


def client_for(service):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from deps import get_market_data_service
    from routers import market_data

    app = FastAPI()
    app.include_router(market_data.router)
    app.dependency_overrides[get_market_data_service] = lambda: service
    return TestClient(app, raise_server_exceptions=False)


TICK = {"BTC-USDT": Ticker(price=Decimal("100"), quote_volume=Decimal("5"), timestamp=1.0)}


def test_no_filter_reads_the_pool_without_fetching():
    service = StubMarketDataService(pool={"binance": TICK, "okx": TICK})
    body = client_for(service).get("/market-data/tickers").json()
    assert set(body["tickers"]) == {"binance", "okx"}
    assert body["counts"] == {"binance": 1, "okx": 1}
    assert service.fetch_calls == []  # a plain pool read makes no requests


def test_multiple_connectors_are_fetched_together():
    service = StubMarketDataService(results={"binance": TICK, "okx": TICK, "htx": TICK})
    body = client_for(service).get("/market-data/tickers?connectors=binance,okx").json()
    assert set(body["tickers"]) == {"binance", "okx"}
    assert service.fetch_calls == [(("binance", "okx"), False)]  # one gathered call


def test_refresh_forces_a_fetch_of_the_whole_pool():
    service = StubMarketDataService(pool={"binance": TICK}, results={"binance": TICK})
    client_for(service).get("/market-data/tickers?refresh=true")
    assert service.fetch_calls == [(("binance",), True)]


def test_partial_failure_still_returns_the_successful_connectors():
    service = StubMarketDataService(
        results={"binance": TICK}, errors={"ascend_ex": TickerFetchError("symbol map is empty")}
    )
    response = client_for(service).get("/market-data/tickers?connectors=binance,ascend_ex")
    assert response.status_code == 200
    body = response.json()
    assert body["counts"] == {"binance": 1}
    assert "symbol map is empty" in body["errors"]["ascend_ex"]


def test_status_codes_when_nothing_can_be_served():
    unsupported = StubMarketDataService(errors={"xrpl": TickerUnsupportedError("needs a node pool")})
    assert client_for(unsupported).get("/market-data/tickers?connectors=xrpl").status_code == 400

    failed = StubMarketDataService(errors={"ascend_ex": TickerFetchError("boom")})
    assert client_for(failed).get("/market-data/tickers?connectors=ascend_ex").status_code == 502

    empty = StubMarketDataService()
    assert client_for(empty).get("/market-data/tickers?connectors=nope").status_code == 404
