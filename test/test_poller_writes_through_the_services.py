"""The poller decides what the chain says; the services decide what gets written.

The poller used to build `GatewayCLMMRepository` / `GatewaySwapRepository` itself in six
places and hold one session open across a whole cycle of Gateway calls (ARCH-103). It now
reads its work as plain dicts and writes through the same services the `/gateway/clmm/*`
and `/gateway/swap*` routes persist through.

Every call site changed, so these pin the decisions that must have survived the move: a
transaction is only aged out after a poll that actually reached the chain, a NOT_FOUND is
given its grace window before it counts as dropped, and a position is only closed after
the consecutive-miss gate says so.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.gateway_transaction_poller import GatewayTransactionPoller


def _poller(poll_result=None, position_info=None):
    """A poller with both services stubbed, so writes are observable as calls."""
    poller = object.__new__(GatewayTransactionPoller)
    poller.max_retry_age = 3600
    poller._position_missing_strikes = {}
    poller.gateway_client = SimpleNamespace(
        ping=AsyncMock(return_value=True),
        poll_transaction=AsyncMock(return_value=poll_result),
        clmm_position_info=AsyncMock(return_value=position_info),
    )
    poller.swap_service = SimpleNamespace(update_swap_status=AsyncMock())
    poller.clmm_service = SimpleNamespace(
        record_event_confirmed=AsyncMock(),
        update_event_status=AsyncMock(),
        mark_position_closed=AsyncMock(),
        record_position_state=AsyncMock(),
    )
    return poller


def _swap(age_seconds=10):
    return {
        "transaction_hash": "TX",
        "network": "solana-mainnet-beta",
        "timestamp": datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    }


def _event(age_seconds=10, network="solana-mainnet-beta"):
    return {
        "transaction_hash": "TX",
        "network": network,
        "position_address": "POS",
        "timestamp": datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    }


def _position():
    return {
        "id": 7,
        "position_address": "POS",
        "wallet_address": "WALLET",
        "connector": "meteora",
        "network": "solana-mainnet-beta",
    }


# ---------------------------------------------------------------------------
# Swaps
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_confirmed_swap_is_recorded_with_its_gas():
    poller = _poller({"txStatus": 1, "fee": 0.000005})
    await poller._poll_swap_transaction(_swap())

    written = poller.swap_service.update_swap_status.await_args.kwargs
    assert written["transaction_hash"] == "TX"
    assert written["status"] == "CONFIRMED"
    assert float(written["gas_fee"]) == 0.000005
    assert written["gas_token"] == "SOL"


@pytest.mark.asyncio
async def test_a_transient_gateway_error_writes_nothing():
    # No information came back, so no state may change — the swap is polled again.
    poller = _poller({"error": "Gateway 500", "status": 500})
    await poller._poll_swap_transaction(_swap(age_seconds=99999))
    poller.swap_service.update_swap_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_not_found_is_only_dropped_once_the_blockhash_can_no_longer_be_valid():
    poller = _poller({"txStatus": -2})
    await poller._poll_swap_transaction(_swap(age_seconds=30))
    poller.swap_service.update_swap_status.assert_not_awaited()

    await poller._poll_swap_transaction(_swap(age_seconds=600))
    assert poller.swap_service.update_swap_status.await_args.kwargs["status"] == "FAILED"


@pytest.mark.asyncio
async def test_a_swap_still_pending_past_the_retry_age_is_timed_out():
    poller = _poller({"txStatus": 0})
    await poller._poll_swap_transaction(_swap(age_seconds=60))
    poller.swap_service.update_swap_status.assert_not_awaited()

    await poller._poll_swap_transaction(_swap(age_seconds=7200))
    written = poller.swap_service.update_swap_status.await_args.kwargs
    assert written["status"] == "FAILED"
    assert written["error_message"] == "Transaction confirmation timeout"


# ---------------------------------------------------------------------------
# CLMM events
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_confirmed_event_is_booked_against_its_position():
    # One service call: the status and the position bookkeeping it owes share a session.
    poller = _poller({"txStatus": 1, "fee": 0.000011772})
    await poller._poll_clmm_event_transaction(_event())

    written = poller.clmm_service.record_event_confirmed.await_args.kwargs
    assert written["transaction_hash"] == "TX"
    assert float(written["gas_fee"]) == 0.000011772
    poller.clmm_service.update_event_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_event_records_the_reason_and_books_nothing():
    poller = _poller({"txStatus": -1, "error": "SLIPPAGE_EXCEEDED (0x1771)", "fee": 0.000005})
    await poller._poll_clmm_event_transaction(_event())

    written = poller.clmm_service.update_event_status.await_args.kwargs
    assert written["status"] == "FAILED"
    assert "SLIPPAGE_EXCEEDED" in written["error_message"]
    poller.clmm_service.record_event_confirmed.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_event_whose_position_row_is_missing_is_not_polled():
    # Without the position there is no chain to ask about the event.
    poller = _poller({"txStatus": 1})
    await poller._poll_clmm_event_transaction(_event(network=None))

    poller.gateway_client.poll_transaction.assert_not_awaited()
    poller.clmm_service.record_event_confirmed.assert_not_awaited()


# ---------------------------------------------------------------------------
# Position state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_live_position_writes_back_one_reading():
    poller = _poller(position_info={
        "address": "POS",
        "price": 200.0,
        "lowerPrice": 150.0,
        "upperPrice": 250.0,
        "baseTokenAmount": 0.0099,
        "quoteTokenAmount": 1.98,
        "baseFeeAmount": 0.00031,
        "quoteFeeAmount": 0.062,
    })
    await poller._refresh_position_state(_position())

    written = poller.clmm_service.record_position_state.await_args.kwargs
    assert written["position_address"] == "POS"
    assert written["in_range"] == "IN_RANGE"
    assert float(written["current_price"]) == 200.0
    assert float(written["base_fee_pending"]) == 0.00031
    poller.clmm_service.mark_position_closed.assert_not_awaited()


@pytest.mark.asyncio
async def test_zero_liquidity_closes_the_position():
    poller = _poller(position_info={
        "address": "POS", "price": 200.0, "lowerPrice": 150.0, "upperPrice": 250.0,
        "baseTokenAmount": 0, "quoteTokenAmount": 0,
    })
    await poller._refresh_position_state(_position())

    poller.clmm_service.mark_position_closed.assert_awaited_once_with("POS")
    poller.clmm_service.record_position_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_position_closes_only_after_three_consecutive_misses():
    # Gateway 500s on transient RPC trouble as well as on a position that is gone,
    # so one miss must never close a live position.
    poller = _poller(position_info={"error": "not found", "status": 500})

    for _ in range(GatewayTransactionPoller.MISSING_STRIKES_TO_CLOSE - 1):
        await poller._refresh_position_state(_position())
        poller.clmm_service.mark_position_closed.assert_not_awaited()

    await poller._refresh_position_state(_position())
    poller.clmm_service.mark_position_closed.assert_awaited_once_with("POS")
    # The strike count is cleared with the position it belonged to.
    assert "POS" not in poller._position_missing_strikes


@pytest.mark.asyncio
async def test_a_successful_read_clears_earlier_misses():
    poller = _poller(position_info={"error": "not found", "status": 500})
    await poller._refresh_position_state(_position())
    assert poller._position_missing_strikes["POS"] == 1

    poller.gateway_client.clmm_position_info = AsyncMock(return_value={
        "address": "POS", "price": 200.0, "lowerPrice": 150.0, "upperPrice": 250.0,
        "baseTokenAmount": 0.0099, "quoteTokenAmount": 1.98,
    })
    await poller._refresh_position_state(_position())
    assert "POS" not in poller._position_missing_strikes
