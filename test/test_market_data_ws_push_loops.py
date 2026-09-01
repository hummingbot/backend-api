"""
Tests for the /ws/market-data push loops (CORR-102).

The candles, order-book and trades loops used to wrap their whole poll body in
`except (WebSocketDisconnect, RuntimeError)` and break, so a RuntimeError raised
by the *data fetch* (a service fault, a connector still initialising) killed the
subscription for good and logged a disconnect that never happened. All three now
go through one `_send_or_stop` helper that guards only `send_json`; fetch errors
fall to `except Exception`, which logs and retries on the next interval.

Run with: pytest test/test_market_data_ws_push_loops.py -v --asyncio-mode=auto
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest
from fastapi.websockets import WebSocketDisconnect

from services.websocket_manager import Subscription, WebSocketManager


class RecordingWebSocket:
    """Captures pushed frames; can be told to raise on the first N sends."""

    def __init__(self, raise_on_send=None, fail_first=0):
        self.sent = []
        self.raise_on_send = raise_on_send
        self.fail_first = fail_first
        self.send_calls = 0
        self._target = None
        self._reached = asyncio.Event()

    async def send_json(self, message):
        self.send_calls += 1
        if self.raise_on_send is not None and self.send_calls > self.fail_first:
            raise self.raise_on_send
        self.sent.append(message)
        if self._target is not None and len(self.sent) >= self._target:
            self._reached.set()

    async def wait_for_frames(self, count, timeout=2.0):
        self._target = count
        if len(self.sent) >= count:
            return
        self._reached.clear()
        await asyncio.wait_for(self._reached.wait(), timeout)


class FlakyFetch:
    """Raises RuntimeError on the first `failures` calls, then returns `value`."""

    def __init__(self, value, failures=1):
        self.value = value
        self.failures = failures
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("market data service is not ready")
        return self.value


def make_candles_df(timestamp=1_700_000_000.0):
    return pd.DataFrame(
        [{"timestamp": timestamp, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0}]
    )


def make_feed(df=None):
    feed = MagicMock()
    feed.ready = True
    feed.candles_df = make_candles_df() if df is None else df
    return feed


def make_order_book():
    ob = MagicMock()
    ob.last_diff_uid = None
    ob.snapshot_uid = None
    bids = pd.DataFrame([{"price": 100.0, "amount": 1.0}])
    asks = pd.DataFrame([{"price": 101.0, "amount": 2.0}])
    ob.snapshot = (bids, asks)
    return ob


def make_manager():
    market_data_service = MagicMock()
    market_data_service.get_candles_feed = AsyncMock(return_value=make_feed())
    market_data_service.get_order_book = MagicMock(return_value=make_order_book())
    return WebSocketManager(market_data_service), market_data_service


def make_sub(sub_type, interval=0.01):
    return Subscription(
        subscription_id=f"{sub_type}_binance_BTC-USDT",
        sub_type=sub_type,
        connector="binance",
        trading_pair="BTC-USDT",
        update_interval=interval,
        interval="1m",
        max_records=100,
        depth=10,
    )


# ---------------------------------------------------------------------------
# A RuntimeError from the fetch must NOT end the subscription
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candles_loop_survives_a_runtime_error_from_the_fetch():
    manager, service = make_manager()
    flaky = FlakyFetch(make_feed(), failures=2)

    async def get_candles_feed(config):
        return flaky(config)

    service.get_candles_feed = AsyncMock(side_effect=get_candles_feed)
    ws = RecordingWebSocket()
    sub = make_sub("candles")

    task = asyncio.create_task(manager._candles_push_loop(ws, sub))
    await ws.wait_for_frames(1)
    task.cancel()

    # The two failing fetches were logged and retried, not fatal.
    assert flaky.calls >= 3
    assert ws.sent[0]["type"] == "candles"
    assert not task.done() or task.cancelled()


@pytest.mark.asyncio
async def test_order_book_loop_survives_a_runtime_error_from_the_fetch():
    manager, service = make_manager()
    flaky = FlakyFetch(make_order_book(), failures=2)
    service.get_order_book = MagicMock(side_effect=flaky)
    ws = RecordingWebSocket()
    sub = make_sub("order_book")

    task = asyncio.create_task(manager._order_book_push_loop(ws, sub))
    await ws.wait_for_frames(1)
    task.cancel()

    assert flaky.calls >= 3
    assert ws.sent[0]["type"] == "order_book"
    assert ws.sent[0]["data"] == {"bids": [[100.0, 1.0]], "asks": [[101.0, 2.0]]}


class FlakyBuffer(list):
    """A trade buffer whose first `failures` drains raise RuntimeError."""

    def __init__(self, items, failures=2):
        super().__init__(items)
        self.failures = failures
        self.drains = 0

    def __getitem__(self, item):
        if isinstance(item, slice):
            self.drains += 1
            if self.drains <= self.failures:
                raise RuntimeError("transient fault while draining")
        return list.__getitem__(self, item)


@pytest.mark.asyncio
async def test_trades_loop_survives_a_runtime_error_while_draining():
    """A RuntimeError raised before the send is retried, not fatal."""
    manager, _ = make_manager()
    ws = RecordingWebSocket()
    sub = make_sub("trades")
    sub.trade_buffer = FlakyBuffer([{"price": 1.0, "amount": 2.0}], failures=2)

    task = asyncio.create_task(manager._trades_push_loop(ws, sub))
    await ws.wait_for_frames(1)
    task.cancel()

    assert sub.trade_buffer.drains >= 3
    assert ws.sent[0]["type"] == "trades"
    assert ws.sent[0]["data"] == [{"price": 1.0, "amount": 2.0}]


# ---------------------------------------------------------------------------
# A dropped client still ends the loop, once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [WebSocketDisconnect(), RuntimeError("client disconnected")])
async def test_candles_loop_stops_when_the_client_is_gone(error):
    manager, _ = make_manager()
    ws = RecordingWebSocket(raise_on_send=error)
    sub = make_sub("candles")

    await asyncio.wait_for(manager._candles_push_loop(ws, sub), timeout=1.0)

    assert ws.send_calls == 1  # logged once, loop ended
    assert ws.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [WebSocketDisconnect(), RuntimeError("client disconnected")])
async def test_order_book_loop_stops_when_the_client_is_gone(error):
    manager, _ = make_manager()
    ws = RecordingWebSocket(raise_on_send=error)
    sub = make_sub("order_book")

    await asyncio.wait_for(manager._order_book_push_loop(ws, sub), timeout=1.0)

    assert ws.send_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [WebSocketDisconnect(), RuntimeError("client disconnected")])
async def test_trades_loop_stops_when_the_client_is_gone(error):
    manager, _ = make_manager()
    ws = RecordingWebSocket(raise_on_send=error)
    sub = make_sub("trades")
    sub.trade_buffer.append({"price": 1.0, "amount": 2.0})

    await asyncio.wait_for(manager._trades_push_loop(ws, sub), timeout=1.0)

    assert ws.send_calls == 1


# ---------------------------------------------------------------------------
# All three loops go through the one shared helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_or_stop_reports_true_on_a_healthy_send():
    manager, _ = make_manager()
    ws = RecordingWebSocket()
    sub = make_sub("candles")

    assert await manager._send_or_stop(ws, sub, "candles", {"type": "candles"}) is True
    assert ws.sent == [{"type": "candles"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [WebSocketDisconnect(), RuntimeError("boom")])
async def test_send_or_stop_reports_false_when_the_send_fails(error):
    manager, _ = make_manager()
    ws = RecordingWebSocket(raise_on_send=error)
    sub = make_sub("candles")

    assert await manager._send_or_stop(ws, sub, "candles", {"type": "candles"}) is False


def test_no_push_loop_guards_its_fetch_against_runtime_error():
    """The regression guard: the broad guard must not come back."""
    import inspect

    import services.websocket_manager as module

    source = inspect.getsource(module)
    assert "except (WebSocketDisconnect, RuntimeError)" in source  # still guarded in the helper
    assert source.count("except (WebSocketDisconnect, RuntimeError)") == 1
    assert inspect.getsource(module.WebSocketManager._send_or_stop).count(
        "except (WebSocketDisconnect, RuntimeError)"
    ) == 1
