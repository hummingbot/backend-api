"""
Regression tests for the Gateway availability guard's ping cache (PERF-114).

``deps.require_gateway_online`` guards ~39 routes and runs ahead of every one of them, so an
uncached ``GatewayClient.ping()`` charges each guarded request a full HTTP round-trip to Gateway.
These pin the cache that removes that cost, and — just as importantly — the ways it must NOT
lie:
- a burst of guarded requests inside the TTL costs one ping, not one per request,
- the verdict expires: a Gateway that goes down is reported unavailable within the TTL,
- any Gateway call that fails to connect drops the cached verdict immediately, so an outage
  mid-TTL is never masked by a stale "available",
- the resulting 503 still carries the single-sourced detail, now with ``Retry-After``.

Run with: pytest test/test_gateway_ping_cache.py -v
"""
import math
from types import SimpleNamespace

import aiohttp
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import services.gateway_client as gateway_client_module
from deps import GATEWAY_UNAVAILABLE_DETAIL, get_accounts_service, require_gateway_online
from services.gateway_client import GatewayClient

TTL = GatewayClient.PING_CACHE_TTL_SECONDS


class FakeClock:
    """Stand-in for ``time.monotonic`` so TTL expiry is exercised without sleeping."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(gateway_client_module, "time", fake)
    return fake


@pytest.fixture
def client_and_pings(monkeypatch):
    """A GatewayClient whose root request is counted instead of hitting the network."""
    client = GatewayClient()
    pings = []
    responses = {"status": "ok"}

    async def fake_request(method, path, params=None, json=None):
        pings.append((method, path))
        return responses

    monkeypatch.setattr(client, "_request", fake_request)
    return client, pings, responses


# ==================================
# The cache hit path (the whole point)
# ==================================


@pytest.mark.asyncio
async def test_repeated_pings_inside_the_ttl_cost_one_round_trip(client_and_pings, clock):
    client, pings, _ = client_and_pings

    results = [await client.ping() for _ in range(10)]

    assert results == [True] * 10
    assert pings == [("GET", "")], "the guard must reuse the cached verdict, not re-ping per request"


@pytest.mark.asyncio
async def test_a_false_verdict_is_cached_too(client_and_pings, clock):
    client, pings, responses = client_and_pings
    responses["status"] = "down"

    assert await client.ping() is False
    assert await client.ping() is False
    assert len(pings) == 1


# ==============================================
# The expiry path (a stale verdict must not stick)
# ==============================================


@pytest.mark.asyncio
async def test_the_verdict_expires_after_the_ttl(client_and_pings, clock):
    client, pings, _ = client_and_pings

    assert await client.ping() is True
    clock.advance(TTL + 0.01)
    assert await client.ping() is True

    assert len(pings) == 2, "the verdict must not outlive PING_CACHE_TTL_SECONDS"


@pytest.mark.asyncio
async def test_a_gateway_that_goes_down_is_reported_within_the_ttl(client_and_pings, clock):
    """An 'available' verdict may not survive the TTL once Gateway stops answering."""
    client, _, responses = client_and_pings

    assert await client.ping() is True
    responses["status"] = "down"  # Gateway dies right after the cached ping

    clock.advance(TTL)
    assert await client.ping() is False


def test_the_ttl_stays_short_enough_to_be_a_guard():
    assert 0 < TTL <= 5, "the guard reports outages within the TTL; keep it in the seconds range"


# ===============================================================
# Invalidation: a failed Gateway call must not leave a stale 'up'
# ===============================================================


class _OkResponse:
    """Minimal stand-in for the aiohttp response a healthy Gateway returns."""

    ok = True
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def json(self):
        return {"status": "ok"}


class _FlakySession:
    """Session that answers until ``state['down']`` flips, then refuses to connect."""

    def __init__(self, state):
        self.state = state

    def get(self, *args, **kwargs):
        if self.state["down"]:
            raise aiohttp.ClientConnectionError("connection refused")
        return _OkResponse()

    post = get
    delete = get


@pytest.mark.asyncio
async def test_a_failed_gateway_call_invalidates_the_cached_verdict(monkeypatch, clock):
    """A connection failure on any call drops the cache, so the outage is not masked mid-TTL."""
    client = GatewayClient()
    state = {"down": False}

    async def session():
        return _FlakySession(state)

    monkeypatch.setattr(client, "_get_session", session)

    assert await client.ping() is True
    assert client._ping_cache is not None

    # Gateway goes down mid-TTL; an unrelated guarded handler's call is the one that finds out.
    state["down"] = True
    assert await client.get_wallets() is None
    assert client._ping_cache is None, "an unreachable Gateway must clear the cached verdict"

    # ... and the very next guard (still inside the TTL) sees the outage, not the stale 'up'.
    assert await client.ping() is False


@pytest.mark.asyncio
async def test_missing_mtls_certs_also_invalidate_the_cached_verdict(monkeypatch, clock):
    client = GatewayClient()
    client._ping_cache = (clock.monotonic() + TTL, True)

    async def no_certs():
        raise FileNotFoundError("gateway client cert missing")

    monkeypatch.setattr(client, "_get_session", no_certs)

    result = await client.get_wallets()

    assert result["status"] == 503
    assert client._ping_cache is None


# ==========================================
# The guard itself: ping count and the 503
# ==========================================


@pytest.fixture
def guarded_client_for():
    app = FastAPI()

    @app.get("/guarded", dependencies=[Depends(require_gateway_online)])
    async def guarded():
        return {"ok": True}

    def build(gateway):
        accounts_service = SimpleNamespace(gateway_client=gateway)
        app.dependency_overrides[get_accounts_service] = lambda: accounts_service
        return TestClient(app, raise_server_exceptions=False)

    return build


def test_many_guarded_requests_produce_one_ping(guarded_client_for, client_and_pings, clock):
    gateway, pings, _ = client_and_pings
    client = guarded_client_for(gateway)

    for _ in range(5):
        assert client.get("/guarded").status_code == 200

    assert len(pings) == 1, f"5 guarded requests inside the TTL pinged Gateway {len(pings)} times"


def test_the_503_carries_retry_after_and_the_shared_detail(guarded_client_for, client_and_pings, clock):
    gateway, _, responses = client_and_pings
    responses["status"] = "down"
    client = guarded_client_for(gateway)

    response = client.get("/guarded")

    assert response.status_code == 503
    assert response.json()["detail"] == GATEWAY_UNAVAILABLE_DETAIL
    assert response.headers["Retry-After"] == str(math.ceil(TTL))
    assert int(response.headers["Retry-After"]) >= 1
