"""The rows an AMM route writes must not change when the writer moves.

Session ownership moved out of `routers/gateway_amm.py` into `GatewayAMMService`, and the
bot-run cleanup out of `routers/archived_bots.py` into `BotsOrchestrator` (ARCH-109), the
last two routers that still opened their own transactions. Those AMM routes are the only
writers of `gateway_amm_events` and `gateway_amm_positions` — the AMM history and the
DAMM v2 position book — so moving the writer is only safe if the same rows still land,
with the same values.

These tests drive the real FastAPI routes with a fake repository behind the service and
pin every column each handler writes, the two paginated envelopes it hands back, and the
policies the move had to preserve: a persistence failure never fails a write that already
happened on-chain, and only a failure that actually reached the chain is recorded.
"""
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deps import get_accounts_service, get_bots_orchestrator, get_gateway_amm_service
from routers import archived_bots, gateway_amm
from services.gateway_amm_service import GatewayAMMService

WALLET = "82SggYRE2Vo4jN4a2pk3aQ4SET4ctafZJGbowmCqyHx5"
POOL = "2sf5NYcY4zUPXUSmG6f66mskb24t5F8S11pC1Nz5nQT3"
POSITION = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
SIGNATURE = "5xLmQ5s5xZ9jTqk3Y8bNvW2pR7cH4dF6gJ1kM3nP9qS8tU4vX6yZ2aB5cD7eF9gH1jK3zM5nP7qR9sT"
SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SYMBOLS = {SOL: "SOL", USDC: "USDC"}

POOL_INFO = {"baseTokenAddress": SOL, "quoteTokenAddress": USDC, "price": 200.0}


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


def _repo_class(calls, position=None, events=(), positions=(), failing=False):
    """A repository that records what it was asked to write."""

    class _Repo:
        def __init__(self, session):
            if failing:
                raise RuntimeError("database is down")

        async def get_position_by_address(self, address):
            calls.append(("get_position_by_address", address))
            return position

        async def create_position(self, position_data):
            calls.append(("create_position", position_data))
            return SimpleNamespace(id=7)

        async def add_to_position_amounts(self, **kwargs):
            calls.append(("add_to_position_amounts", kwargs))
            return position

        async def subtract_from_position_amounts(self, **kwargs):
            calls.append(("subtract_from_position_amounts", kwargs))
            return position

        async def close_position(self, address, **kwargs):
            calls.append(("close_position", {"position_address": address, **kwargs}))

        async def create_event(self, event_data):
            calls.append(("create_event", event_data))
            return SimpleNamespace(id=11)

        async def search_events(self, **kwargs):
            calls.append(("search_events", kwargs))
            return list(events)

        async def search_positions(self, **kwargs):
            calls.append(("search_positions", kwargs))
            return list(positions)

        @staticmethod
        def event_to_dict(event):
            return {"id": event.id}

        @staticmethod
        def position_to_dict(position):
            return {"position_address": position.position_address}

    return _Repo


def _db_manager():
    manager = SimpleNamespace()

    @asynccontextmanager
    async def session_context():
        yield object()

    manager.get_session_context = session_context
    return manager


def _service(calls, **repo_kwargs):
    service = GatewayAMMService(db_manager=_db_manager())
    service.repository_class = _repo_class(calls, **repo_kwargs)
    return service


def _accounts_service(**gateway_client_methods):
    gateway_client = SimpleNamespace(
        ping=AsyncMock(return_value=True),
        parse_network_id=lambda network_id: tuple(network_id.split("-", 1)),
        get_wallet_address_or_default=AsyncMock(return_value=WALLET),
        resolve_token_symbol=AsyncMock(side_effect=lambda chain, network, address: SYMBOLS[address]),
        **{name: AsyncMock(return_value=value) for name, value in gateway_client_methods.items()},
    )
    return SimpleNamespace(gateway_client=gateway_client)


def _client(accounts_service, amm_service=None):
    app = FastAPI()
    app.include_router(gateway_amm.router)
    app.dependency_overrides[get_accounts_service] = lambda: accounts_service
    app.dependency_overrides[get_gateway_amm_service] = lambda: amm_service
    return TestClient(app, raise_server_exceptions=False)


def _stored_position(**overrides):
    """A position row as the repository hands it back."""
    return SimpleNamespace(**{
        "position_address": POSITION,
        "pool_address": POOL,
        "wallet_address": WALLET,
        "status": "OPEN",
        "closed_at": None,
        **overrides,
    })


# ---------------------------------------------------------------------------
# AMM add: a DAMM v2 position row and its ADD_LIQUIDITY event
# ---------------------------------------------------------------------------

ADD_BODY = {
    "connector": "meteora",
    "network": "solana-mainnet-beta",
    "pool_address": POOL,
    "base_token_amount": 0.01,
    "quote_token_amount": 2,
}

ADD_RESULT = {
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


def test_add_liquidity_writes_the_same_position_row():
    calls = RepoCalls()
    accounts_service = _accounts_service(amm_pool_info=POOL_INFO, amm_add_liquidity=ADD_RESULT)
    client = _client(accounts_service, amm_service=_service(calls))

    response = client.post("/gateway/amm/add-liquidity", json=ADD_BODY)
    assert response.status_code == 200

    assert calls.payload("create_position") == {
        # Gateway names the position its transaction created; nothing else can.
        "position_address": POSITION,
        "pool_address": POOL,
        "connector": "meteora",
        "network": "solana-mainnet-beta",
        "wallet_address": WALLET,
        "base_token": "SOL",
        "quote_token": "USDC",
        "trading_pair": "SOL-USDC",
        # The on-chain amounts, never the requested 0.01 / 2.
        "initial_base_token_amount": 0.0099,
        "initial_quote_token_amount": 1.98,
        "base_token_amount": 0.0099,
        "quote_token_amount": 1.98,
        # Locked, not spent — kept as a Decimal so the close can be checked against it.
        "position_rent": Decimal("0.05788"),
        "entry_price": 200.0,
        "current_price": 200.0,
    }


def test_add_liquidity_writes_the_same_event_row():
    calls = RepoCalls()
    accounts_service = _accounts_service(amm_pool_info=POOL_INFO, amm_add_liquidity=ADD_RESULT)
    client = _client(accounts_service, amm_service=_service(calls))

    client.post("/gateway/amm/add-liquidity", json=ADD_BODY)

    assert calls.payload("create_event") == {
        "transaction_hash": SIGNATURE,
        "connector": "meteora",
        "network": "solana-mainnet-beta",
        "wallet_address": WALLET,
        "pool_address": POOL,
        "position_address": POSITION,
        "event_type": "ADD_LIQUIDITY",
        "base_token_amount": 0.0099,
        "quote_token_amount": 1.98,
        "price": 200.0,
        "gas_fee": 0.000011772,
        "gas_token": "SOL",
        "status": "CONFIRMED",
    }


def test_adding_to_a_tracked_position_tops_it_up_and_reopens_it():
    calls = RepoCalls()
    closed = _stored_position(status="CLOSED", closed_at="2026-08-01T00:00:00")
    accounts_service = _accounts_service(amm_pool_info=POOL_INFO, amm_add_liquidity=ADD_RESULT)
    client = _client(accounts_service, amm_service=_service(calls, position=closed))

    response = client.post("/gateway/amm/add-liquidity", json={**ADD_BODY, "position_address": POSITION})
    assert response.status_code == 200

    assert calls.payload("add_to_position_amounts") == {
        "position_address": POSITION,
        "base_delta": Decimal("0.0099"),
        "quote_delta": Decimal("1.98"),
        "entry_price": Decimal("200.0"),
    }
    # Capital went back into a position that had been emptied: it is open again.
    assert (closed.status, closed.closed_at) == ("OPEN", None)
    assert "create_position" not in calls.names()


def test_a_fungible_lp_add_records_the_event_and_no_position():
    # Raydium CPMM has no position identity — the event log is the entire record.
    calls = RepoCalls()
    accounts_service = _accounts_service(amm_pool_info=POOL_INFO, amm_add_liquidity=ADD_RESULT)
    client = _client(accounts_service, amm_service=_service(calls))

    response = client.post("/gateway/amm/add-liquidity", json={**ADD_BODY, "connector": "raydium"})
    assert response.status_code == 200

    assert calls.names() == ["create_event"]
    assert calls.payload("create_event")["position_address"] is None


def test_a_submitted_add_books_nothing_and_records_null_amounts():
    calls = RepoCalls()
    accounts_service = _accounts_service(
        amm_pool_info=POOL_INFO,
        amm_add_liquidity={"signature": SIGNATURE, "status": 0},
    )
    client = _client(accounts_service, amm_service=_service(calls))

    response = client.post("/gateway/amm/add-liquidity", json=ADD_BODY)
    assert response.status_code == 200

    assert calls.names() == ["create_event"]
    event = calls.payload("create_event")
    assert event["status"] == "SUBMITTED"
    # Not yet confirmed: no amounts are invented, and no gas token without a fee.
    assert (event["base_token_amount"], event["quote_token_amount"]) == (None, None)
    assert (event["gas_fee"], event["gas_token"]) == (None, None)


def test_add_liquidity_answers_the_caller_even_when_the_write_fails():
    # The liquidity is in the pool; a bookkeeping failure must not be reported as a
    # failed add. That policy now lives in one place, so this is what pins it.
    accounts_service = _accounts_service(amm_pool_info=POOL_INFO, amm_add_liquidity=ADD_RESULT)
    client = _client(accounts_service, amm_service=_service(RepoCalls(), failing=True))

    response = client.post("/gateway/amm/add-liquidity", json=ADD_BODY)

    assert response.status_code == 200
    assert response.json()["signature"] == SIGNATURE
    assert response.json()["status"] == "CONFIRMED"


def test_an_unreadable_pool_costs_the_price_not_the_write():
    calls = RepoCalls()
    accounts_service = _accounts_service(amm_add_liquidity=ADD_RESULT)
    accounts_service.gateway_client.amm_pool_info = AsyncMock(side_effect=RuntimeError("gateway down"))
    client = _client(accounts_service, amm_service=_service(calls))

    response = client.post("/gateway/amm/add-liquidity", json={**ADD_BODY, "connector": "raydium"})

    assert response.status_code == 200
    assert calls.payload("create_event")["price"] is None


# ---------------------------------------------------------------------------
# AMM remove: unbooking the capital, and the close a 100% remove is
# ---------------------------------------------------------------------------

REMOVE_BODY = {
    "connector": "meteora",
    "network": "solana-mainnet-beta",
    "pool_address": POOL,
    "position_address": POSITION,
    "percentage_to_remove": 100,
}

REMOVE_RESULT = {
    "signature": SIGNATURE,
    "status": 1,
    "data": {
        "baseTokenAmountRemoved": 0.0099,
        "quoteTokenAmountRemoved": 1.98,
        "positionRentRefunded": 0.05788,
        "fee": 0.000005,
    },
}


def test_a_full_remove_unbooks_the_capital_and_closes_the_row():
    calls = RepoCalls()
    accounts_service = _accounts_service(amm_pool_info=POOL_INFO, amm_remove_liquidity=REMOVE_RESULT)
    client = _client(accounts_service, amm_service=_service(calls, position=_stored_position()))

    response = client.post("/gateway/amm/remove-liquidity", json=REMOVE_BODY)
    assert response.status_code == 200

    assert calls.payload("subtract_from_position_amounts") == {
        "position_address": POSITION,
        "base_delta": Decimal("0.0099"),
        "quote_delta": Decimal("1.98"),
    }
    # Gateway closes the position account in the same transaction, which is what
    # returns the rent — so the refund is recorded against the close.
    assert calls.payload("close_position") == {
        "position_address": POSITION,
        "position_rent_refunded": Decimal("0.05788"),
    }
    assert calls.payload("create_event")["event_type"] == "REMOVE_LIQUIDITY"


def test_a_partial_remove_leaves_the_position_open():
    calls = RepoCalls()
    accounts_service = _accounts_service(
        amm_pool_info=POOL_INFO,
        amm_remove_liquidity={**REMOVE_RESULT, "data": {"baseTokenAmountRemoved": 0.005,
                                                        "quoteTokenAmountRemoved": 1.0}},
    )
    client = _client(accounts_service, amm_service=_service(calls, position=_stored_position()))

    response = client.post("/gateway/amm/remove-liquidity",
                           json={**REMOVE_BODY, "percentage_to_remove": 50})
    assert response.status_code == 200

    assert calls.payload("subtract_from_position_amounts")["base_delta"] == Decimal("0.005")
    # The account stays open and refunds nothing.
    assert "close_position" not in calls.names()


def test_a_remove_that_only_submitted_unbooks_nothing():
    calls = RepoCalls()
    accounts_service = _accounts_service(
        amm_pool_info=POOL_INFO,
        amm_remove_liquidity={"signature": SIGNATURE, "status": 0},
    )
    client = _client(accounts_service, amm_service=_service(calls, position=_stored_position()))

    response = client.post("/gateway/amm/remove-liquidity", json=REMOVE_BODY)

    assert response.status_code == 200
    assert calls.names() == ["create_event"]


# ---------------------------------------------------------------------------
# Create-pool: the event files against the pool the response names
# ---------------------------------------------------------------------------

def test_create_pool_records_its_event_against_the_pool_it_created():
    calls = RepoCalls()
    accounts_service = _accounts_service(amm_create_pool={
        "signature": SIGNATURE,
        "status": 1,
        "poolAddress": POOL,
        "price": 200.0,
        "data": {"baseTokenAmountAdded": 0.0099, "quoteTokenAmountAdded": 1.98, "fee": 0.0002},
    })
    client = _client(accounts_service, amm_service=_service(calls))

    response = client.post("/gateway/amm/create-pool", json={
        "connector": "meteora",
        "network": "solana-mainnet-beta",
        "base_token": "SOL",
        "quote_token": "USDC",
        "base_token_amount": 0.01,
        "quote_token_amount": 2,
        "extra_params": {"configAddress": POOL},
    })
    assert response.status_code == 200

    assert calls.payload("create_event") == {
        "transaction_hash": SIGNATURE,
        "connector": "meteora",
        "network": "solana-mainnet-beta",
        "wallet_address": WALLET,
        # Read from the response: the request names tokens, not a pool that did not exist.
        "pool_address": POOL,
        "position_address": None,
        "event_type": "CREATE_POOL",
        "base_token_amount": 0.0099,
        "quote_token_amount": 1.98,
        "price": 200.0,
        "gas_fee": 0.0002,
        "gas_token": "SOL",
        "status": "CONFIRMED",
    }


# ---------------------------------------------------------------------------
# The failed-write path keeps its single transaction-id parser
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_reverted_write_is_filed_with_the_gas_it_burned():
    from services.gateway_client import GatewayError

    calls = RepoCalls()
    error = GatewayError(f"Transaction {SIGNATURE} landed on-chain but failed: 0x1771", status=500)

    await gateway_amm._record_failed_event(
        _service(calls), error, event_type="ADD_LIQUIDITY", connector="meteora",
        network="solana-mainnet-beta", wallet_address=WALLET, pool_address=POOL,
        position_address=POSITION,
    )

    assert calls.payload("create_event") == {
        "transaction_hash": SIGNATURE,
        "connector": "meteora",
        "network": "solana-mainnet-beta",
        "wallet_address": WALLET,
        "pool_address": POOL,
        "position_address": POSITION,
        "event_type": "ADD_LIQUIDITY",
        "status": "FAILED",
        "error_message": str(error),
    }


@pytest.mark.asyncio
async def test_a_failure_that_never_reached_the_chain_writes_nothing():
    from services.gateway_client import GatewayError

    calls = RepoCalls()

    await gateway_amm._record_failed_event(
        _service(calls),
        GatewayError("Simulation failed: insufficient funds", status=400),
        event_type="ADD_LIQUIDITY", connector="meteora", network="solana-mainnet-beta",
        wallet_address=WALLET, pool_address=POOL,
    )

    assert calls.names() == []


# ---------------------------------------------------------------------------
# The read envelopes the handlers used to hand-build
# ---------------------------------------------------------------------------

def test_event_search_answers_the_same_envelope():
    calls = RepoCalls()
    events = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
    client = _client(_accounts_service(), amm_service=_service(calls, events=events))

    response = client.post("/gateway/amm/events/search?limit=2000&offset=10&event_type=CREATE_POOL")

    assert response.status_code == 200
    assert response.json() == {
        "data": [{"id": 1}, {"id": 2}],
        "total_count": 2,
        # The query is clamped to the 1000 ceiling; the envelope echoes what was asked.
        "limit": 2000,
        "offset": 10,
    }
    assert calls.payload("search_events") == {
        "connector": None,
        "network": None,
        "wallet_address": None,
        "pool_address": None,
        "event_type": "CREATE_POOL",
        "status": None,
        "limit": 1000,
        "offset": 10,
    }


def test_position_search_answers_the_same_envelope():
    calls = RepoCalls()
    positions = [_stored_position(position_address=f"POS-{i}") for i in range(2)]
    client = _client(_accounts_service(), amm_service=_service(calls, positions=positions))

    response = client.post("/gateway/amm/positions/search?wallet_address=" + WALLET)

    assert response.status_code == 200
    assert response.json() == {
        "data": [{"position_address": "POS-0"}, {"position_address": "POS-1"}],
        "total_count": 2,
        "limit": 50,
        "offset": 0,
    }
    assert calls.payload("search_positions")["wallet_address"] == WALLET


def test_an_unreachable_database_is_a_500_not_an_empty_page():
    client = _client(_accounts_service(), amm_service=_service(RepoCalls(), failing=True))

    response = client.post("/gateway/amm/events/search")

    assert response.status_code == 500
    assert response.json()["detail"].startswith("Error searching AMM events")


# ---------------------------------------------------------------------------
# Archived bots: the bot-run cleanup its delete route reports
# ---------------------------------------------------------------------------

def _archived_bots_client(orchestrator):
    app = FastAPI()
    app.include_router(archived_bots.router)
    app.dependency_overrides[get_bots_orchestrator] = lambda: orchestrator
    return TestClient(app, raise_server_exceptions=False)


def test_deleting_an_archived_bot_reports_the_bot_runs_it_cleaned(monkeypatch):
    monkeypatch.setattr(archived_bots.fs_util, "delete_archived_bot", lambda db_path: "hummingbot-1")
    orchestrator = SimpleNamespace(delete_bot_runs_for_bot=AsyncMock(return_value=3))

    response = _archived_bots_client(orchestrator).delete("/archived-bots/hummingbot-1/data/bot.sqlite")

    assert response.status_code == 200
    assert response.json() == {
        "message": "Archived bot 'hummingbot-1' deleted successfully",
        "bot_name": "hummingbot-1",
        "bot_runs_deleted": 3,
    }
    orchestrator.delete_bot_runs_for_bot.assert_awaited_once_with("hummingbot-1")


@pytest.mark.asyncio
async def test_a_bot_run_cleanup_failure_never_fails_the_deletion(monkeypatch):
    """The files are already gone; a database problem must not report otherwise."""
    from services import bots_orchestrator as orchestrator_module

    class _ExplodingRepo:
        def __init__(self, session):
            raise RuntimeError("database is down")

    monkeypatch.setattr(orchestrator_module, "BotRunRepository", _ExplodingRepo)
    orchestrator = orchestrator_module.BotsOrchestrator.__new__(orchestrator_module.BotsOrchestrator)
    orchestrator.db_manager = _db_manager()

    assert await orchestrator.delete_bot_runs_for_bot("hummingbot-1") == 0


@pytest.mark.asyncio
async def test_the_cleanup_returns_what_the_repository_deleted(monkeypatch):
    from services import bots_orchestrator as orchestrator_module

    deleted = []

    class _StubRepo:
        def __init__(self, session):
            pass

        async def delete_bot_runs_by_bot_name(self, bot_name):
            deleted.append(bot_name)
            return 2

    monkeypatch.setattr(orchestrator_module, "BotRunRepository", _StubRepo)
    orchestrator = orchestrator_module.BotsOrchestrator.__new__(orchestrator_module.BotsOrchestrator)
    orchestrator.db_manager = _db_manager()

    assert await orchestrator.delete_bot_runs_for_bot("hummingbot-1") == 2
    assert deleted == ["hummingbot-1"]
