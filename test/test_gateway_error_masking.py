"""
Regression tests for Gateway error-dict masking.

GatewayClient._request returns {"error": <str>, "status": <int>} on non-OK HTTP.
These tests pin the three places where treating that shape as data caused real
damage (found in the 2026-07-13 audit):
- /gateway/swap/quote rendered a Gateway 404 as a 200 quote with price "0",
- refresh_position_data marked positions CLOSED in the DB on any Gateway error,
- the transaction poller recorded transient Gateway errors as on-chain FAILED.

Run with: pytest test/test_gateway_error_masking.py -v
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ERROR_DICT = {"error": "Route GET:/nope not found", "status": 404}


def _mock_accounts_service(**gateway_client_methods):
    gateway_client = SimpleNamespace(
        ping=AsyncMock(return_value=True),
        parse_network_id=lambda network_id: tuple(network_id.split("-", 1)),
        **{name: AsyncMock(return_value=value) for name, value in gateway_client_methods.items()},
    )
    return SimpleNamespace(gateway_client=gateway_client)


# ============================================
# /gateway/swap/quote must not mask errors as price "0"
# ============================================

@pytest.fixture
def swap_app():
    from deps import get_accounts_service
    from routers import gateway_swap

    app = FastAPI()
    app.include_router(gateway_swap.router)

    def with_service(service):
        app.dependency_overrides[get_accounts_service] = lambda: service
        return TestClient(app, raise_server_exceptions=False)

    return with_service


def _quote_body(connector="jupiter"):
    return {
        "connector": connector,
        "network": "solana-mainnet-beta",
        "trading_pair": "SOL-USDC",
        "side": "BUY",
        "amount": 0.1,
    }


def test_swap_quote_propagates_gateway_error(swap_app):
    client = swap_app(_mock_accounts_service(quote_swap=ERROR_DICT))
    response = client.post("/gateway/swap/quote", json=_quote_body())
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_swap_quote_happy_path_returns_real_price(swap_app):
    quote = {"price": 75.2, "amountIn": 7.52, "amountOut": 0.1}
    client = swap_app(_mock_accounts_service(quote_swap=quote))
    response = client.post("/gateway/swap/quote", json=_quote_body("meteora/clmm"))
    assert response.status_code == 200
    assert response.json()["price"] == "75.2"


def test_swap_quote_gateway_unreachable_is_503(swap_app):
    client = swap_app(_mock_accounts_service(quote_swap=None))
    response = client.post("/gateway/swap/quote", json=_quote_body())
    assert response.status_code == 503


# ============================================
# refresh_position_data must not close positions on Gateway errors
# ============================================

def _position():
    return SimpleNamespace(
        connector="meteora",
        network="solana-mainnet-beta",
        wallet_address="WALLET",
        pool_address="POOL",
        position_address="POS",
    )


@pytest.mark.asyncio
async def test_refresh_does_not_close_position_on_gateway_error():
    from services.gateway_clmm_service import refresh_position_data

    accounts_service = _mock_accounts_service(clmm_positions_owned=ERROR_DICT)
    clmm_repo = SimpleNamespace(
        close_position=AsyncMock(),
        update_position_liquidity=AsyncMock(),
        update_position_fees=AsyncMock(),
    )

    await refresh_position_data(_position(), accounts_service.gateway_client, clmm_repo)
    clmm_repo.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_skips_position_missing_from_valid_list():
    """Absence from ONE positions-owned read is not proof of closure (RPC lag):
    the refresh skips the update and leaves close detection to the poller's
    consecutive-miss gate — a single read must never close a live position."""
    from services.gateway_clmm_service import refresh_position_data

    accounts_service = _mock_accounts_service(clmm_positions_owned=[{"address": "OTHER"}])
    clmm_repo = SimpleNamespace(
        close_position=AsyncMock(),
        update_position_liquidity=AsyncMock(),
        update_position_fees=AsyncMock(),
    )

    await refresh_position_data(_position(), accounts_service.gateway_client, clmm_repo)
    clmm_repo.close_position.assert_not_awaited()
    clmm_repo.update_position_liquidity.assert_not_awaited()


# ============================================
# Transaction poller must treat Gateway errors as transient, not FAILED
# ============================================

def _poller_with_result(result):
    from services.gateway_transaction_poller import GatewayTransactionPoller

    poller = object.__new__(GatewayTransactionPoller)
    poller.gateway_client = SimpleNamespace(
        ping=AsyncMock(return_value=True),
        poll_transaction=AsyncMock(return_value=result),
    )
    return poller


@pytest.mark.asyncio
async def test_poller_treats_gateway_error_as_transient():
    poller = _poller_with_result({"error": "Gateway 500", "status": 500})
    result = await poller._check_transaction_status("solana", "mainnet-beta", "TX")
    assert result is None  # retried later — NOT recorded as FAILED


@pytest.mark.asyncio
async def test_poller_still_reports_real_onchain_failure():
    poller = _poller_with_result({
        "txStatus": -1,
        "error": "SLIPPAGE_EXCEEDED (0x1771): slippage tolerance exceeded",
        "fee": 0.00001,
    })
    result = await poller._check_transaction_status("solana", "mainnet-beta", "TX")
    assert result["status"] == "FAILED"
    assert "SLIPPAGE_EXCEEDED" in result["error_message"]


@pytest.mark.asyncio
async def test_poller_confirms_transaction():
    poller = _poller_with_result({"txStatus": 1, "fee": 0.00001, "error": None})
    result = await poller._check_transaction_status("solana", "mainnet-beta", "TX")
    assert result["status"] == "CONFIRMED"
    # fee of exactly 0 must survive (no truthiness drop)
    poller = _poller_with_result({"txStatus": 1, "fee": 0, "error": None})
    result = await poller._check_transaction_status("solana", "mainnet-beta", "TX")
    assert result["gas_fee"] == 0


@pytest.mark.asyncio
async def test_poller_pending_with_transient_error_stays_pending():
    """Gateway deliberately returns txStatus 0 WITH an error message for transient
    poll failures ("poll again, don't give up") — the error field must never
    promote a pending transaction to FAILED."""
    poller = _poller_with_result({
        "txStatus": 0,
        "error": "Error polling transaction: 429 Too Many Requests",
    })
    result = await poller._check_transaction_status("solana", "mainnet-beta", "TX")
    assert result["status"] == "PENDING"


@pytest.mark.asyncio
async def test_poller_reports_not_found_as_dropped():
    """txStatus -2 is NOT_FOUND — terminal on Solana after blockhash expiry; the
    caller applies the grace window, but the classifier must not call it pending."""
    poller = _poller_with_result({"txStatus": -2, "error": None})
    result = await poller._check_transaction_status("solana", "mainnet-beta", "TX")
    assert result["status"] == "DROPPED"
