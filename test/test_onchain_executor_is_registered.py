"""The ExecutorService can create, list and complete an onchain executor.

The executor has no connector, so ``create_executor`` must not try to prepare a market for it:
its ``connector_name`` is a chain name ("base"), and ``_prepare_market`` would hand that to the
trading interface to build a Gateway connector. The record still needs a connector and pair
(both NOT NULL), which the typed config derives.
"""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("hummingbot")
pytest.importorskip("aomi")

from aomi.pipeline.models import Build, CommitOutcome  # noqa: E402
from hummingbot.strategy_v2.executors.data_types import ExecutorConfigBase  # noqa: E402
from hummingbot.strategy_v2.models.executors import CloseType  # noqa: E402

from config import AomiSettings  # noqa: E402
from services.executor_service import ExecutorService  # noqa: E402
from services.onchain_executor import OnchainExecutor  # noqa: E402

WALLET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
DIGEST = "0x" + "ab" * 32
TX_HASH = "0x" + "cd" * 32
A_CALL = {"to": WALLET, "value": "0", "data": {"signature": "", "args": [], "raw": ""}, "description": "self"}
SIMULATED = {
    "status": "simulated",
    "digest": DIGEST,
    "actions": [{"chain_id": 8453, "from": WALLET, "to": WALLET, "value": "0", "label": "self-transfer"}],
    "simulation": {"status": "passed", "gas": {"units": "21000", "priceWei": "1", "nativeCost": "0.000021"}},
}
CONFIRMED = {"status": "committed", "digest": DIGEST, "result": {"status": "confirmed", "tx_hashes": [TX_HASH]}}


class FakePipelineClient:
    """A /v1/pipeline whose every call succeeds."""

    async def stage_evm(self, actions, *, app=None, skills=None):
        return Build.from_json({**SIMULATED, "status": "staged"}, "evm")

    async def simulate(self, build):
        return Build.from_json(dict(SIMULATED), build.chain)

    async def commit(self, build, *, idempotency_key=None):
        return CommitOutcome.from_response(dict(CONFIRMED))

    async def close(self):
        pass


def _strategy():
    return SimpleNamespace(connectors={}, current_timestamp=1000.0, _market_data_service=None)


def _service(update_interval=0.01):
    service = ExecutorService.__new__(ExecutorService)
    service.default_account = "master_account"
    service.update_interval = update_interval
    service.max_retries = 3
    service.db_manager = None
    service._trading_interfaces = {"master_account": _strategy()}
    service._trading_service = MagicMock()
    service._active_executors = {}
    service._executor_metadata = {}
    service._positions_held = {}
    service._lp_position_addresses = {}
    service._lp_rent_recorded = set()
    service._lp_rent_retry_after = {}
    service._log_capture = MagicMock()
    service._log_capture.get_error_count.return_value = 0
    service._log_capture.get_last_error.return_value = None
    service._prepare_market = AsyncMock()
    service._persist_executor_created = AsyncMock()
    return service


@pytest.fixture
def fake_pipeline(monkeypatch):
    """The service builds the executor without a client factory, so the default one is replaced."""
    import services.onchain_executor as module

    client = FakePipelineClient()
    monkeypatch.setattr(module.settings, "aomi", AomiSettings(token="test-bearer", token_file=""))
    monkeypatch.setattr(OnchainExecutor, "_default_client", staticmethod(lambda: client))
    return client


@pytest.mark.asyncio
async def test_create_executor_does_not_prepare_a_market_for_a_chain(fake_pipeline):
    service = _service()

    result = await service.create_executor(
        {"type": "onchain_executor", "chain_id": 8453, "mode": "calls", "calls": [A_CALL]},
        controller_id="e2e",
    )
    executor = service._active_executors[result["executor_id"]]
    await asyncio.wait_for(executor.terminated.wait(), 5)

    service._prepare_market.assert_not_awaited()
    assert result["executor_type"] == "onchain_executor"
    assert result["controller_id"] == "e2e"
    assert executor.close_type == CloseType.COMPLETED


@pytest.mark.asyncio
async def test_the_record_gets_the_derived_connector_and_pair(fake_pipeline):
    """The executors table requires both, and the caller gave neither."""
    service = _service()

    result = await service.create_executor(
        {"type": "onchain_executor", "chain_id": 8453, "mode": "calls", "calls": [A_CALL]},
    )
    executor = service._active_executors[result["executor_id"]]
    await asyncio.wait_for(executor.terminated.wait(), 5)

    assert result["connector_name"] == "base"
    assert result["trading_pair"] == "ETH-ETH"
    metadata = service._executor_metadata[result["executor_id"]]
    assert metadata["connector_name"] == "base"
    assert metadata["trading_pair"] == "ETH-ETH"
    assert metadata["executor_type"] == "onchain_executor"


@pytest.mark.asyncio
async def test_executors_with_connectors_still_get_their_market_prepared(monkeypatch):
    """The USES_CONNECTORS gate defaults open: a class that does not declare it is prepared as before."""

    class DummyConfig(ExecutorConfigBase):
        type: Literal["dummy_executor"] = "dummy_executor"
        connector_name: str
        trading_pair: str

    class DummyExecutor:
        def __init__(self, strategy, config, update_interval, max_retries):
            self.config = config
            self.is_closed = False
            self.status = SimpleNamespace(name="RUNNING")

        def start(self):
            pass

    monkeypatch.setitem(ExecutorService.EXECUTOR_REGISTRY, "dummy_executor", (DummyExecutor, DummyConfig))
    service = _service()

    await service.create_executor({"type": "dummy_executor", "connector_name": "binance", "trading_pair": "BTC-USDT"})

    service._prepare_market.assert_awaited_once_with("master_account", "binance", "BTC-USDT")


@pytest.mark.asyncio
async def test_completion_persists_the_transaction_hashes(fake_pipeline):
    service = _service()
    updates = []

    class _Repo:
        def __init__(self, _session):
            pass

        async def update_executor(self, **kwargs):
            updates.append(kwargs)

    @asynccontextmanager
    async def session_context():
        yield MagicMock()

    service.db_manager = MagicMock()
    service.db_manager.get_session_context = session_context
    import services.executor_service as module
    monkeypatch_target = module.ExecutorRepository
    module.ExecutorRepository = _Repo
    try:
        result = await service.create_executor(
            {"type": "onchain_executor", "chain_id": 8453, "mode": "calls", "calls": [A_CALL]},
        )
        executor_id = result["executor_id"]
        executor = service._active_executors[executor_id]
        await asyncio.wait_for(executor.terminated.wait(), 5)

        await service._handle_executor_completion(executor_id)
    finally:
        module.ExecutorRepository = monkeypatch_target

    assert len(updates) == 1
    update = updates[0]
    assert update["executor_id"] == executor_id
    assert update["status"] == "TERMINATED"
    assert update["close_type"] == "COMPLETED"
    final_state = json.loads(update["final_state"])
    assert final_state["tx_hashes"] == [TX_HASH]
    assert final_state["committed"] is True
    assert "transaction_hash" not in final_state
    assert "orphaned_position" not in final_state
    assert executor_id not in service._active_executors


@pytest.mark.asyncio
async def test_a_running_onchain_executor_is_listed(fake_pipeline):
    service = _service(update_interval=0.5)

    result = await service.create_executor(
        {"type": "onchain_executor", "chain_id": 8453, "mode": "calls", "calls": [A_CALL]},
    )
    executor_id = result["executor_id"]
    executor = service._active_executors[executor_id]
    try:
        formatted = service._format_executor_info(executor_id, executor)
    finally:
        executor.early_stop()
        await asyncio.wait_for(executor.terminated.wait(), 5)

    assert formatted["executor_id"] == executor_id
    assert formatted["executor_type"] == "onchain_executor"
    assert formatted["custom_info"]["chain_id"] == 8453
    json.dumps(formatted, default=str)


@pytest.mark.asyncio
async def test_the_type_listing_offers_it():
    from routers.executors import get_available_executor_types

    listing = await get_available_executor_types()

    types = {entry["type"]: entry for entry in listing["executor_types"]}
    assert "onchain_executor" in types
    assert types["onchain_executor"]["description"]
    assert types["onchain_executor"]["use_case"]


@pytest.mark.asyncio
async def test_onchain_never_starts_before_durable_creation(fake_pipeline, monkeypatch):
    service = _service()
    started = MagicMock()
    monkeypatch.setattr(OnchainExecutor, "start", started)

    async def failed_storage(*_):
        started.assert_not_called()
        raise RuntimeError("storage unavailable")

    service._persist_executor_created = failed_storage
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await service.create_executor(
            {"type": "onchain_executor", "chain_id": 8453, "mode": "calls", "calls": [A_CALL]},
        )
    started.assert_not_called()
    assert service._active_executors == {}
    assert service._executor_metadata == {}


@pytest.mark.asyncio
async def test_onchain_creation_requires_storage():
    service = _service()
    service._executor_metadata["test"] = {"executor_type": "onchain_executor"}
    with pytest.raises(RuntimeError, match="persistent executor storage"):
        await ExecutorService._persist_executor_created(service, "test", MagicMock())


@pytest.mark.asyncio
async def test_failed_completion_storage_keeps_result_visible_for_retry(fake_pipeline):
    service = _service()
    result = await service.create_executor(
        {"type": "onchain_executor", "chain_id": 8453, "mode": "calls", "calls": [A_CALL]},
    )
    executor_id = result["executor_id"]
    executor = service._active_executors[executor_id]
    await asyncio.wait_for(executor.terminated.wait(), 5)
    service._persist_executor_completed = AsyncMock(side_effect=[RuntimeError("offline"), None])
    with pytest.raises(RuntimeError, match="offline"):
        await service._handle_executor_completion(executor_id)
    assert service._active_executors[executor_id] is executor
    assert executor.get_custom_info()["tx_hashes"] == [TX_HASH]
    assert executor_id in service._executor_metadata
    await service._handle_executor_completion(executor_id)
    assert executor_id not in service._active_executors
    assert executor_id not in service._executor_metadata
