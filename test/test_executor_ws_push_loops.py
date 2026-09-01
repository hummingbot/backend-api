"""
Tests for the shared /ws/executors push loop (ARCH-055).

Seven near-identical `_*_push_loop` coroutines were collapsed into one generic
`_push_loop` driven by a `sub_type -> PushSpec(fetch, msg_type, extra)` table,
with `_logs_push_loop` kept separate because it keys on `last_log_count`.
These tests pin the behaviour that must not have changed: the frame shape of
every channel, per-subscription intervals, change detection, per-channel error
handling, and cancellation/cleanup.

Run with: pytest test/test_executor_ws_push_loops.py -v --asyncio-mode=auto
"""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.websockets import WebSocketDisconnect

from services.executor_ws_manager import SUBSCRIPTION_TYPES, ExecutorSubscription, ExecutorWebSocketManager


class RecordingWebSocket:
    """Captures pushed frames and lets a test await the Nth one."""

    def __init__(self, raise_on_send=None):
        self.sent = []
        self.raise_on_send = raise_on_send
        self._target = None
        self._reached = asyncio.Event()

    async def send_json(self, message):
        if self.raise_on_send is not None:
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

    def data_frames(self):
        return [f for f in self.sent if f.get("type") not in ("subscribed", "unsubscribed", "error")]


class FakePosition:
    """Minimal stand-in for a PositionHeld row, with real numbers to cast."""

    def __init__(self, trading_pair="BTC-USDT"):
        self.trading_pair = trading_pair
        self.connector_name = "binance_perpetual"
        self.account_name = "master"
        self.controller_id = "ctrl-1"
        self.buy_amount_base = 1.0
        self.buy_amount_quote = 100.0
        self.sell_amount_base = 0.5
        self.sell_amount_quote = 60.0
        self.net_amount_base = 0.5
        self.buy_breakeven_price = 100.0
        self.sell_breakeven_price = 120.0
        self.matched_amount_base = 0.5
        self.unmatched_amount_base = 0.5
        self.position_side = "LONG"
        self.realized_pnl_quote = 10.0
        self.executor_ids = ["e-1", "e-2"]
        self.last_updated = datetime(2026, 1, 1, 12, 0, 0)

    def get_unrealized_pnl(self, rate):
        return float(rate) * self.net_amount_base


def build_manager(**overrides):
    """A manager whose every backing service call returns canned data."""
    executor_service = MagicMock()
    executor_service.get_executors = AsyncMock(return_value=[{"id": "e-1"}])
    executor_service.get_executor = AsyncMock(return_value={"id": "e-1"})
    executor_service.get_summary = MagicMock(return_value={"total": 1})
    executor_service.get_performance_report = AsyncMock(return_value={"pnl": 1.0})
    executor_service.get_positions_held = MagicMock(return_value=[FakePosition()])
    executor_service.get_executor_logs = MagicMock(return_value=[{"msg": "hello"}])

    market_data_service = MagicMock()
    market_data_service.get_rate = MagicMock(return_value=200.0)

    orchestrator = MagicMock()
    orchestrator.get_bot_status = MagicMock(
        return_value={
            "status": "running",
            "performance": {"pnl": 1.0},
            "recently_active": True,
            "logs": ["should be stripped"],
        }
    )
    orchestrator.get_all_bots_status = MagicMock(
        return_value={
            "bot-1": {
                "status": "running",
                "source": "broker",
                "performance": {"pnl": 1.0},
                "recently_active": True,
                "logs": ["should be stripped"],
            }
        }
    )

    for name, value in overrides.items():
        setattr(executor_service, name, value)

    return ExecutorWebSocketManager(
        executor_service=executor_service,
        market_data_service=market_data_service,
        bots_orchestrator=orchestrator,
    ), executor_service, orchestrator


SUBSCRIBE_MESSAGES = {
    "executors": {"type": "executors"},
    "executor_detail": {"type": "executor_detail", "executor_id": "e-1"},
    "executor_summary": {"type": "executor_summary"},
    "performance": {"type": "performance", "controller_id": "ctrl-1"},
    "positions": {"type": "positions"},
    "executor_logs": {"type": "executor_logs", "executor_id": "e-1"},
    "bot_status": {"type": "bot_status", "bot_name": "bot-1"},
    "all_bots_status": {"type": "all_bots_status"},
}

EXPECTED_FRAME_KEYS = {
    "executors": ["type", "subscription_id", "data", "total_count", "timestamp"],
    "executor_detail": ["type", "subscription_id", "data", "timestamp"],
    "executor_summary": ["type", "subscription_id", "data", "timestamp"],
    "performance": ["type", "subscription_id", "data", "timestamp"],
    "positions": ["type", "subscription_id", "data", "timestamp"],
    "executor_logs": ["type", "subscription_id", "data", "total_count", "timestamp"],
    "bot_status": ["type", "subscription_id", "data", "timestamp"],
    "all_bots_status": ["type", "subscription_id", "data", "bot_count", "timestamp"],
}


async def subscribe_and_collect(manager, websocket, sub_type, frames=2):
    """Subscribe, wait for the ack plus the first data frame, then clean up."""
    await manager.handle_subscribe("conn-1", websocket, SUBSCRIBE_MESSAGES[sub_type])
    try:
        await websocket.wait_for_frames(frames)
    finally:
        manager.remove_connection("conn-1")
        await asyncio.sleep(0)


class TestDispatchTable:
    """_get_push_fn must still resolve every declared subscription type."""

    def test_every_subscription_type_resolves_to_a_loop(self):
        manager, _, _ = build_manager()
        for sub_type in SUBSCRIPTION_TYPES:
            assert manager._get_push_fn(sub_type) is not None

    def test_the_spec_table_covers_every_type_except_logs(self):
        manager, _, _ = build_manager()
        assert set(manager._push_specs()) == SUBSCRIPTION_TYPES - {"executor_logs"}

    def test_each_spec_message_type_matches_its_subscription_type(self):
        manager, _, _ = build_manager()
        for sub_type, spec in manager._push_specs().items():
            assert spec.msg_type == sub_type

    def test_only_two_polling_loop_bodies_remain(self):
        """The seven hash-and-send copies are gone (ARCH-055 acceptance)."""
        from pathlib import Path

        import services.executor_ws_manager as module

        source = Path(module.__file__).read_text()
        assert source.count("await asyncio.sleep(sub.update_interval)") == 2
        assert source.count("while True:") == 2


class TestFrameShapePerChannel:
    """Each of the eight channels still emits its original frame shape."""

    @pytest.mark.parametrize("sub_type", sorted(SUBSCRIPTION_TYPES))
    async def test_ack_then_a_frame_of_the_subscription_type(self, sub_type):
        manager, _, _ = build_manager()
        websocket = RecordingWebSocket()

        await subscribe_and_collect(manager, websocket, sub_type)

        assert websocket.sent[0]["type"] == "subscribed"
        assert websocket.sent[0]["subscription_type"] == sub_type
        data_frames = websocket.data_frames()
        assert data_frames, f"{sub_type} pushed no data frame"
        assert data_frames[0]["type"] == sub_type

    @pytest.mark.parametrize("sub_type", sorted(SUBSCRIPTION_TYPES))
    async def test_frame_keys_and_their_order_are_unchanged(self, sub_type):
        manager, _, _ = build_manager()
        websocket = RecordingWebSocket()

        await subscribe_and_collect(manager, websocket, sub_type)

        frame = websocket.data_frames()[0]
        assert list(frame) == EXPECTED_FRAME_KEYS[sub_type]
        assert frame["subscription_id"] == websocket.sent[0]["subscription_id"]
        assert isinstance(frame["timestamp"], float)

    async def test_executors_frame_carries_the_total_count(self):
        manager, _, _ = build_manager()
        websocket = RecordingWebSocket()

        await subscribe_and_collect(manager, websocket, "executors")

        frame = websocket.data_frames()[0]
        assert frame["data"] == [{"id": "e-1"}]
        assert frame["total_count"] == 1

    async def test_all_bots_status_frame_carries_the_bot_count_and_strips_logs(self):
        manager, _, _ = build_manager()
        websocket = RecordingWebSocket()

        await subscribe_and_collect(manager, websocket, "all_bots_status")

        frame = websocket.data_frames()[0]
        assert frame["bot_count"] == 1
        assert frame["data"] == {
            "bot-1": {
                "status": "running",
                "source": "broker",
                "performance": {"pnl": 1.0},
                "recently_active": True,
            }
        }

    async def test_bot_status_frame_strips_logs(self):
        manager, _, _ = build_manager()
        websocket = RecordingWebSocket()

        await subscribe_and_collect(manager, websocket, "bot_status")

        assert websocket.data_frames()[0]["data"] == {
            "bot_name": "bot-1",
            "status": "running",
            "performance": {"pnl": 1.0},
            "recently_active": True,
        }

    async def test_positions_payload_shaping_is_preserved(self):
        manager, _, _ = build_manager()
        websocket = RecordingWebSocket()

        await subscribe_and_collect(manager, websocket, "positions")

        payload = websocket.data_frames()[0]["data"]
        assert payload["total_positions"] == 1
        assert payload["total_realized_pnl"] == 10.0
        assert payload["total_unrealized_pnl"] == 100.0
        position = payload["positions"][0]
        assert list(position) == [
            "trading_pair", "connector_name", "account_name", "controller_id",
            "buy_amount_base", "buy_amount_quote", "sell_amount_base",
            "sell_amount_quote", "net_amount_base", "buy_breakeven_price",
            "sell_breakeven_price", "matched_amount_base", "unmatched_amount_base",
            "position_side", "realized_pnl_quote", "unrealized_pnl_quote",
            "executor_count", "executor_ids", "last_updated",
        ]
        assert position["unrealized_pnl_quote"] == 100.0
        assert position["executor_count"] == 2
        assert position["last_updated"] == "2026-01-01T12:00:00"

    async def test_positions_without_a_rate_report_no_unrealized_pnl(self):
        manager, _, _ = build_manager()
        manager._market_data_service.get_rate = MagicMock(return_value=None)
        websocket = RecordingWebSocket()

        await subscribe_and_collect(manager, websocket, "positions")

        payload = websocket.data_frames()[0]["data"]
        assert payload["total_unrealized_pnl"] is None
        assert payload["positions"][0]["unrealized_pnl_quote"] is None

    async def test_logs_frame_sends_only_the_new_entries(self):
        manager, executor_service, _ = build_manager()
        executor_service.get_executor_logs = MagicMock(
            side_effect=[[{"n": 1}], [{"n": 1}, {"n": 2}]]
        )
        websocket = RecordingWebSocket()
        sub = ExecutorSubscription(
            sub_id="executor_logs_e-1", sub_type="executor_logs",
            update_interval=0, executor_id="e-1",
        )

        await run_loop(manager, "executor_logs", sub, websocket, frames=2)

        assert [f["data"] for f in websocket.sent] == [[{"n": 1}], [{"n": 2}]]
        assert [f["total_count"] for f in websocket.sent] == [1, 2]


def make_sub(sub_type, interval=0, **kwargs):
    return ExecutorSubscription(
        sub_id=f"{sub_type}_test", sub_type=sub_type, update_interval=interval, **kwargs
    )


async def run_loop(manager, sub_type, sub, websocket, frames=1, timeout=2.0):
    """Drive one push loop directly until N frames land, then cancel it."""
    push_fn = manager._get_push_fn(sub_type)
    task = asyncio.create_task(push_fn("conn-1", websocket, sub))
    try:
        await websocket.wait_for_frames(frames, timeout)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return task


class TestChangeDetection:
    """A repeated payload must not be re-sent; a changed one must be sent once."""

    async def test_identical_data_is_pushed_only_once(self):
        manager, executor_service, _ = build_manager()
        executor_service.get_executors = AsyncMock(return_value=[{"id": "e-1"}])
        websocket = RecordingWebSocket()
        sub = make_sub("executors")

        task = asyncio.create_task(
            manager._get_push_fn("executors")("conn-1", websocket, sub)
        )
        await websocket.wait_for_frames(1)
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert executor_service.get_executors.await_count > 1
        assert len(websocket.sent) == 1

    async def test_changed_data_is_pushed_exactly_once_more(self):
        manager, executor_service, _ = build_manager()
        executor_service.get_executors = AsyncMock(
            side_effect=[[{"id": "e-1"}], [{"id": "e-1"}], [{"id": "e-2"}], [{"id": "e-2"}]]
        )
        websocket = RecordingWebSocket()
        sub = make_sub("executors")

        await run_loop(manager, "executors", sub, websocket, frames=2)

        assert [f["data"] for f in websocket.sent] == [[{"id": "e-1"}], [{"id": "e-2"}]]

    async def test_the_hash_is_kept_on_the_subscription(self):
        manager, _, _ = build_manager()
        websocket = RecordingWebSocket()
        sub = make_sub("executor_summary")

        await run_loop(manager, "executor_summary", sub, websocket, frames=1)

        assert sub.last_sent_hash is not None


class TestErrorHandling:
    """A failing fetch is logged per channel and the loop keeps polling."""

    @pytest.mark.parametrize(
        "sub_type,attr,is_async",
        [
            ("executors", "get_executors", True),
            ("executor_detail", "get_executor", True),
            ("executor_summary", "get_summary", False),
            ("performance", "get_performance_report", True),
            ("positions", "get_positions_held", False),
            ("executor_logs", "get_executor_logs", False),
        ],
    )
    async def test_a_fetch_failure_does_not_kill_the_loop(self, sub_type, attr, is_async, caplog):
        manager, executor_service, _ = build_manager()
        good = {
            "executors": [{"id": "e-1"}],
            "executor_logs": [{"msg": "x"}],
            "positions": [],
        }.get(sub_type, {"ok": True})
        maker = AsyncMock if is_async else MagicMock
        setattr(executor_service, attr, maker(side_effect=[RuntimeError("boom"), good, good]))
        websocket = RecordingWebSocket()
        sub = make_sub(sub_type, executor_id="e-1")

        with caplog.at_level("ERROR"):
            await run_loop(manager, sub_type, sub, websocket, frames=1)

        assert websocket.sent, "the loop did not recover after the failing poll"
        assert websocket.sent[0]["type"] == sub_type
        assert f"[WS-Exec] {sub_type} push error" in caplog.text

    async def test_an_orchestrator_failure_is_logged_under_its_own_channel(self, caplog):
        manager, _, orchestrator = build_manager()
        orchestrator.get_all_bots_status = MagicMock(
            side_effect=[RuntimeError("boom"), {"bot-1": {"status": "running"}}]
        )
        websocket = RecordingWebSocket()
        sub = make_sub("all_bots_status")

        with caplog.at_level("ERROR"):
            await run_loop(manager, "all_bots_status", sub, websocket, frames=1)

        assert "[WS-Exec] all_bots_status push error" in caplog.text
        assert websocket.sent[0]["type"] == "all_bots_status"


class TestDisconnectStopsTheLoop:
    """A dropped client ends the loop instead of erroring every interval."""

    @pytest.mark.parametrize("error", [WebSocketDisconnect(), RuntimeError("closed")])
    @pytest.mark.parametrize("sub_type", sorted(SUBSCRIPTION_TYPES))
    async def test_a_disconnect_breaks_out_of_the_loop(self, sub_type, error, caplog):
        manager, _, _ = build_manager()
        websocket = RecordingWebSocket(raise_on_send=error)
        sub = make_sub(sub_type, executor_id="e-1", bot_name="bot-1")

        with caplog.at_level("ERROR"):
            task = asyncio.create_task(
                manager._get_push_fn(sub_type)("conn-1", websocket, sub)
            )
            await asyncio.wait_for(task, timeout=2.0)

        assert task.done()
        assert "push error" not in caplog.text


class TestCancellationAndCleanup:
    """Cancellation is swallowed and every teardown path cancels its tasks."""

    @pytest.mark.parametrize("sub_type", sorted(SUBSCRIPTION_TYPES))
    async def test_cancelling_a_loop_raises_nothing(self, sub_type):
        manager, _, _ = build_manager()
        websocket = RecordingWebSocket()
        sub = make_sub(sub_type, interval=5.0, executor_id="e-1", bot_name="bot-1")

        task = asyncio.create_task(manager._get_push_fn(sub_type)("conn-1", websocket, sub))
        await websocket.wait_for_frames(1)
        task.cancel()
        await task

        assert task.done()
        assert task.exception() is None

    async def test_unsubscribe_cancels_the_task(self):
        manager, _, _ = build_manager()
        websocket = RecordingWebSocket()

        await manager.handle_subscribe("conn-1", websocket, SUBSCRIBE_MESSAGES["executors"])
        sub_id = websocket.sent[0]["subscription_id"]
        task = manager._subscriptions["conn-1"][sub_id].task

        await manager.handle_unsubscribe("conn-1", websocket, sub_id)
        await asyncio.sleep(0)

        assert task.cancelled() or task.done()
        assert any(f["type"] == "unsubscribed" for f in websocket.sent)

    async def test_remove_connection_cancels_every_task(self):
        manager, _, _ = build_manager()
        websocket = RecordingWebSocket()

        for sub_type in sorted(SUBSCRIPTION_TYPES):
            await manager.handle_subscribe("conn-1", websocket, SUBSCRIBE_MESSAGES[sub_type])
        tasks = [s.task for s in manager._subscriptions["conn-1"].values()]
        assert len(tasks) == len(SUBSCRIPTION_TYPES)

        manager.remove_connection("conn-1")
        await asyncio.gather(*tasks, return_exceptions=True)

        assert all(t.done() for t in tasks)
        assert "conn-1" not in manager._subscriptions

    async def test_shutdown_cancels_every_connection(self):
        manager, _, _ = build_manager()
        websocket = RecordingWebSocket()

        await manager.handle_subscribe("conn-1", websocket, SUBSCRIBE_MESSAGES["executors"])
        await manager.handle_subscribe("conn-2", websocket, SUBSCRIBE_MESSAGES["positions"])
        tasks = [
            s.task
            for subs in manager._subscriptions.values()
            for s in subs.values()
        ]

        await manager.shutdown()
        await asyncio.gather(*tasks, return_exceptions=True)

        assert all(t.done() for t in tasks)
        assert manager._subscriptions == {}

    async def test_resubscribing_replaces_the_previous_task(self):
        manager, _, _ = build_manager()
        websocket = RecordingWebSocket()

        await manager.handle_subscribe("conn-1", websocket, SUBSCRIBE_MESSAGES["executors"])
        first = next(iter(manager._subscriptions["conn-1"].values())).task
        await manager.handle_subscribe("conn-1", websocket, SUBSCRIBE_MESSAGES["executors"])
        await asyncio.sleep(0)
        subs = manager._subscriptions["conn-1"]
        assert len(subs) == 1
        second = next(iter(subs.values())).task
        assert first is not second
        assert first.done() or first.cancelled()

        manager.remove_connection("conn-1")
        await asyncio.gather(first, second, return_exceptions=True)


class TestPerSubscriptionInterval:
    """Each loop still sleeps its own subscription's interval."""

    @pytest.fixture
    def record_sleeps(self, monkeypatch):
        real_sleep = asyncio.sleep
        calls = []

        async def fake_sleep(delay, *args, **kwargs):
            calls.append(delay)
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        return calls

    async def test_two_subscriptions_sleep_their_own_intervals(self, record_sleeps):
        manager, _, _ = build_manager()
        fast_ws, slow_ws = RecordingWebSocket(), RecordingWebSocket()
        fast = make_sub("executors", interval=0.5)
        slow = make_sub("executor_summary", interval=7.0)

        tasks = [
            asyncio.create_task(manager._get_push_fn("executors")("c", fast_ws, fast)),
            asyncio.create_task(manager._get_push_fn("executor_summary")("c", slow_ws, slow)),
        ]
        await fast_ws.wait_for_frames(1)
        await slow_ws.wait_for_frames(1)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        assert 0.5 in record_sleeps
        assert 7.0 in record_sleeps
        assert set(record_sleeps) <= {0.5, 7.0, 0}

    async def test_the_logs_loop_uses_its_own_interval_too(self, record_sleeps):
        manager, _, _ = build_manager()
        websocket = RecordingWebSocket()
        sub = make_sub("executor_logs", interval=3.0, executor_id="e-1")

        task = asyncio.create_task(
            manager._get_push_fn("executor_logs")("c", websocket, sub)
        )
        await websocket.wait_for_frames(1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert 3.0 in record_sleeps
