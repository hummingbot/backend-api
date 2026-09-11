"""Tests for the bounded, ordered log-deduplication cache in MQTTManager."""

import time
from collections import OrderedDict

from utils.mqtt_manager import MQTTManager


def make_manager() -> MQTTManager:
    return MQTTManager(host="localhost", port=1883, username="u", password="p")


class CountingOrderedDict(OrderedDict):
    """OrderedDict that records how many times the cleanup loop peeks at it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.values_calls = 0

    def values(self):
        self.values_calls += 1
        return super().values()


async def test_duplicate_log_messages_within_ttl_are_suppressed():
    manager = make_manager()
    entry = {"level_name": "INFO", "msg": "hello", "timestamp": 1000.0}

    await manager._handle_log("bot-1", entry)
    await manager._handle_log("bot-1", dict(entry))

    assert len(manager._bot_logs["bot-1"]) == 1
    assert len(manager._processed_messages) == 1


async def test_error_logs_are_routed_to_the_error_deque():
    manager = make_manager()

    await manager._handle_log("bot-1", {"level_name": "ERROR", "msg": "boom", "timestamp": 1000.0})
    await manager._handle_log("bot-1", {"level_name": "INFO", "msg": "fine", "timestamp": 1000.0})
    await manager._handle_log("bot-1", "plain string log")

    assert [e["msg"] for e in manager._bot_error_logs["bot-1"]] == ["boom"]
    assert [e["msg"] for e in manager._bot_logs["bot-1"]] == ["fine", "plain string log"]


async def test_cleanup_only_touches_the_expired_end_of_the_cache():
    manager = make_manager()
    now = time.time()

    cache = CountingOrderedDict()
    for i in range(3):
        cache[f"expired-{i}"] = now - manager._message_ttl - 10
    for i in range(5000):
        cache[f"live-{i}"] = now
    manager._processed_messages = cache

    await manager._handle_log("bot-1", {"level_name": "INFO", "msg": "new", "timestamp": now})

    # 3 pops for the expired entries + 1 peek that finds a live entry and stops.
    assert cache.values_calls == 4
    assert not any(h.startswith("expired-") for h in cache)
    assert len(cache) == 5001


async def test_processed_messages_cache_is_bounded_regardless_of_log_rate():
    manager = make_manager()
    manager._max_processed_messages = 50

    for i in range(500):
        await manager._handle_log("bot-1", {"level_name": "INFO", "msg": f"msg-{i}", "timestamp": 1000.0})

    assert len(manager._processed_messages) == 50
    # The most recent messages are the ones retained.
    assert "bot-1:msg-499:1000" in manager._processed_messages
    assert "bot-1:msg-0:1000" not in manager._processed_messages
