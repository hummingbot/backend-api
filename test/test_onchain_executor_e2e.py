"""Live: an onchain executor commits a 0-value self-transfer on Base through a running API.

Needs a running hummingbot-api whose environment carries an Aomi bearer, plus:

    API_URL, API_USER, API_PASSWORD           the API and its basic auth
    AOMI_TOKEN or AOMI_TOKEN_FILE             the same bearer, so the test can discover the wallet
    AOMI_E2E_WALLET (optional)                the kernel wallet; discovered through the pipeline when unset

Run with: pytest test/test_onchain_executor_e2e.py -v -m integration
"""
import asyncio
import os
import time

import pytest

pytest.importorskip("aomi")

from aomi.pipeline.client import PipelineClient  # noqa: E402

BASE = 8453
USDC_ON_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
POLL_SECONDS = 2
DEADLINE_SECONDS = 180


def _env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(f"{name} not set")
    return value


def _aomi_token():
    token_file = os.environ.get("AOMI_TOKEN_FILE", "").strip()
    if token_file:
        with open(token_file, encoding="utf-8") as handle:
            return handle.read().strip()
    return _env("AOMI_TOKEN")


@pytest.fixture
def api():
    aiohttp = pytest.importorskip("aiohttp")
    url = _env("API_URL").rstrip("/")
    auth = aiohttp.BasicAuth(_env("API_USER"), _env("API_PASSWORD"))
    return url, auth


@pytest.fixture
def aomi_url():
    return os.environ.get("AOMI_URL", "https://chat.aomi.dev").rstrip("/")


async def _wallet(aomi_url):
    wallet = os.environ.get("AOMI_E2E_WALLET", "").strip()
    if wallet:
        return wallet
    async with PipelineClient(aomi_url, _aomi_token()) as client:
        holdings = await client.evm_token_holdings(BASE, USDC_ON_BASE)
    holder = holdings.get("holder") if isinstance(holdings, dict) else None
    if not holder:
        pytest.skip(f"could not discover the kernel wallet from token-holdings: {holdings!r}")
    return holder


async def _create(session, api, wallet, **config):
    url, auth = api
    payload = {
        "account_name": "master_account",
        "controller_id": "e2e",
        "executor_config": {
            "type": "onchain_executor",
            "chain_id": BASE,
            "mode": "calls",
            "calls": [PipelineClient.native_transfer(wallet, chain_id=BASE, value="0", description="e2e self-transfer")],
            **config,
        },
    }
    async with session.post(f"{url}/executors/", json=payload, auth=auth) as response:
        body = await response.json()
        assert response.status in (200, 201), body
        return body["executor_id"]


async def _wait_for_termination(session, api, executor_id):
    """Poll until TERMINATED; while RUNNING, the log endpoint must already have entries."""
    url, auth = api
    logs_seen_while_running = False
    deadline = time.monotonic() + DEADLINE_SECONDS
    while time.monotonic() < deadline:
        async with session.get(f"{url}/executors/{executor_id}", auth=auth) as response:
            detail = await response.json()
            assert response.status == 200, detail
        if detail["status"] == "TERMINATED":
            return detail, logs_seen_while_running
        async with session.get(f"{url}/executors/{executor_id}/logs", auth=auth) as response:
            logs = await response.json()
            if response.status == 200 and logs.get("total_count", 0) > 0:
                logs_seen_while_running = True
        await asyncio.sleep(POLL_SECONDS)
    pytest.fail(f"executor {executor_id} did not terminate within {DEADLINE_SECONDS}s")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_config_schema_describes_the_executor(api):
    import aiohttp

    url, auth = api
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{url}/executors/types/onchain_executor/config", auth=auth) as response:
            body = await response.json()
            assert response.status == 200, body

    names = {field["name"] for field in body["fields"]}
    assert {"chain_id", "mode", "calls"} <= names


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_self_transfer_on_base_is_committed(api, aomi_url):
    import aiohttp

    wallet = await _wallet(aomi_url)
    async with aiohttp.ClientSession() as session:
        executor_id = await _create(session, api, wallet)
        detail, logs_seen = await _wait_for_termination(session, api, executor_id)

    custom_info = detail["custom_info"]
    assert logs_seen, "GET /executors/{id}/logs stayed empty while the executor ran"
    assert custom_info["wallet_address"].lower() == wallet.lower()
    error = custom_info.get("error") or {}
    if (
        detail["close_type"] == "FAILED"
        and error.get("backend_code") == "pipeline_commit_failed"
        and os.environ.get("AOMI_E2E_REQUIRE_COMMIT", "").strip() in ("", "0", "false")
    ):
        pytest.skip(
            f"staging could not sign/broadcast for {wallet}: {error.get('backend_code')} "
            f"(request {error.get('request_id')}); the executor recorded the failure; "
            "set AOMI_E2E_REQUIRE_COMMIT=1 to fail instead"
        )
    assert detail["close_type"] == "COMPLETED", detail
    assert custom_info["committed"] is True
    assert custom_info["tx_hashes"], custom_info
    print(f"\nonchain_executor {executor_id} committed on Base: {custom_info['tx_hashes'][0]}")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_dry_run_simulates_without_committing(api, aomi_url):
    import aiohttp

    wallet = await _wallet(aomi_url)
    async with aiohttp.ClientSession() as session:
        executor_id = await _create(session, api, wallet, commit=False)
        detail, _ = await _wait_for_termination(session, api, executor_id)

    assert detail["close_type"] == "COMPLETED", detail
    custom_info = detail["custom_info"]
    assert custom_info["committed"] is False
    assert custom_info["simulation_passed"] is True
    assert custom_info["tx_hashes"] == []
