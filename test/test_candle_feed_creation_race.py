"""
Regression tests for the check-then-create race in MarketDataService.get_candles_feed.

Creating a candle feed validates the trading pair over the network (exchange data load plus a
REST probe) *before* the feed is registered in `_candle_feeds`. Without a per-key lock two
concurrent first-touch callers both pass the `feed_key not in self._candle_feeds` guard, both
build and start a feed, and the loser of the assignment race keeps running a live exchange
subscription that no teardown path can reach, because every teardown keys off `_candle_feeds`.

Run with: pytest test/test_candle_feed_creation_race.py -v
"""
import asyncio

import numpy as np
import pytest
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig

from services.market_data_service import FeedType, MarketDataService


class FakeCandleFeed:
    """Candle feed stand-in whose validation yields to the loop, exposing the race window."""

    def __init__(self, config: CandlesConfig):
        self.config = config
        self.started = False
        self.stopped = False

    async def initialize_exchange_data(self):
        # A real initialize does network I/O; yielding twice makes the suspension deterministic.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def fetch_candles(self, end_time=None, limit=50):
        await asyncio.sleep(0)
        return np.zeros((limit, 6))

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeCandlesFactory:
    """Records every feed it builds so orphaned (unreferenced) feeds can be detected."""

    _candles_map = {"binance": FakeCandleFeed}

    def __init__(self):
        self.created = []

    def get_candle(self, config: CandlesConfig) -> FakeCandleFeed:
        feed = FakeCandleFeed(config)
        self.created.append(feed)
        return feed


@pytest.fixture
def factory(monkeypatch):
    fake = FakeCandlesFactory()
    monkeypatch.setattr("services.market_data_service.CandlesFactory", fake)
    return fake


@pytest.fixture
def service():
    return MarketDataService(connector_service=None)


CONFIG = CandlesConfig(connector="binance", trading_pair="BTC-USDT", interval="1m")


async def test_concurrent_first_touch_creates_exactly_one_feed(service, factory):
    """Two racing callers must collapse into a single created-and-started feed."""
    feed_a, feed_b = await asyncio.gather(
        service.get_candles_feed(CONFIG),
        service.get_candles_feed(CONFIG),
    )

    assert len(factory.created) == 1, "the race started more than one candle feed"
    assert feed_a is feed_b
    assert feed_a.started is True
    assert len(service._candle_feeds) == 1


async def test_no_started_feed_is_left_unreferenced(service, factory):
    """Every feed that was started must be reachable through _candle_feeds, or it leaks."""
    await asyncio.gather(*(service.get_candles_feed(CONFIG) for _ in range(5)))

    referenced = set(id(feed) for feed in service._candle_feeds.values())
    orphans = [feed for feed in factory.created if feed.started and id(feed) not in referenced]

    assert orphans == [], f"{len(orphans)} started candle feed(s) left with no reference"


async def test_distinct_feed_keys_are_not_serialized_into_one_feed(service, factory):
    """The lock is per feed_key: different pairs still get their own feeds."""
    other = CandlesConfig(connector="binance", trading_pair="ETH-USDT", interval="1m")

    feed_a, feed_b = await asyncio.gather(
        service.get_candles_feed(CONFIG),
        service.get_candles_feed(other),
    )

    assert feed_a is not feed_b
    assert len(service._candle_feeds) == 2


async def test_cached_feed_is_returned_without_recreating(service, factory):
    """The second, non-concurrent call must hit the cache and pay no creation cost."""
    first = await service.get_candles_feed(CONFIG)
    second = await service.get_candles_feed(CONFIG)

    assert first is second
    assert len(factory.created) == 1


# ==================== Lock dict pruning ====================

async def test_stop_candle_feed_prunes_the_lock(service, factory):
    await service.get_candles_feed(CONFIG)
    assert service._candle_feed_locks

    service.stop_candle_feed(CONFIG)

    assert service._candle_feed_locks == {}


async def test_manual_cleanup_prunes_the_lock(service, factory):
    await service.get_candles_feed(CONFIG)

    service.manually_cleanup_feed(FeedType.CANDLES, "binance", "BTC-USDT", "1m")

    assert service._candle_feed_locks == {}
    assert service._candle_feeds == {}


async def test_unused_feed_cleanup_prunes_the_lock(service, factory):
    feed = await service.get_candles_feed(CONFIG)
    # Age the feed past the timeout so the janitor collects it.
    for key in service._last_access_times:
        service._last_access_times[key] = 0.0

    await service._cleanup_unused_feeds()

    assert feed.stopped is True
    assert service._candle_feed_locks == {}
    assert service._candle_feeds == {}


async def test_stop_service_prunes_the_locks(service, factory):
    await service.get_candles_feed(CONFIG)

    service.stop()

    assert service._candle_feed_locks == {}
    assert service._candle_feeds == {}


async def test_a_held_lock_survives_pruning(service, factory):
    """
    Pruning must not hand a fresh lock to the next caller while a creator still holds the old
    one: that would reopen the race the lock exists to close.
    """
    feed_key = service._generate_feed_key(FeedType.CANDLES, "binance", "BTC-USDT", "1m")
    lock = service._candle_feed_locks.setdefault(feed_key, asyncio.Lock())

    async with lock:
        service._discard_candle_feed_lock(feed_key)
        assert service._candle_feed_locks.get(feed_key) is lock

    service._discard_candle_feed_lock(feed_key)
    assert feed_key not in service._candle_feed_locks
