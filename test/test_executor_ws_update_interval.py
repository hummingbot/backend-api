"""
Tests for the /ws/executors update-interval bounds (READ-057).

The bounds used to be module-level literals in services/executor_ws_manager.py;
they now come from MarketDataSettings, so the MARKET_DATA_WS_EXECUTOR_* env vars
actually reach the executor WebSocket.

Run with: pytest test/test_executor_ws_update_interval.py -v
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import MarketDataSettings, settings
from services.executor_ws_manager import ExecutorWebSocketManager, _clamp_interval


@pytest.fixture
def custom_bounds(monkeypatch):
    """Rebuild MarketDataSettings from the env, as an operator override would."""

    def _apply(**env):
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        monkeypatch.setattr(settings, "market_data", MarketDataSettings())

    return _apply


class TestClampIntervalDefaults:
    """Default behaviour must be identical to the old hardcoded literals."""

    def test_missing_interval_falls_back_to_two_seconds(self):
        assert _clamp_interval(None) == 2.0

    def test_interval_below_the_floor_is_raised_to_half_a_second(self):
        assert _clamp_interval(0.01) == 0.5

    def test_interval_above_the_ceiling_is_capped_at_sixty_seconds(self):
        assert _clamp_interval(1000.0) == 60.0

    def test_interval_inside_the_range_is_left_alone(self):
        assert _clamp_interval(5.0) == 5.0

    def test_executor_floor_is_stricter_than_the_market_data_floor(self):
        md = MarketDataSettings()
        assert md.ws_executor_min_update_interval > md.ws_min_update_interval


class TestClampIntervalIsConfigurable:
    """The MARKET_DATA_WS_EXECUTOR_* env vars must change the clamping."""

    def test_configured_floor_is_honoured(self, custom_bounds):
        custom_bounds(MARKET_DATA_WS_EXECUTOR_MIN_UPDATE_INTERVAL=5.0)
        assert _clamp_interval(1.0) == 5.0

    def test_configured_ceiling_is_honoured(self, custom_bounds):
        custom_bounds(MARKET_DATA_WS_EXECUTOR_MAX_UPDATE_INTERVAL=10.0)
        assert _clamp_interval(30.0) == 10.0

    def test_configured_default_is_honoured(self, custom_bounds):
        custom_bounds(MARKET_DATA_WS_EXECUTOR_DEFAULT_UPDATE_INTERVAL=7.5)
        assert _clamp_interval(None) == 7.5

    def test_market_data_bounds_do_not_leak_into_the_executor_clamp(self, custom_bounds):
        custom_bounds(
            MARKET_DATA_WS_MIN_UPDATE_INTERVAL=0.01,
            MARKET_DATA_WS_MAX_UPDATE_INTERVAL=3600.0,
        )
        assert _clamp_interval(0.01) == 0.5
        assert _clamp_interval(3600.0) == 60.0


class TestSubscribeAcknowledgement:
    """handle_subscribe must apply and report the configured clamp."""

    @staticmethod
    def _manager():
        executor_service = MagicMock()
        executor_service.get_executors = AsyncMock(return_value=[])
        return ExecutorWebSocketManager(
            executor_service=executor_service,
            market_data_service=MagicMock(),
        )

    @staticmethod
    async def _subscribe(manager, websocket, msg):
        await manager.handle_subscribe("conn-1", websocket, msg)
        manager.remove_connection("conn-1")

    async def test_ack_reports_the_configured_floor(self, custom_bounds):
        custom_bounds(MARKET_DATA_WS_EXECUTOR_MIN_UPDATE_INTERVAL=4.0)
        manager = self._manager()
        websocket = MagicMock()
        websocket.send_json = AsyncMock()

        await self._subscribe(manager, websocket, {"type": "executors", "update_interval": 0.5})

        ack = next(
            call.args[0]
            for call in websocket.send_json.call_args_list
            if call.args[0].get("type") == "subscribed"
        )
        assert ack["update_interval"] == 4.0

    async def test_ack_reports_the_default_when_none_is_requested(self):
        manager = self._manager()
        websocket = MagicMock()
        websocket.send_json = AsyncMock()

        await self._subscribe(manager, websocket, {"type": "executors"})

        ack = next(
            call.args[0]
            for call in websocket.send_json.call_args_list
            if call.args[0].get("type") == "subscribed"
        )
        assert ack["update_interval"] == 2.0
