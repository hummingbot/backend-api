"""Gateway's write-response wire format is parsed in one place, not copied per handler.

Gateway answers a write with the transaction id under one of three keys (`signature` on
Solana, `txHash` on EVM, `hash` on some older shapes) and says nothing at all about which
token paid for the gas. Both facts used to be re-derived in every handler that recorded an
event, and the copies drifted:

- the chain -> gas-token mapping existed three times — the 9-entry dict, a 6-entry one in
  the poller, and a two-branch ternary (`"SOL" if chain == "solana" else "ETH" if chain ==
  "ethereum" else None`) in the add/remove handlers. On base/arbitrum/polygon that ternary
  wrote gas_token NULL, and a row inserted CONFIRMED is never re-polled, so the NULL was
  permanent. Add/remove liquidity gas costs were unusable off solana/ethereum;
- the tx-id extraction existed six times, and the CLMM open handler's copy read
  `signature` alone — answering every EVM open with "no transaction signature returned"
  for a position that had just been opened.

These tests pin both invariants: one definition of each parser, and the same gas token
persisted by every CLMM event writer for the same chain.
"""
import ast
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.gateway_extras import get_transaction_hash_from_response
from services.gateway_client import get_native_gas_token

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Every module that writes a gas_token column or parses a Gateway write response.
GAS_TOKEN_WRITERS = [
    REPO_ROOT / "routers" / "gateway_clmm.py",
    REPO_ROOT / "routers" / "gateway_amm.py",
    REPO_ROOT / "routers" / "gateway_swap.py",
    REPO_ROOT / "services" / "gateway_transaction_poller.py",
    REPO_ROOT / "services" / "executor_service.py",
]


# ============================================
# One definition of each parser
# ============================================

def _definitions_of(name: str):
    found = []
    for directory in ("routers", "services"):
        for path in sorted((REPO_ROOT / directory).rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                    found.append(path.relative_to(REPO_ROOT).as_posix())
    return found


@pytest.mark.parametrize("parser, home", [
    ("get_transaction_status_from_response", "routers/gateway_extras.py"),
    ("get_transaction_hash_from_response", "routers/gateway_extras.py"),
    ("get_native_gas_token", "services/gateway_client.py"),
])
def test_each_response_parser_is_defined_exactly_once(parser, home):
    assert _definitions_of(parser) == [home]


def test_only_gateway_client_holds_a_chain_to_gas_token_map():
    """A second dict keyed by chain name is how the poller's 6-entry copy came to answer
    "UNKNOWN" for base/bsc/cronos while the routers answered correctly."""
    offenders = []
    for directory in ("routers", "services"):
        for path in sorted((REPO_ROOT / directory).rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Dict):
                    continue
                keys = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                if {"solana", "ethereum"} <= keys:
                    offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert offenders == ["services/gateway_client.py"]


def _gas_token_values(tree):
    """Every expression assigned to a gas_token variable or dict key."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "gas_token":
                    yield node.value
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "gas_token":
                    yield value


@pytest.mark.parametrize("path", GAS_TOKEN_WRITERS, ids=lambda p: p.name)
def test_every_gas_token_write_resolves_through_the_one_helper(path):
    """No literal and no chain ternary: the value is either the helper's call or a name
    bound from it. The old `"SOL" if chain == "solana" else ... else None` fails here."""
    tree = ast.parse(path.read_text())
    for value in _gas_token_values(tree):
        names = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
        calls_helper = "get_native_gas_token" in names
        # A bare `gas_token` / `status_result.get("gas_token")` reference is a name bound
        # from the helper earlier in the same function (asserted for that binding above).
        reads_a_binding = isinstance(value, (ast.Name, ast.Call, ast.Attribute)) and not isinstance(
            value, ast.Constant)
        assert calls_helper or reads_a_binding, (
            f"{path.name}:{value.lineno} writes gas_token from a literal or a chain "
            f"ternary instead of get_native_gas_token()"
        )


# ============================================
# The gas-token map itself
# ============================================

@pytest.mark.parametrize("chain, token", [
    ("solana", "SOL"),
    ("ethereum", "ETH"),
    ("polygon", "MATIC"),
    ("avalanche", "AVAX"),
    ("optimism", "ETH"),
    ("arbitrum", "ETH"),
    ("base", "ETH"),
    ("bsc", "BNB"),
    ("cronos", "CRO"),
])
def test_the_gas_token_map_covers_every_supported_chain(chain, token):
    assert get_native_gas_token(chain) == token
    # Gateway is not consistent about casing in network ids.
    assert get_native_gas_token(chain.upper()) == token


def test_an_unknown_chain_is_named_not_nulled():
    """"UNKNOWN" over None deliberately: a NULL column cannot be told apart from a row
    written before gas was recorded at all, so the backfill can't find it."""
    assert get_native_gas_token("hyperliquid") == "UNKNOWN"
    assert get_native_gas_token("") == "UNKNOWN"


# ============================================
# Transaction-id extraction
# ============================================

@pytest.mark.parametrize("response, expected", [
    ({"signature": "5Uq9x"}, "5Uq9x"),                      # Solana
    ({"txHash": "0xabc"}, "0xabc"),                         # EVM — the shape open used to drop
    ({"hash": "0xdef"}, "0xdef"),                           # older Gateway shapes
    ({"signature": "5Uq9x", "txHash": "0xabc"}, "5Uq9x"),   # signature wins
    ({"status": 1}, None),                                  # nothing to report
    ({"signature": None, "txHash": "0xabc"}, "0xabc"),      # null signature falls through
])
def test_the_transaction_id_is_read_from_whichever_key_gateway_used(response, expected):
    assert get_transaction_hash_from_response(response) == expected


# ============================================
# ADD/REMOVE persist the same gas token as COLLECT_FEES — the data bug
# ============================================

class _FakeRepo:
    """Captures create_event payloads; every other repository call is a no-op await."""

    events = []

    def __init__(self, session):
        pass

    async def get_position_by_address(self, position_address):
        return SimpleNamespace(
            id=1,
            position_address=position_address,
            pool_address="0xPOOL",
            wallet_address="0xWALLET",
        )

    async def create_event(self, event_data):
        _FakeRepo.events.append(event_data)

    def __getattr__(self, name):
        return AsyncMock()


class _FakeDbManager:
    def get_session_context(self):
        class _Ctx:
            async def __aenter__(self):
                return SimpleNamespace()

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


# A base-mainnet (EVM) write: no `signature`, gas paid in ETH. Under the old code this
# recorded gas_token NULL for add/remove and "ETH" for collect-fees — same chain, same
# wallet, same block.
def _evm_write(**data):
    return {"txHash": "0xdeadbeef", "status": 1, "data": {"fee": 0.00042, **data}}


CLMM_WRITES = [
    (
        "/gateway/clmm/add",
        {"connector": "uniswap", "network": "base-mainnet", "position_address": "0xPOS",
         "base_token_amount": 0.5, "quote_token_amount": 50},
        "clmm_add_liquidity",
        _evm_write(baseTokenAmountAdded=0.5, quoteTokenAmountAdded=50),
        "ADD_LIQUIDITY",
    ),
    (
        "/gateway/clmm/remove",
        {"connector": "uniswap", "network": "base-mainnet", "position_address": "0xPOS",
         "percentage_to_remove": 50},
        "clmm_remove_liquidity",
        _evm_write(baseTokenAmountRemoved=0.25, quoteTokenAmountRemoved=25),
        "REMOVE_LIQUIDITY",
    ),
    (
        "/gateway/clmm/collect-fees",
        {"connector": "uniswap", "network": "base-mainnet", "position_address": "0xPOS"},
        "clmm_collect_fees",
        _evm_write(baseFeeAmountCollected=0.01, quoteFeeAmountCollected=1),
        "COLLECT_FEES",
    ),
]


def _record_clmm_write(monkeypatch, route, body, gateway_method, gateway_response):
    from deps import get_accounts_service, get_database_manager
    from routers import gateway_clmm

    monkeypatch.setattr(gateway_clmm, "GatewayCLMMRepository", _FakeRepo)
    _FakeRepo.events = []

    gateway_client = SimpleNamespace(
        ping=AsyncMock(return_value=True),
        parse_network_id=lambda network_id: tuple(network_id.split("-", 1)),
        get_wallet_address_or_default=AsyncMock(return_value="0xWALLET"),
        clmm_positions_owned=AsyncMock(return_value=[]),
        clmm_pool_info=AsyncMock(return_value={"price": 2500.0}),
        **{gateway_method: AsyncMock(return_value=gateway_response)},
    )

    app = FastAPI()
    app.include_router(gateway_clmm.router)
    app.dependency_overrides[get_accounts_service] = lambda: SimpleNamespace(gateway_client=gateway_client)
    app.dependency_overrides[get_database_manager] = lambda: _FakeDbManager()

    response = TestClient(app, raise_server_exceptions=False).post(route, json=body)
    assert response.status_code == 200, response.text
    return _FakeRepo.events


@pytest.mark.parametrize("route, body, gateway_method, gateway_response, event_type", CLMM_WRITES)
def test_every_clmm_event_records_the_chains_gas_token(
    monkeypatch, route, body, gateway_method, gateway_response, event_type
):
    events = _record_clmm_write(monkeypatch, route, body, gateway_method, gateway_response)

    recorded = [e for e in events if e["event_type"] == event_type]
    assert recorded, f"{event_type} wrote no event"
    for event in recorded:
        assert event["gas_token"] == "ETH", (
            f"{event_type} on base recorded gas_token {event['gas_token']!r}; a CONFIRMED "
            "row is never re-polled, so a wrong value here is permanent"
        )
        assert event["gas_fee"] == pytest.approx(0.00042)
        # The EVM id came from `txHash`; reading `signature` alone got None here.
        assert event["transaction_hash"] == "0xdeadbeef"


def test_add_remove_and_collect_fees_agree_on_the_gas_token(monkeypatch):
    """The parity the ternary broke: three writers, one chain, one answer."""
    tokens = {}
    for route, body, gateway_method, gateway_response, event_type in CLMM_WRITES:
        events = _record_clmm_write(monkeypatch, route, body, gateway_method, gateway_response)
        tokens[event_type] = next(e["gas_token"] for e in events if e["event_type"] == event_type)

    assert set(tokens.values()) == {get_native_gas_token("base")}, tokens
    assert None not in tokens.values()


def test_an_evm_open_is_not_rejected_for_having_no_solana_signature(monkeypatch):
    """The open handler read `signature` alone, so a uniswap open that Gateway confirmed
    came back as a 500 saying no signature was returned — for a position that existed."""
    from deps import get_accounts_service, get_database_manager
    from routers import gateway_clmm

    monkeypatch.setattr(gateway_clmm, "GatewayCLMMRepository", _FakeRepo)
    _FakeRepo.events = []

    gateway_client = SimpleNamespace(
        ping=AsyncMock(return_value=True),
        parse_network_id=lambda network_id: tuple(network_id.split("-", 1)),
        get_wallet_address_or_default=AsyncMock(return_value="0xWALLET"),
        clmm_pool_info=AsyncMock(return_value={"price": 2500.0}),
        clmm_open_position=AsyncMock(return_value={
            "txHash": "0xopened",
            "status": 1,
            "data": {"positionAddress": "0xNEWPOS", "fee": 0.00042},
        }),
    )

    app = FastAPI()
    app.include_router(gateway_clmm.router)
    app.dependency_overrides[get_accounts_service] = lambda: SimpleNamespace(gateway_client=gateway_client)
    app.dependency_overrides[get_database_manager] = lambda: _FakeDbManager()

    response = TestClient(app, raise_server_exceptions=False).post("/gateway/clmm/open", json={
        "connector": "uniswap",
        "network": "base-mainnet",
        "pool_address": "0xPOOL",
        "lower_price": 2000,
        "upper_price": 3000,
        "base_token_amount": 0.5,
    })

    assert response.status_code == 200, response.text
    assert response.json()["transaction_hash"] == "0xopened"


def test_the_swap_router_reads_the_evm_transaction_id_too():
    """gateway_swap's copy of the extraction is gone; the shared helper is what runs."""
    source = (REPO_ROOT / "routers" / "gateway_swap.py").read_text()
    assert 'result.get("signature")' not in source
    assert "get_transaction_hash_from_response(result)" in source
