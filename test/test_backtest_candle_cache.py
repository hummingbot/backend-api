"""
Tests that repeated backtests over one market download its candle history once (PERF-112),
without handing two runs anything mutable to share.

History: a backtest used to run on a service-wide BacktestingEngineBase whose data provider
cached downloaded candles across runs -- and corrupted concurrent runs doing it (CORR-060).
ARCH-063 gave every run its own engine in its own process, which fixed that structurally and
lost the cache with it: an optimizer sweeping N configs over one market downloaded that
market N times.

The cache is back, outside the process, and holds only candle *data*: a reader gets its own
unpickled copy, so there is still no engine, provider or controller reachable from two runs.
These tests pin both halves -- that the download happens once, and that what is shared cannot
be mutated by one run into another's view -- plus the two bounds a cache has to have: it is
capped in size, and it never answers for a range it does not actually hold.

The repo has no async test setup, so coroutines are driven with asyncio.run().

Run with: pytest test/test_backtest_candle_cache.py -v
"""
import asyncio
import multiprocessing
import os
import time
from pathlib import Path

import pandas as pd
import pytest

from services.backtesting_service import _install_candle_cache, _run_backtest_blocking
from services.candles_cache import CandlesCache, cache_key

WINDOW = (1_700_000_000, 1_700_086_400)


class _CandlesConfig:
    """The fields of hummingbot's CandlesConfig that decide what a download fetches."""

    def __init__(self, connector="binance", trading_pair="BTC-USDT", interval="1m", max_records=500):
        self.connector = connector
        self.trading_pair = trading_pair
        self.interval = interval
        self.max_records = max_records


def _frame(seed=0.0):
    return pd.DataFrame({"timestamp": [WINDOW[0], WINDOW[1]], "close": [100.0 + seed, 101.0 + seed]})


class _Provider:
    """Stands in for BacktestingDataProvider: what the wrapper touches, and a counted download."""

    def __init__(self, downloads, start_time=WINDOW[0], end_time=WINDOW[1]):
        self.start_time = start_time
        self.end_time = end_time
        self.candles_feeds = {}
        self.downloads = downloads

    @staticmethod
    def _generate_candle_feed_key(config):
        return f"{config.connector}_{config.trading_pair}_{config.interval}"

    async def get_candles_feed(self, config):
        self.downloads.append(self._generate_candle_feed_key(config))
        return _frame(len(self.downloads))


class _CoveringProvider(_Provider):
    """Upstream's own rule: a feed already held for this market answers, whatever was asked."""

    async def get_candles_feed(self, config):
        held = self.candles_feeds.get(self._generate_candle_feed_key(config))
        if held is not None:
            return held
        return await super().get_candles_feed(config)


def _cache(tmp_path, max_entries=32, ttl_seconds=3600.0):
    return CandlesCache(path=str(tmp_path / "candles"), max_entries=max_entries, ttl_seconds=ttl_seconds)


def _run(cache, downloads, config=None, **provider_kwargs):
    """One backtest's worth of candle fetching, on a provider that has never seen the market."""
    provider = _Provider(downloads, **provider_kwargs)
    _install_candle_cache(provider, cache)
    return asyncio.run(provider.get_candles_feed(config or _CandlesConfig())), provider


def _entries(tmp_path):
    return list((tmp_path / "candles").glob("*.pkl"))


class TestRepeatedRunsDownloadOnce:
    def test_a_sweep_over_one_market_downloads_it_once(self, tmp_path):
        """The item's case: N configs, one market, one window -- one download."""
        cache, downloads = _cache(tmp_path), []
        frames = [_run(cache, downloads)[0] for _ in range(7)]

        assert len(downloads) == 1, f"a sweep of 7 runs downloaded {len(downloads)} times"
        for frame in frames[1:]:
            pd.testing.assert_frame_equal(frame, frames[0])

    def test_the_provider_still_holds_the_feed_under_its_own_key(self, tmp_path):
        """A cache hit must leave the provider looking exactly like a download did."""
        cache, downloads = _cache(tmp_path), []
        _run(cache, downloads)
        _, provider = _run(cache, downloads)

        assert list(provider.candles_feeds) == ["binance_BTC-USDT_1m"]
        pd.testing.assert_frame_equal(provider.candles_feeds["binance_BTC-USDT_1m"], _frame(1))

    def test_a_run_asking_twice_reads_once(self, tmp_path):
        """The second ask inside one run is answered from the run's own memo, not the disk."""
        cache, downloads = _cache(tmp_path), []
        provider = _Provider(downloads)
        _install_candle_cache(provider, cache)

        async def scenario():
            first = await provider.get_candles_feed(_CandlesConfig())
            second = await provider.get_candles_feed(_CandlesConfig())
            return first, second

        first, second = asyncio.run(scenario())
        assert first is second
        assert len(downloads) == 1


class TestNothingMutableIsShared:
    """The guarantee CORR-060 established and this cache must not undo."""

    def test_each_run_gets_its_own_copy_of_the_frame(self, tmp_path):
        cache, downloads = _cache(tmp_path), []
        first, _ = _run(cache, downloads)
        second, _ = _run(cache, downloads)

        assert first is not second, "two runs were handed the same mutable frame"

    def test_one_run_mutating_its_frame_cannot_reach_another(self, tmp_path):
        cache, downloads = _cache(tmp_path), []
        first, _ = _run(cache, downloads)
        first.loc[0, "close"] = -999.0

        second, _ = _run(cache, downloads)
        assert second.loc[0, "close"] == 100.0 + 1
        assert len(downloads) == 1


class TestItNeverAnswersForARangeItDoesNotHold:
    @pytest.mark.parametrize("provider_kwargs", [
        {"start_time": WINDOW[0] - 86_400},  # window starts earlier
        {"end_time": WINDOW[1] + 86_400},    # window ends later
    ])
    def test_a_different_window_is_a_miss(self, tmp_path, provider_kwargs):
        cache, downloads = _cache(tmp_path), []
        _run(cache, downloads)
        _run(cache, downloads, **provider_kwargs)

        assert len(downloads) == 2, "a window the cache does not hold was served from it anyway"

    @pytest.mark.parametrize("config_kwargs", [
        {"connector": "kucoin"},
        {"trading_pair": "ETH-USDT"},
        {"interval": "5m"},
        {"max_records": 1000},  # decides how far before the window the fetch reaches back
    ])
    def test_a_different_feed_is_a_miss(self, tmp_path, config_kwargs):
        cache, downloads = _cache(tmp_path), []
        _run(cache, downloads)
        _run(cache, downloads, config=_CandlesConfig(**config_kwargs))

        assert len(downloads) == 2, f"{config_kwargs} was served another feed's candles"

    def test_a_feed_reused_inside_a_run_is_not_stored_as_another_key(self, tmp_path):
        """Upstream reuses a feed it already holds whatever max_records asked for.

        That frame was fetched for the narrower request, so caching it under the wider one
        would serve a later run less history than it asked for. Only real downloads are kept.
        """
        cache, downloads = _cache(tmp_path), []
        provider = _CoveringProvider(downloads)
        _install_candle_cache(provider, cache)

        async def a_run_that_asks_narrow_then_wide():
            await provider.get_candles_feed(_CandlesConfig(max_records=500))
            await provider.get_candles_feed(_CandlesConfig(max_records=1000))

        asyncio.run(a_run_that_asks_narrow_then_wide())
        assert len(downloads) == 1  # upstream's own in-run reuse is left alone

        _run(cache, downloads, config=_CandlesConfig(max_records=1000))
        assert len(downloads) == 2, "a run was handed a frame fetched for a shorter lookback"

    def test_an_entry_past_its_freshness_bound_is_refetched(self, tmp_path):
        """A window ending near now is fetched with its last candle still forming."""
        cache, downloads = _cache(tmp_path, ttl_seconds=0.0), []
        _run(cache, downloads)
        time.sleep(0.01)
        _run(cache, downloads)

        assert len(downloads) == 2


class TestBounds:
    def test_distinct_markets_cannot_grow_the_store_without_limit(self, tmp_path):
        cache, downloads = _cache(tmp_path, max_entries=3), []
        for n in range(12):
            _run(cache, downloads, config=_CandlesConfig(trading_pair=f"T{n}-USDT"))

        assert len(downloads) == 12
        assert len(_entries(tmp_path)) == 3, "the candle cache grew past its cap"

    def test_the_least_recently_used_entry_is_the_one_dropped(self, tmp_path):
        cache, downloads = _cache(tmp_path, max_entries=2), []
        configs = [_CandlesConfig(trading_pair=f"T{n}-USDT") for n in range(3)]
        _run(cache, downloads, config=configs[0])
        _run(cache, downloads, config=configs[1])
        time.sleep(0.01)
        _run(cache, downloads, config=configs[0])  # touch the oldest, making it the newest
        _run(cache, downloads, config=configs[2])  # evicts one

        assert len(_entries(tmp_path)) == 2
        _run(cache, downloads, config=configs[0])
        assert len(downloads) == 3, "the entry that was just used is the one that got dropped"

    def test_a_disabled_cache_downloads_every_time(self, tmp_path):
        """The operator escape hatch: BACKTESTING_CANDLES_CACHE_ENTRIES=0."""
        cache, downloads = _cache(tmp_path, max_entries=0), []
        _run(cache, downloads)
        _run(cache, downloads)

        assert len(downloads) == 2
        assert not (tmp_path / "candles").exists(), "a disabled cache still wrote to disk"


class TestTheCacheIsNeverAFailureMode:
    def test_an_unreadable_entry_is_a_miss_not_a_crash(self, tmp_path):
        cache, downloads = _cache(tmp_path), []
        _run(cache, downloads)
        for entry in _entries(tmp_path):
            entry.write_bytes(b"not a pickle")

        frame, _ = _run(cache, downloads)
        assert len(downloads) == 2
        pd.testing.assert_frame_equal(frame, _frame(2))

    def test_an_unwritable_store_still_runs_the_backtest(self, tmp_path):
        cache = CandlesCache(path=str(tmp_path / "candles"), max_entries=4, ttl_seconds=3600.0)
        (tmp_path / "candles").chmod(0o500)
        try:
            downloads = []
            frame, _ = _run(cache, downloads)
            assert len(downloads) == 1
            pd.testing.assert_frame_equal(frame, _frame(1))
        finally:
            (tmp_path / "candles").chmod(0o700)


# -- single flight across processes --
#
# Runs are separate processes, so the workers that miss the same key at once are in separate
# processes too. Defined at module scope because a spawned child has to import them.


def _download_once(cache_path, key, marker_dir):
    """A worker's first touch of a market: miss, take the lock, re-check, download."""
    cache = CandlesCache(path=cache_path, max_entries=8, ttl_seconds=3600.0)
    if cache.get(key) is None:
        with cache.single_flight(key):
            if cache.get(key) is None:
                Path(marker_dir, f"{os.getpid()}").write_text("")
                time.sleep(0.3)  # stands in for the multi-second historical download
                cache.put(key, _frame())


def test_workers_that_miss_the_same_market_together_download_once(tmp_path):
    """Without single flight, raising BACKTESTING_MAX_CONCURRENT re-multiplies the download."""
    marker_dir = tmp_path / "downloads"
    marker_dir.mkdir()
    key = cache_key("binance", "BTC-USDT", "1m", 500, *WINDOW)

    ctx = multiprocessing.get_context("spawn")
    workers = [ctx.Process(target=_download_once, args=(str(tmp_path / "candles"), key, str(marker_dir)))
               for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(60)

    assert [w.exitcode for w in workers] == [0, 0, 0, 0]
    downloads = list(marker_dir.iterdir())
    assert len(downloads) == 1, f"{len(downloads)} workers downloaded the same market at once"


# -- the real classes --


class TestAgainstTheRealEngine:
    def test_the_data_provider_routes_every_download_through_what_the_cache_wraps(self, tmp_path):
        """Upstream's initialize_candles_feed must go through the instance attribute we wrap."""
        from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
        from hummingbot.strategy_v2.backtesting.backtesting_data_provider import BacktestingDataProvider

        config = CandlesConfig(connector="binance", trading_pair="BTC-USDT", interval="1m")
        cache, downloads = _cache(tmp_path), []

        def _fetch(provider):
            provider.update_backtesting_time(*WINDOW)

            async def fake_download(_config):
                downloads.append(_config.trading_pair)
                return _frame()

            provider.get_candles_feed = fake_download  # stands in for the exchange
            _install_candle_cache(provider, cache)
            asyncio.run(provider.initialize_candles_feed(config))
            return provider

        _fetch(BacktestingDataProvider(connectors={}))
        provider = _fetch(BacktestingDataProvider(connectors={}))

        assert len(downloads) == 1, "the real provider downloaded again on a cache hit"
        assert provider.get_candles_df("binance", "BTC-USDT", "1m").empty is False

    def test_the_worker_installs_the_cache_on_the_engine_it_builds(self, tmp_path, monkeypatch):
        """The wiring: two worker runs of the same config download the history once."""
        import hummingbot.strategy_v2.backtesting.backtesting_engine_base as engine_module

        downloads = []

        class _FakeEngine:
            def __init__(self):
                self.backtesting_data_provider = _Provider(downloads)

            @classmethod
            def get_controller_config_instance_from_dict(cls, config_data, controllers_module):
                return config_data

            async def run_backtesting(self, controller_config, trade_cost, start, end, backtesting_resolution):
                await self.backtesting_data_provider.get_candles_feed(_CandlesConfig())
                return {
                    "executors": [],
                    "results": {"sharpe_ratio": None, "net_pnl": 0.0},
                    "processed_data": {"features": _frame()},
                }

        monkeypatch.setattr(engine_module, "BacktestingEngineBase", _FakeEngine)
        cache = _cache(tmp_path)
        config = {"config": {"controller_name": "x"}, "start_time": WINDOW[0], "end_time": WINDOW[1]}

        first = _run_backtest_blocking(config, "conf", "controllers", cache=cache)
        second = _run_backtest_blocking(config, "conf", "controllers", cache=cache)

        assert len(downloads) == 1, "the worker did not route its download through the cache"
        assert first["results"]["sharpe_ratio"] == 0  # payload shaping is untouched
        assert second["processed_data"] == first["processed_data"]
