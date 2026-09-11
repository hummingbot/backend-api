"""The rows a CLMM/swap route writes must not change when the writer moves.

Session ownership moved out of `routers/gateway_clmm.py` and `routers/gateway_swap.py`
and into `GatewayCLMMService` / `GatewaySwapService` (ARCH-052). Those routes are the
only writers of `gateway_clmm_positions`, `gateway_clmm_events` and `gateway_swaps` —
real trading history that PnL and the transaction poller both read back — so a
refactor there is only safe if the same rows still land, with the same values.

These tests drive the real FastAPI routes with a fake repository behind the service
and pin every column each handler writes, plus the two policies the move had to
preserve: a persistence failure never fails the trade, and "no such row" is still a
404 rather than a defaulted 200.
"""
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deps import get_accounts_service, get_gateway_clmm_service, get_gateway_swap_service
from routers import gateway_clmm, gateway_swap
from services.gateway_clmm_service import GatewayCLMMService
from services.gateway_swap_service import GatewaySwapService

WALLET = "82SggYRE2Vo4jN4a2pk3aQ4SET4ctafZJGbowmCqyHx5"
POOL = "2sf5NYcY4zUPXUSmG6f66mskb24t5F8S11pC1Nz5nQT3"
POSITION = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
SIGNATURE = "5xLmQ5s5xZ9jTqk3Y8bNvW2pR7cH4dF6gJ1kM3nP9qS8tU4vX6yZ2aB5cD7eF9gH1jK3zM5nP7qR9sT"
SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class RepoCalls(list):
    """Every repository call a request made, in order."""

    def payload(self, method):
        """The single payload passed to ``method`` (fails if not called exactly once)."""
        matches = [args for name, args in self if name == method]
        assert len(matches) == 1, f"{method} called {len(matches)} times, expected 1"
        return matches[0]

    def names(self):
        return [name for name, _ in self]


def _repo_class(calls, position=None, failing=False):
    """A repository that records what it was asked to write."""

    class _Repo:
        def __init__(self, session):
            if failing:
                raise RuntimeError("database is down")

        async def create_position(self, position_data):
            calls.append(("create_position", position_data))
            return SimpleNamespace(id=7)

        async def create_event(self, event_data):
            calls.append(("create_event", event_data))
            return SimpleNamespace(id=11)

        async def get_position_by_address(self, address):
            calls.append(("get_position_by_address", address))
            return position

        async def add_to_position_amounts(self, **kwargs):
            calls.append(("add_to_position_amounts", kwargs))

        async def subtract_from_position_amounts(self, **kwargs):
            calls.append(("subtract_from_position_amounts", kwargs))

        async def update_position_fees(self, **kwargs):
            calls.append(("update_position_fees", kwargs))

        async def update_position_liquidity(self, **kwargs):
            calls.append(("update_position_liquidity", kwargs))

        async def close_position(self, address, **kwargs):
            calls.append(("close_position", {"position_address": address, **kwargs}))

        async def create_swap(self, swap_data):
            calls.append(("create_swap", swap_data))
            return SimpleNamespace(id=3)

        async def get_swap_by_tx_hash(self, transaction_hash):
            calls.append(("get_swap_by_tx_hash", transaction_hash))
            return position

        def to_dict(self, swap):
            return {"transaction_hash": swap.transaction_hash}

    return _Repo


def _db_manager():
    manager = SimpleNamespace()

    @asynccontextmanager
    async def session_context():
        yield object()

    manager.get_session_context = session_context
    return manager


def _service(service_class, calls, position=None, failing=False):
    service = service_class(db_manager=_db_manager())
    service.repository_class = _repo_class(calls, position=position, failing=failing)
    return service


def _accounts_service(**gateway_client_methods):
    gateway_client = SimpleNamespace(
        ping=AsyncMock(return_value=True),
        parse_network_id=lambda network_id: tuple(network_id.split("-", 1)),
        get_wallet_address_or_default=AsyncMock(return_value=WALLET),
        **{name: AsyncMock(return_value=value) for name, value in gateway_client_methods.items()},
    )
    return SimpleNamespace(gateway_client=gateway_client)


def _client(accounts_service, clmm_service=None, swap_service=None):
    app = FastAPI()
    app.include_router(gateway_clmm.router)
    app.include_router(gateway_swap.router)
    app.dependency_overrides[get_accounts_service] = lambda: accounts_service
    app.dependency_overrides[get_gateway_clmm_service] = lambda: clmm_service
    app.dependency_overrides[get_gateway_swap_service] = lambda: swap_service
    return TestClient(app, raise_server_exceptions=False)


def _stored_position(**overrides):
    """A position row as the repository hands it back."""
    return SimpleNamespace(**{
        "id": 7,
        "position_address": POSITION,
        "pool_address": POOL,
        "wallet_address": WALLET,
        "base_fee_collected": Decimal("0.5"),
        "quote_fee_collected": Decimal("2.5"),
        "base_token_amount": Decimal("0.0099"),
        "quote_token_amount": Decimal("1.98"),
        **overrides,
    })


# ---------------------------------------------------------------------------
# CLMM open: a position row and its OPEN event
# ---------------------------------------------------------------------------

OPEN_BODY = {
    "connector": "meteora",
    "network": "solana-mainnet-beta",
    "pool_address": POOL,
    "lower_price": 150,
    "upper_price": 250,
    "base_token_amount": 0.01,
    "quote_token_amount": 2,
}

OPEN_RESULT = {
    "signature": SIGNATURE,
    "status": 1,
    "data": {
        "positionAddress": POSITION,
        "positionRent": 0.05788,
        "baseTokenAmountAdded": 0.0099,
        "quoteTokenAmountAdded": 1.98,
        "fee": 0.000011772,
    },
}

POOL_INFO = {"baseTokenAddress": SOL, "quoteTokenAddress": USDC, "price": 200.0}


def test_open_writes_the_same_position_row():
    calls = RepoCalls()
    accounts_service = _accounts_service(clmm_pool_info=POOL_INFO, clmm_open_position=OPEN_RESULT)
    client = _client(accounts_service, clmm_service=_service(GatewayCLMMService, calls))

    response = client.post("/gateway/clmm/open", json=OPEN_BODY)
    assert response.status_code == 200

    assert calls.payload("create_position") == {
        "position_address": POSITION,
        "pool_address": POOL,
        "network": "solana-mainnet-beta",
        "connector": "meteora",
        "wallet_address": WALLET,
        "trading_pair": f"{SOL}-{USDC}",
        "base_token": SOL,
        "quote_token": USDC,
        "status": "OPEN",
        "lower_price": 150.0,
        "upper_price": 250.0,
        # (upper - lower) / lower, computed on the request's Decimals
        "percentage": float((Decimal("250") - Decimal("150")) / Decimal("150")),
        "entry_price": 200.0,
        "current_price": 200.0,
        # The on-chain amounts, never the requested 0.01 / 2.
        "initial_base_token_amount": 0.0099,
        "initial_quote_token_amount": 1.98,
        "position_rent": 0.05788,
        "base_token_amount": 0.0099,
        "quote_token_amount": 1.98,
        "in_range": "UNKNOWN",
        # The columns this route did not use to write at all. It shares one row
        # builder with the poller's discovery sweep (ARCH-103), so the key set no
        # longer depends on which path recorded the position — what the open route
        # cannot know is NULL, and what is genuinely zero at open time is zero.
        "lower_bin_id": None,
        "upper_bin_id": None,
        "base_fee_pending": 0.0,
        "quote_fee_pending": 0.0,
        "base_fee_collected": 0.0,
        "quote_fee_collected": 0.0,
    }


def test_open_writes_the_same_event_row():
    calls = RepoCalls()
    accounts_service = _accounts_service(clmm_pool_info=POOL_INFO, clmm_open_position=OPEN_RESULT)
    client = _client(accounts_service, clmm_service=_service(GatewayCLMMService, calls))

    client.post("/gateway/clmm/open", json=OPEN_BODY)

    assert calls.payload("create_event") == {
        # Keyed to the row create_position just returned, not to the address.
        "position_id": 7,
        "transaction_hash": SIGNATURE,
        "event_type": "OPEN",
        "base_token_amount": 0.0099,
        "quote_token_amount": 1.98,
        "gas_fee": 0.000011772,
        "gas_token": "SOL",
        "status": "CONFIRMED",
    }


def test_open_answers_the_caller_even_when_the_write_fails():
    # The position is open on-chain; a bookkeeping failure must not be reported as a
    # failed open. This policy now lives in one place, so this is what pins it.
    accounts_service = _accounts_service(clmm_pool_info=POOL_INFO, clmm_open_position=OPEN_RESULT)
    client = _client(
        accounts_service,
        clmm_service=_service(GatewayCLMMService, RepoCalls(), failing=True),
    )

    response = client.post("/gateway/clmm/open", json=OPEN_BODY)
    assert response.status_code == 200
    assert response.json()["position_address"] == POSITION
    assert response.json()["status"] == "confirmed"


# ---------------------------------------------------------------------------
# CLMM add / remove: event row plus the position bookkeeping
# ---------------------------------------------------------------------------

def test_add_liquidity_writes_its_event_and_books_the_capital():
    calls = RepoCalls()
    accounts_service = _accounts_service(
        clmm_pool_info={"price": 205.0},
        clmm_add_liquidity={
            "signature": SIGNATURE,
            "status": 1,
            "data": {"baseTokenAmountAdded": 0.005, "quoteTokenAmountAdded": 1.0, "fee": 0.000009},
        },
    )
    client = _client(
        accounts_service,
        clmm_service=_service(GatewayCLMMService, calls, position=_stored_position()),
    )

    response = client.post("/gateway/clmm/add", json={
        "connector": "meteora",
        "network": "solana-mainnet-beta",
        "position_address": POSITION,
        "base_token_amount": 0.005,
        "quote_token_amount": 1,
    })
    assert response.status_code == 200

    assert calls.payload("create_event") == {
        "position_id": 7,
        "transaction_hash": SIGNATURE,
        "event_type": "ADD_LIQUIDITY",
        "base_token_amount": 0.005,
        "quote_token_amount": 1.0,
        "gas_fee": 0.000009,
        "gas_token": "SOL",
        "status": "CONFIRMED",
    }
    # The pool price read for the re-weighting still reaches the booking call.
    assert calls.payload("add_to_position_amounts") == {
        "position_address": POSITION,
        "base_delta": Decimal("0.005"),
        "quote_delta": Decimal("1.0"),
        "entry_price": Decimal("205.0"),
    }


def test_add_liquidity_books_nothing_while_the_transaction_is_only_submitted():
    # A SUBMITTED event is booked by the poller's confirm path instead; booking here
    # too would double-count it.
    calls = RepoCalls()
    accounts_service = _accounts_service(
        clmm_pool_info={"price": 205.0},
        clmm_add_liquidity={"signature": SIGNATURE, "status": 0},
    )
    client = _client(
        accounts_service,
        clmm_service=_service(GatewayCLMMService, calls, position=_stored_position()),
    )

    client.post("/gateway/clmm/add", json={
        "connector": "meteora",
        "network": "solana-mainnet-beta",
        "position_address": POSITION,
        "base_token_amount": 0.005,
    })

    assert calls.payload("create_event")["status"] == "SUBMITTED"
    assert "add_to_position_amounts" not in calls.names()


def test_remove_liquidity_writes_its_event_and_unbooks_the_capital():
    calls = RepoCalls()
    accounts_service = _accounts_service(clmm_remove_liquidity={
        "signature": SIGNATURE,
        "status": 1,
        "data": {"baseTokenAmountRemoved": 0.004, "quoteTokenAmountRemoved": 0.8, "fee": 0.000008},
    })
    client = _client(
        accounts_service,
        clmm_service=_service(GatewayCLMMService, calls, position=_stored_position()),
    )

    response = client.post("/gateway/clmm/remove", json={
        "connector": "meteora",
        "network": "solana-mainnet-beta",
        "position_address": POSITION,
        "percentage_to_remove": 50,
    })
    assert response.status_code == 200

    # No "percentage" key: GatewayCLMMEvent has no such column.
    assert calls.payload("create_event") == {
        "position_id": 7,
        "transaction_hash": SIGNATURE,
        "event_type": "REMOVE_LIQUIDITY",
        "base_token_amount": 0.004,
        "quote_token_amount": 0.8,
        "gas_fee": 0.000008,
        "gas_token": "SOL",
        "status": "CONFIRMED",
    }
    assert calls.payload("subtract_from_position_amounts") == {
        "position_address": POSITION,
        "base_delta": Decimal("0.004"),
        "quote_delta": Decimal("0.8"),
    }


def test_an_event_for_an_unknown_position_is_skipped_not_invented():
    calls = RepoCalls()
    accounts_service = _accounts_service(clmm_remove_liquidity={"signature": SIGNATURE, "status": 1})
    client = _client(
        accounts_service,
        clmm_service=_service(GatewayCLMMService, calls, position=None),
    )

    response = client.post("/gateway/clmm/remove", json={
        "connector": "meteora",
        "network": "solana-mainnet-beta",
        "position_address": POSITION,
        "percentage_to_remove": 50,
    })

    assert response.status_code == 200
    assert "create_event" not in calls.names()


# ---------------------------------------------------------------------------
# CLMM collect-fees and close: the fee accounting
# ---------------------------------------------------------------------------

def test_collect_fees_writes_its_event_and_rolls_the_collected_totals():
    calls = RepoCalls()
    accounts_service = _accounts_service(
        clmm_positions_owned=[{"address": POSITION, "baseFeeAmount": 0.01, "quoteFeeAmount": 2.0}],
        clmm_collect_fees={
            "signature": SIGNATURE,
            "status": 1,
            "data": {"baseFeeAmountCollected": 0.01, "quoteFeeAmountCollected": 2.0, "fee": 0.000005},
        },
    )
    client = _client(
        accounts_service,
        clmm_service=_service(GatewayCLMMService, calls, position=_stored_position()),
    )

    response = client.post("/gateway/clmm/collect-fees", json={
        "connector": "meteora",
        "network": "solana-mainnet-beta",
        "position_address": POSITION,
    })
    assert response.status_code == 200

    assert calls.payload("create_event") == {
        "position_id": 7,
        "transaction_hash": SIGNATURE,
        "event_type": "COLLECT_FEES",
        "base_fee_collected": 0.01,
        "quote_fee_collected": 2.0,
        "gas_fee": 0.000005,
        "gas_token": "SOL",
        "status": "CONFIRMED",
    }
    # Added to what the row already held (0.5 / 2.5), and pending reset to zero.
    assert calls.payload("update_position_fees") == {
        "position_address": POSITION,
        "base_fee_collected": Decimal("0.51"),
        "quote_fee_collected": Decimal("4.5"),
        "base_fee_pending": Decimal("0"),
        "quote_fee_pending": Decimal("0"),
    }


def test_close_writes_its_event_and_closes_the_row_once_gateway_agrees(monkeypatch):
    import services.gateway_clmm_service as service_module

    # The close path waits for the transaction to propagate before verifying.
    monkeypatch.setattr(service_module.asyncio, "sleep", AsyncMock())

    calls = RepoCalls()
    accounts_service = _accounts_service(
        clmm_positions_owned=[{"address": POSITION, "baseFeeAmount": 0.01,
                               "quoteFeeAmount": 2.0, "price": 198.0}],
        clmm_close_position={
            "signature": SIGNATURE,
            "status": 1,
            "data": {
                "baseTokenAmountRemoved": 0.0099,
                "quoteTokenAmountRemoved": 1.98,
                "baseFeeAmountCollected": 0.01,
                "quoteFeeAmountCollected": 2.0,
                "positionRentRefunded": 0.05788,
                "fee": 0.000011,
            },
        },
        # Gateway no longer knows the position: proof the close landed.
        clmm_position_info={"error": "Position not found", "status": 404},
    )
    client = _client(
        accounts_service,
        clmm_service=_service(GatewayCLMMService, calls, position=_stored_position()),
    )

    response = client.post("/gateway/clmm/close", json={
        "connector": "meteora",
        "network": "solana-mainnet-beta",
        "position_address": POSITION,
    })
    assert response.status_code == 200

    assert calls.payload("create_event") == {
        "position_id": 7,
        "transaction_hash": SIGNATURE,
        "event_type": "CLOSE",
        "base_token_amount": 0.0099,
        "quote_token_amount": 1.98,
        "base_fee_collected": 0.01,
        "quote_fee_collected": 2.0,
        "gas_fee": 0.000011,
        "gas_token": "SOL",
        "status": "CONFIRMED",
    }
    assert calls.payload("update_position_liquidity") == {
        "position_address": POSITION,
        "base_token_amount": Decimal("0.0099"),
        "quote_token_amount": Decimal("1.98"),
        "current_price": Decimal("198.0"),
    }
    assert calls.payload("close_position") == {
        "position_address": POSITION,
        "position_rent_refunded": Decimal("0.05788"),
    }


def test_close_releases_the_session_before_waiting_for_propagation(monkeypatch):
    """The two-second propagation wait must not hold a pooled connection.

    The close path books the fees, lets the session go, waits, and only then opens a
    second short session to mark the row CLOSED. Holding one connection idle per
    close is how a fleet closing several positions at once drains the pool while the
    database has nothing to do (PERF-105).
    """
    import services.gateway_clmm_service as service_module

    depth = []       # sessions currently open
    timeline = []    # what happened, in order
    sessions_open_during_sleep = []

    @asynccontextmanager
    async def session_context():
        depth.append(1)
        timeline.append("session_open")
        try:
            yield object()
        finally:
            depth.pop()
            timeline.append("session_close")

    async def fake_sleep(_seconds):
        timeline.append("sleep")
        sessions_open_during_sleep.append(len(depth))

    monkeypatch.setattr(service_module.asyncio, "sleep", fake_sleep)

    calls = RepoCalls()
    accounts_service = _accounts_service(
        clmm_positions_owned=[{"address": POSITION, "baseFeeAmount": 0.01,
                               "quoteFeeAmount": 2.0, "price": 198.0}],
        clmm_close_position={
            "signature": SIGNATURE,
            "status": 1,
            "data": {
                "baseTokenAmountRemoved": 0.0099,
                "quoteTokenAmountRemoved": 1.98,
                "baseFeeAmountCollected": 0.01,
                "quoteFeeAmountCollected": 2.0,
                "positionRentRefunded": 0.05788,
                "fee": 0.000011,
            },
        },
        clmm_position_info={"error": "Position not found", "status": 404},
    )
    service = GatewayCLMMService(db_manager=SimpleNamespace(get_session_context=session_context))
    service.repository_class = _repo_class(calls, position=_stored_position())
    client = _client(accounts_service, clmm_service=service)

    response = client.post("/gateway/clmm/close", json={
        "connector": "meteora",
        "network": "solana-mainnet-beta",
        "position_address": POSITION,
    })
    assert response.status_code == 200

    # No connection was checked out while we waited on the chain.
    assert sessions_open_during_sleep == [0]
    # The bookkeeping commits, then the wait, then a second session for the close.
    # (Earlier pairs belong to the route's own wallet lookup, before the close.)
    assert timeline.count("sleep") == 1
    assert timeline[-3:] == ["sleep", "session_open", "session_close"]
    # And the same rows still land, in the same order.
    assert calls.names()[-5:] == [
        "get_position_by_address", "create_event",
        "update_position_fees", "update_position_liquidity",
        "close_position",
    ]


def test_a_failed_close_mutates_nothing_but_still_files_the_event():
    calls = RepoCalls()
    accounts_service = _accounts_service(
        clmm_positions_owned=[{"address": POSITION, "baseFeeAmount": 0.01, "quoteFeeAmount": 2.0}],
        clmm_close_position={"signature": SIGNATURE, "status": -1, "data": {"fee": 0.000011}},
    )
    client = _client(
        accounts_service,
        clmm_service=_service(GatewayCLMMService, calls, position=_stored_position()),
    )

    response = client.post("/gateway/clmm/close", json={
        "connector": "meteora",
        "network": "solana-mainnet-beta",
        "position_address": POSITION,
    })
    assert response.status_code == 200

    assert calls.payload("create_event")["status"] == "FAILED"
    # No fee booking, no price update, no close: a reverted close changes nothing.
    assert "update_position_fees" not in calls.names()
    assert "close_position" not in calls.names()


# ---------------------------------------------------------------------------
# Swaps
# ---------------------------------------------------------------------------

def test_execute_swap_writes_the_same_swap_row():
    calls = RepoCalls()
    accounts_service = _accounts_service(execute_swap={
        "signature": SIGNATURE,
        "status": 1,
        "data": {
            "amountIn": 0.01,
            "amountOut": 0.878444,
            "fee": 0.000005,
            "slippagePct": 0.5,
            "poolAddress": POOL,
        },
    })
    client = _client(accounts_service, swap_service=_service(GatewaySwapService, calls))

    response = client.post("/gateway/swap/execute", json={
        "connector": "jupiter/router",
        "network": "solana-mainnet-beta",
        "trading_pair": "SOL-USDC",
        "side": "SELL",
        "amount": 0.01,
        "slippage_pct": 1,
    })
    assert response.status_code == 200

    assert calls.payload("create_swap") == {
        "transaction_hash": SIGNATURE,
        "network": "solana-mainnet-beta",
        # The base venue name: "jupiter/router" files under "jupiter".
        "connector": "jupiter",
        "wallet_address": WALLET,
        "trading_pair": "SOL-USDC",
        "base_token": "SOL",
        "quote_token": "USDC",
        "side": "SELL",
        "input_amount": 0.01,
        "output_amount": 0.878444,
        "price": float(Decimal("0.878444") / Decimal("0.01")),
        # What Gateway says it applied, not the 1 that was asked for.
        "slippage_pct": 0.5,
        "gas_fee": 0.000005,
        "gas_token": "SOL",
        "status": "CONFIRMED",
        "pool_address": POOL,
    }


def test_a_submitted_swap_records_placeholders_and_no_gas():
    calls = RepoCalls()
    accounts_service = _accounts_service(execute_swap={"signature": SIGNATURE, "status": 0})
    client = _client(accounts_service, swap_service=_service(GatewaySwapService, calls))

    response = client.post("/gateway/swap/execute", json={
        "connector": "jupiter",
        "network": "solana-mainnet-beta",
        "trading_pair": "SOL-USDC",
        "side": "BUY",
        "amount": 0.01,
    })
    assert response.status_code == 200

    row = calls.payload("create_swap")
    assert row["status"] == "SUBMITTED"
    # BUY: the requested amount is the base leg out; the unknown leg stays 0.
    assert (row["input_amount"], row["output_amount"], row["price"]) == (0.0, 0.01, 0.0)
    assert row["gas_fee"] is None and row["gas_token"] is None
    assert row["pool_address"] is None
    # The response says nothing about a fill it does not know.
    assert response.json()["output_amount"] is None


def test_a_swap_is_still_reported_when_the_write_fails():
    accounts_service = _accounts_service(execute_swap={
        "signature": SIGNATURE, "status": 1,
        "data": {"amountIn": 0.01, "amountOut": 0.878444},
    })
    client = _client(
        accounts_service,
        swap_service=_service(GatewaySwapService, RepoCalls(), failing=True),
    )

    response = client.post("/gateway/swap/execute", json={
        "connector": "jupiter",
        "network": "solana-mainnet-beta",
        "trading_pair": "SOL-USDC",
        "side": "SELL",
        "amount": 0.01,
    })

    assert response.status_code == 200
    assert response.json()["transaction_hash"] == SIGNATURE


def test_an_unknown_swap_is_a_404_and_an_unreachable_database_is_not():
    # The trap of routing reads through a helper that swallows exceptions and returns
    # a default: a database outage would answer 404 "Swap not found", which reads as
    # "that swap never happened".
    accounts_service = _accounts_service()

    client = _client(accounts_service, swap_service=_service(GatewaySwapService, RepoCalls()))
    assert client.get(f"/gateway/swaps/{SIGNATURE}/status").status_code == 404

    client = _client(
        accounts_service,
        swap_service=_service(GatewaySwapService, RepoCalls(), failing=True),
    )
    assert client.get(f"/gateway/swaps/{SIGNATURE}/status").status_code == 500


def test_a_known_swap_is_returned_as_the_repository_renders_it():
    swap = SimpleNamespace(transaction_hash=SIGNATURE)
    client = _client(
        _accounts_service(),
        swap_service=_service(GatewaySwapService, RepoCalls(), position=swap),
    )

    response = client.get(f"/gateway/swaps/{SIGNATURE}/status")
    assert response.status_code == 200
    assert response.json() == {"transaction_hash": SIGNATURE}


# ---------------------------------------------------------------------------
# The failed-write path keeps its single transaction-id parser
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_reverted_write_is_filed_against_the_position_it_belonged_to():
    from services.gateway_client import GatewayError

    calls = RepoCalls()
    service = _service(GatewayCLMMService, calls, position=_stored_position())
    error = GatewayError(f"Transaction {SIGNATURE} landed on-chain but failed: 0x1771", status=500)

    await gateway_clmm._record_failed_write(
        service, error, event_type="CLOSE", position_address=POSITION)

    assert calls.payload("create_event") == {
        "position_id": 7,
        "transaction_hash": SIGNATURE,
        "event_type": "CLOSE",
        "status": "FAILED",
        "error_message": str(error),
    }


@pytest.mark.asyncio
async def test_a_failure_that_never_reached_the_chain_writes_nothing():
    from services.gateway_client import GatewayError

    calls = RepoCalls()
    service = _service(GatewayCLMMService, calls, position=_stored_position())

    await gateway_clmm._record_failed_write(
        service,
        GatewayError("Simulation failed: insufficient funds", status=400),
        event_type="CLOSE",
        position_address=POSITION,
    )

    assert calls.names() == []


# ---------------------------------------------------------------------------
# The read envelopes the handlers used to hand-build
# ---------------------------------------------------------------------------

def _search_repo_class(calls, positions, refreshed):
    class _Repo:
        def __init__(self, session):
            pass

        async def get_positions(self, **kwargs):
            calls.append(("get_positions", kwargs))
            return positions

        async def get_position_by_address(self, address):
            return _stored_position(position_address=address, connector="meteora",
                                    network="solana-mainnet-beta")

        async def get_position_events(self, **kwargs):
            calls.append(("get_position_events", kwargs))
            return [SimpleNamespace(id=1), SimpleNamespace(id=2)]

        def position_to_dict(self, position):
            return {"position_address": position.position_address}

        def event_to_dict(self, event):
            return {"id": event.id}

        async def update_position_liquidity(self, **kwargs):
            refreshed.append(kwargs)

        async def update_position_fees(self, **kwargs):
            pass

        async def close_position(self, address, **kwargs):
            pass

    return _Repo


def _search_service(calls, positions, refreshed=None):
    service = GatewayCLMMService(db_manager=_db_manager())
    service.repository_class = _search_repo_class(calls, positions, refreshed if refreshed is not None else [])
    return service


def test_position_search_answers_the_same_paginated_envelope():
    calls = RepoCalls()
    positions = [_stored_position(position_address=f"POS-{i}") for i in range(2)]
    client = _client(_accounts_service(), clmm_service=_search_service(calls, positions))

    response = client.post("/gateway/clmm/positions/search?limit=2000&offset=10")

    assert response.status_code == 200
    assert response.json() == {
        "data": [{"position_address": "POS-0"}, {"position_address": "POS-1"}],
        "pagination": {
            # Clamped to the 1000 ceiling, and echoed clamped.
            "limit": 1000,
            "offset": 10,
            "has_more": False,
            "total_count": 12,
        },
    }
    assert calls.payload("get_positions")["limit"] == 1000


def test_a_full_page_reports_more_rather_than_a_wrong_total():
    calls = RepoCalls()
    positions = [_stored_position(position_address=f"POS-{i}") for i in range(2)]
    client = _client(_accounts_service(), clmm_service=_search_service(calls, positions))

    body = client.post("/gateway/clmm/positions/search?limit=2").json()

    assert body["pagination"]["has_more"] is True
    assert body["pagination"]["total_count"] is None


def test_a_refreshing_search_writes_back_what_gateway_reports():
    calls, refreshed = RepoCalls(), []
    positions = [_stored_position(position_address=POSITION, connector="meteora",
                                  network="solana-mainnet-beta")]
    accounts_service = _accounts_service(clmm_positions_owned=[{
        "address": POSITION,
        "price": 200.0,
        "lowerPrice": 150.0,
        "upperPrice": 250.0,
        "baseTokenAmount": 0.0099,
        "quoteTokenAmount": 1.98,
    }])
    client = _client(
        accounts_service,
        clmm_service=_search_service(calls, positions, refreshed),
    )

    response = client.post("/gateway/clmm/positions/search?refresh=true")

    assert response.status_code == 200
    assert refreshed == [{
        "position_address": POSITION,
        "base_token_amount": Decimal("0.0099"),
        "quote_token_amount": Decimal("1.98"),
        "in_range": "IN_RANGE",
        "current_price": Decimal("200.0"),
    }]


def test_position_events_answer_the_same_envelope():
    calls = RepoCalls()
    client = _client(_accounts_service(), clmm_service=_search_service(calls, []))

    response = client.get(f"/gateway/clmm/positions/{POSITION}/events?event_type=CLOSE&limit=5")

    assert response.status_code == 200
    assert response.json() == {"data": [{"id": 1}, {"id": 2}], "total_count": 2}
    assert calls.payload("get_position_events") == {
        "position_address": POSITION,
        "event_type": "CLOSE",
        "limit": 5,
    }
