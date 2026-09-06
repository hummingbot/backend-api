"""Tests for UnifiedConnectorService.sync_pair_derived_state (issue #207).

Connectors are created with trading_pairs=[] and pairs registered dynamically, but
throttler pair-templated rate limits are built from that list at init. The helper
must re-sync them, idempotently, on every registration.
"""
import asyncio

from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.api_throttler.data_types import RateLimit

from services.unified_connector_service import UnifiedConnectorService


class FakePairLimitsConnector:
    """Mimics a connector with pair-templated rate limits (bybit-style)."""

    def __init__(self, trading_pairs=None):
        self._trading_pairs = trading_pairs
        self._throttler = AsyncThrottler(rate_limits=self._build_limits(trading_pairs or []))

    @staticmethod
    def _build_limits(trading_pairs):
        limits = [RateLimit(limit_id="global", limit=10, time_interval=1)]
        for pair in trading_pairs:
            limits.append(RateLimit(limit_id=f"order/create-{pair}", limit=5, time_interval=1))
        return limits

    @property
    def rate_limits_rules(self):
        return self._build_limits(self._trading_pairs or [])


def _run(coro):
    return asyncio.run(coro)


def test_pair_appended():
    connector = FakePairLimitsConnector(trading_pairs=[])
    _run(UnifiedConnectorService.sync_pair_derived_state(connector, "BTC-USDT"))
    assert connector._trading_pairs == ["BTC-USDT"]


def test_throttler_learns_pair_scoped_limit_in_place():
    connector = FakePairLimitsConnector(trading_pairs=[])
    # WebAssistantsFactory captures the throttler instance at init — the fix must
    # mutate that same object, not replace it.
    original_throttler = connector._throttler

    _run(UnifiedConnectorService.sync_pair_derived_state(connector, "BTC-USDT"))

    assert connector._throttler is original_throttler
    limit_ids = {limit.limit_id for limit in original_throttler._rate_limits}
    assert "order/create-BTC-USDT" in limit_ids


def test_idempotent_no_duplicate_limits():
    connector = FakePairLimitsConnector(trading_pairs=[])
    _run(UnifiedConnectorService.sync_pair_derived_state(connector, "BTC-USDT"))
    _run(UnifiedConnectorService.sync_pair_derived_state(connector, "BTC-USDT"))

    assert connector._trading_pairs == ["BTC-USDT"]
    limit_ids = [limit.limit_id for limit in connector._throttler._rate_limits]
    assert limit_ids.count("order/create-BTC-USDT") == 1


def test_none_trading_pairs_initialized():
    connector = FakePairLimitsConnector(trading_pairs=None)
    _run(UnifiedConnectorService.sync_pair_derived_state(connector, "RLUSD-XRP"))
    assert connector._trading_pairs == ["RLUSD-XRP"]


def test_minimal_connector_does_not_raise():
    class Minimal:
        pass

    minimal = Minimal()
    _run(UnifiedConnectorService.sync_pair_derived_state(minimal, "BTC-USDT"))
    assert minimal._trading_pairs == ["BTC-USDT"]


def test_unknown_pair_rejected_and_not_registered():
    """A pair the connector's symbol map cannot resolve must never enter
    _trading_pairs: there is no rollback, and a poisoned entry breaks per-pair
    status polling and rules rebuilds for the connector's lifetime."""

    class ValidatingConnector(FakePairLimitsConnector):
        KNOWN = {"BTC-USDT"}

        async def exchange_symbol_associated_to_pair(self, trading_pair):
            if trading_pair not in self.KNOWN:
                raise KeyError(trading_pair)
            return trading_pair.replace("-", "")

    connector = ValidatingConnector(trading_pairs=[])

    try:
        _run(UnifiedConnectorService.sync_pair_derived_state(connector, "BTC-USD"))
        raise AssertionError("expected ValueError for unknown pair")
    except ValueError:
        pass
    assert connector._trading_pairs == []  # nothing registered, nothing to roll back

    # A valid pair still registers normally afterwards
    _run(UnifiedConnectorService.sync_pair_derived_state(connector, "BTC-USDT"))
    assert connector._trading_pairs == ["BTC-USDT"]


def test_already_registered_pair_skips_validation():
    """Re-syncing an already-registered pair must not re-validate — the symbol
    map may be temporarily unavailable, and the pair was validated on entry."""

    class BrokenResolverConnector(FakePairLimitsConnector):
        async def exchange_symbol_associated_to_pair(self, trading_pair):
            raise ConnectionError("symbol map fetch failed")

    connector = BrokenResolverConnector(trading_pairs=["BTC-USDT"])
    _run(UnifiedConnectorService.sync_pair_derived_state(connector, "BTC-USDT"))
    assert connector._trading_pairs == ["BTC-USDT"]
