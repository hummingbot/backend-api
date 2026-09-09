"""The onchain executor walks stage -> simulate -> risk check -> commit -> confirm, and fails closed.

Every case drives the executor by hand: ``on_start()`` then ``control_task()`` per tick, with
``evaluate_max_retries()`` after each tick exactly as core's control loop does. The pipeline is
a scripted fake that records what it was asked and raises on demand, so what is pinned here is
the executor's own policy: one commit at most, replayed under the same idempotency key; a
failing simulation or a non-retryable rejection ends the executor FAILED with the evidence in
custom_info; and nothing it reports can be mistaken for a Gateway swap or an LP position.
"""
import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

pytest.importorskip("hummingbot")
pytest.importorskip("aomi")

from aomi.pipeline.errors import PipelineError  # noqa: E402
from aomi.pipeline.models import Build, CommitOutcome  # noqa: E402
from hummingbot.strategy_v2.models.base import RunnableStatus  # noqa: E402
from hummingbot.strategy_v2.models.executors import CloseType  # noqa: E402

from config import AomiSettings  # noqa: E402
from models.onchain_executor import OnchainExecutorConfig  # noqa: E402
from services.onchain_executor import OnchainExecutor, OnchainExecutorInfo, Phase  # noqa: E402
from utils.executor_log_capture import ExecutorLogCapture, current_executor_id  # noqa: E402

WALLET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
DIGEST = "0x" + "ab" * 32
TX_HASH = "0x" + "cd" * 32
A_CALL = {"to": WALLET, "value": "0", "data": {"signature": "", "args": [], "raw": ""}, "description": "self"}

STAGED = {
    "version": 1,
    "status": "staged",
    "digest": DIGEST,
    "actions": [
        {"chain_id": 8453, "from": WALLET, "to": WALLET, "value": "0", "label": "self-transfer",
         "kind": "transfer", "protocol": None, "pending_tx_id": 1},
    ],
}
PASSED_SIMULATION = {
    "status": "passed",
    "balanceChanges": [{"account": WALLET, "asset": "native", "amount": "-21000000", "direction": "out",
                        "symbol": "ETH", "decimals": 18, "chainId": 8453}],
    "gas": {"units": "21000", "priceWei": "1000000000", "nativeCost": "0.000021"},
    "warnings": ["dust"],
    "fees": [],
    "guards": [],
    "logs": [],
}
SIMULATED = {**STAGED, "status": "simulated", "simulation": PASSED_SIMULATION}
REVERTED = {**STAGED, "status": "simulated", "simulation": {**PASSED_SIMULATION, "status": "reverted",
                                                            "warnings": ["execution reverted"]}}
UNPRICED = {**STAGED, "status": "simulated", "simulation": {**PASSED_SIMULATION, "gas": None}}

CONFIRMED = {"status": "committed", "digest": DIGEST, "result": {"status": "confirmed", "tx_hashes": [TX_HASH]}}
PENDING = {"status": "committed", "digest": DIGEST, "result": {"status": "pending_approval", "tx_ids": [7]}}
AA_SIGN = {"status": "committed", "digest": DIGEST, "result": {"status": "aa_sign_request"},
           "requests": [{"kind": "aa_sign", "tx_id": 7}]}


class FakePipelineClient:
    """A scripted /v1/pipeline. ``errors[method]`` is a queue of exceptions raised before the reply."""

    def __init__(self, *, staged=STAGED, simulated=SIMULATED, built=None, outcome=CONFIRMED, errors=None):
        self.staged = staged
        self.simulated = simulated
        self.built = built if built is not None else simulated
        self.outcome = outcome
        self.errors = {k: list(v) for k, v in (errors or {}).items()}
        self.calls = []
        self.commit_keys = []
        self.closed = False

    def _maybe_raise(self, method):
        queue = self.errors.get(method)
        if queue:
            raise queue.pop(0)

    async def stage_evm(self, actions, *, app=None, skills=None):
        self.calls.append(("stage_evm", {"actions": actions, "app": app, "skills": skills}))
        self._maybe_raise("stage_evm")
        return Build.from_json(dict(self.staged), "evm")

    async def build(self, chain, *, app=None, skills=None, operation=None, arguments=None, operations=None):
        self.calls.append(("build", {"chain": chain, "app": app, "skills": skills, "operation": operation,
                                     "arguments": arguments}))
        self._maybe_raise("build")
        return Build.from_json(dict(self.built), chain)

    async def simulate(self, build):
        self.calls.append(("simulate", {"digest": build.digest}))
        self._maybe_raise("simulate")
        return Build.from_json(dict(self.simulated), build.chain)

    async def commit(self, build, *, idempotency_key=None):
        self.calls.append(("commit", {"digest": build.digest}))
        self.commit_keys.append(idempotency_key or build.digest)
        assert build.is_simulated, "commit() needs a simulated Build"
        self._maybe_raise("commit")
        return CommitOutcome.from_response(dict(self.outcome))

    async def close(self):
        self.closed = True

    def methods_called(self):
        return [name for name, _ in self.calls]


def _strategy(market_data_service=None):
    return SimpleNamespace(connectors={}, current_timestamp=1000.0, _market_data_service=market_data_service)


def _config(**overrides):
    fields = {"chain_id": 8453, "mode": "calls", "calls": [A_CALL]}
    fields.update(overrides)
    return OnchainExecutorConfig(**fields)


def _executor(client, *, config=None, strategy=None, max_retries=10, update_interval=0.01):
    return OnchainExecutor(
        strategy=strategy or _strategy(),
        config=config or _config(),
        update_interval=update_interval,
        max_retries=max_retries,
        client_factory=lambda: client,
    )


async def _run(executor, max_ticks=30):
    """Drive the executor the way core's control loop does, until it terminates."""
    await executor.on_start()
    ticks = 0
    while not executor.is_closed and ticks < max_ticks:
        await executor.control_task()
        executor.evaluate_max_retries()
        ticks += 1
    return ticks


def _retryable():
    return PipelineError(503, "upstream_unavailable", "try again", request_id="req-503")


def _transport():
    return PipelineError(0, "transport", "ClientConnectorError: reset")


# ---------------------------------------------------------------------------- happy paths


@pytest.mark.asyncio
async def test_calls_mode_stages_simulates_and_commits():
    client = FakePipelineClient()
    executor = _executor(client)

    await _run(executor)

    assert executor.close_type == CloseType.COMPLETED
    assert client.methods_called() == ["stage_evm", "simulate", "commit"]
    staged = client.calls[0][1]
    assert staged["actions"][0]["chain_id"] == 8453
    assert staged["app"] == "default"
    info = executor.get_custom_info()
    assert info["phase"] == "done"
    assert info["committed"] is True
    assert info["outcome_kind"] == "confirmed"
    assert info["tx_hashes"] == [TX_HASH]
    assert info["digest"] == DIGEST
    assert info["wallet_address"] == WALLET
    assert info["action_count"] == 1
    assert info["actions"][0]["to"] == WALLET
    assert info["simulation_passed"] is True
    assert info["simulation_warnings"] == ["dust"]
    assert info["balance_changes"][0]["symbol"] == "ETH"
    assert info["gas_units"] == "21000"
    assert info["error"] is None
    assert info["reason"] is None


@pytest.mark.asyncio
async def test_operation_mode_builds_and_skips_the_simulate_call():
    """A build comes back already simulated, so the executor goes straight to the risk check."""
    client = FakePipelineClient()
    config = _config(mode="operation", calls=None, app="erc20", operation="transfer", arguments={"amount": "1"},
                     skills=["gas"])
    executor = _executor(client, config=config)

    await _run(executor)

    assert executor.close_type == CloseType.COMPLETED
    assert client.methods_called() == ["build", "commit"]
    built = client.calls[0][1]
    assert built["chain"] == "evm"
    assert built["app"] == "erc20"
    assert built["skills"] == ["gas"]
    assert built["operation"] == "/v1/pipeline/apps/erc20/operations/transfer"
    assert built["arguments"] == {"amount": "1"}


@pytest.mark.asyncio
async def test_a_build_that_is_not_simulated_yet_gets_simulated():
    client = FakePipelineClient(built=STAGED)
    executor = _executor(client, config=_config(mode="operation", calls=None, operation="transfer"))

    await _run(executor)

    assert executor.close_type == CloseType.COMPLETED
    assert client.methods_called() == ["build", "simulate", "commit"]


@pytest.mark.asyncio
async def test_a_dry_run_completes_without_committing():
    client = FakePipelineClient()
    executor = _executor(client, config=_config(commit=False))

    await _run(executor)

    assert executor.close_type == CloseType.COMPLETED
    assert client.methods_called() == ["stage_evm", "simulate"]
    info = executor.get_custom_info()
    assert info["committed"] is False
    assert info["simulation_passed"] is True
    assert info["tx_hashes"] == []


@pytest.mark.asyncio
async def test_one_phase_per_tick():
    client = FakePipelineClient()
    executor = _executor(client)
    await executor.on_start()

    seen = [executor.get_custom_info()["phase"]]
    while not executor.is_closed:
        await executor.control_task()
        seen.append(executor.get_custom_info()["phase"])

    assert seen == ["staging", "simulating", "risk_check", "committing", "confirming", "done"]


# ---------------------------------------------------------------------------- failures


@pytest.mark.asyncio
async def test_a_failed_simulation_ends_failed_with_the_evidence():
    client = FakePipelineClient(simulated=REVERTED)
    executor = _executor(client)

    await _run(executor)

    assert executor.close_type == CloseType.FAILED
    assert "commit" not in client.methods_called()
    info = executor.get_custom_info()
    assert info["reason"] == "simulation_failed"
    assert info["simulation_passed"] is False
    assert info["error"]["phase"] == "risk_check"
    assert info["error"]["evidence"]["status"] == "reverted"
    assert "execution reverted" in info["error"]["message"]


@pytest.mark.asyncio
async def test_a_stale_build_on_commit_is_not_retried():
    stale = PipelineError(409, "stale_build", "rebuild differs", request_id="req-409", backend_code="digest_mismatch")
    client = FakePipelineClient(errors={"commit": [stale]})
    executor = _executor(client)

    await _run(executor)

    assert executor.close_type == CloseType.FAILED
    assert client.methods_called().count("commit") == 1
    error = executor.get_custom_info()["error"]
    assert error["reason"] == "committing_rejected"
    assert error["status"] == 409
    assert error["code"] == "stale_build"
    assert error["backend_code"] == "digest_mismatch"
    assert error["request_id"] == "req-409"
    assert error["phase"] == "committing"


@pytest.mark.asyncio
async def test_a_rejected_stage_is_not_retried():
    client = FakePipelineClient(errors={"stage_evm": [PipelineError(422, "invalid_action", "bad calldata")]})
    executor = _executor(client)

    await _run(executor)

    assert executor.close_type == CloseType.FAILED
    assert client.methods_called() == ["stage_evm"]
    assert executor.get_custom_info()["reason"] == "staging_rejected"


@pytest.mark.asyncio
async def test_retryable_errors_count_against_max_retries():
    client = FakePipelineClient(errors={"simulate": [_retryable() for _ in range(10)]})
    executor = _executor(client, max_retries=2)

    await _run(executor)

    assert executor.close_type == CloseType.FAILED
    assert executor._current_retries == 3  # core stops once retries exceed the maximum
    assert client.methods_called() == ["stage_evm", "simulate", "simulate", "simulate"]


@pytest.mark.asyncio
async def test_a_retryable_error_keeps_the_phase():
    client = FakePipelineClient(errors={"simulate": [_retryable()]})
    executor = _executor(client)
    await executor.on_start()
    await executor.control_task()  # stage
    await executor.control_task()  # simulate -> 503

    assert executor.get_custom_info()["phase"] == "simulating"
    assert executor._current_retries == 1
    assert not executor.is_closed

    while not executor.is_closed:
        await executor.control_task()
    assert executor.close_type == CloseType.COMPLETED


@pytest.mark.asyncio
async def test_a_commit_lost_in_transport_is_replayed_under_the_same_key():
    """The idempotency key is the digest, so the replay returns the ledger entry, not a second tx."""
    client = FakePipelineClient(errors={"commit": [_transport()]})
    executor = _executor(client)

    await _run(executor)

    assert executor.close_type == CloseType.COMPLETED
    assert client.commit_keys == [DIGEST, DIGEST]
    assert executor._commit_sent is True
    assert executor.get_custom_info()["tx_hashes"] == [TX_HASH]


@pytest.mark.asyncio
async def test_an_unexpected_exception_fails_closed():
    class Boom(FakePipelineClient):
        async def simulate(self, build):
            raise KeyError("simulation")

    executor = _executor(Boom())

    await _run(executor)

    assert executor.close_type == CloseType.FAILED
    error = executor.get_custom_info()["error"]
    assert error["reason"] == "unexpected"
    assert "KeyError" in error["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome, kind", [(PENDING, "pending_approval"), (AA_SIGN, "aa_sign_request")])
async def test_a_commit_that_needs_a_wallet_signature_fails_as_awaiting_wallet(outcome, kind):
    client = FakePipelineClient(outcome=outcome)
    executor = _executor(client)

    await _run(executor)

    assert executor.close_type == CloseType.FAILED
    info = executor.get_custom_info()
    assert info["reason"] == "awaiting_wallet"
    assert info["outcome_kind"] == kind
    assert info["committed"] is False
    assert info["error"]["outcome_kind"] == kind
    if kind == "pending_approval":
        assert info["tx_ids"] == [7]
    else:
        assert info["commit_requests"] == [{"kind": "aa_sign", "tx_id": 7}]


@pytest.mark.asyncio
async def test_a_slow_pipeline_times_out_before_the_commit():
    client = FakePipelineClient(errors={"simulate": [_retryable() for _ in range(10)]})
    strategy = _strategy()
    executor = _executor(client, config=_config(timeout_sec=5), strategy=strategy)
    await executor.on_start()
    await executor.control_task()  # stage
    strategy.current_timestamp = 1006.0
    await executor.control_task()

    assert executor.close_type == CloseType.FAILED
    assert executor.get_custom_info()["reason"] == "timeout"
    assert "commit" not in client.methods_called()


@pytest.mark.asyncio
async def test_the_timeout_does_not_apply_once_the_commit_is_sent():
    client = FakePipelineClient(errors={"commit": [_transport()]})
    strategy = _strategy()
    executor = _executor(client, config=_config(timeout_sec=5), strategy=strategy)
    await executor.on_start()
    for _ in range(4):  # stage, simulate, risk check, commit (lost)
        await executor.control_task()
    assert executor._commit_sent is True
    strategy.current_timestamp = 2000.0

    while not executor.is_closed:
        await executor.control_task()

    assert executor.close_type == CloseType.COMPLETED
    assert client.commit_keys == [DIGEST, DIGEST]


@pytest.mark.asyncio
async def test_an_unconfigured_aomi_fails_the_executor_instead_of_raising(monkeypatch):
    import services.onchain_executor as module

    monkeypatch.setattr(module.settings, "aomi", AomiSettings(token="", token_file=""))
    executor = OnchainExecutor(strategy=_strategy(), config=_config())

    await executor.on_start()

    assert executor.is_closed
    assert executor.close_type == CloseType.FAILED
    error = executor.get_custom_info()["error"]
    assert error["reason"] == "startup"
    assert "AOMI_TOKEN" in error["message"]


# ---------------------------------------------------------------------------- early stop


@pytest.mark.asyncio
async def test_early_stop_before_the_commit_stops_the_executor():
    client = FakePipelineClient()
    executor = _executor(client)
    await executor.on_start()
    await executor.control_task()  # stage

    executor.early_stop()

    assert executor.is_closed
    assert executor.close_type == CloseType.EARLY_STOP
    await executor.control_task()
    assert client.methods_called() == ["stage_evm"]


@pytest.mark.asyncio
async def test_early_stop_after_the_commit_cannot_cancel_it():
    client = FakePipelineClient()
    executor = _executor(client)
    await executor.on_start()
    for _ in range(4):  # stage, simulate, risk check, commit
        await executor.control_task()
    assert executor._commit_sent is True

    executor.early_stop()

    assert not executor.is_closed
    assert executor.close_type is None
    while not executor.is_closed:
        await executor.control_task()
    assert executor.close_type == CloseType.COMPLETED


# ---------------------------------------------------------------------------- fees and gas budget


@pytest.mark.asyncio
async def test_gas_is_priced_when_the_api_has_a_rate():
    market_data = SimpleNamespace(get_rate=lambda base, quote: Decimal("3000"))
    executor = _executor(FakePipelineClient(), strategy=_strategy(market_data))

    await _run(executor)

    assert executor.close_type == CloseType.COMPLETED
    assert executor.get_cum_fees_quote() == Decimal("0.000021") * Decimal("3000")
    assert executor.get_custom_info()["fees_quote_source"] == "priced"


@pytest.mark.asyncio
async def test_a_gas_budget_refuses_an_expensive_commit():
    market_data = SimpleNamespace(get_rate=lambda base, quote: Decimal("3000"))
    client = FakePipelineClient()
    executor = _executor(client, config=_config(max_gas_quote=Decimal("0.01")), strategy=_strategy(market_data))

    await _run(executor)

    assert executor.close_type == CloseType.FAILED
    assert "commit" not in client.methods_called()
    assert executor.get_custom_info()["reason"] == "gas_over_budget"


@pytest.mark.asyncio
async def test_a_gas_budget_passes_a_cheap_commit():
    market_data = SimpleNamespace(get_rate=lambda base, quote: Decimal("3000"))
    executor = _executor(FakePipelineClient(), config=_config(max_gas_quote=Decimal("1")), strategy=_strategy(market_data))

    await _run(executor)

    assert executor.close_type == CloseType.COMPLETED


@pytest.mark.asyncio
async def test_unpriced_gas_reports_zero_fees_and_says_so():
    executor = _executor(FakePipelineClient(simulated=UNPRICED), config=_config(max_gas_quote=Decimal("0.01")))

    await _run(executor)

    assert executor.close_type == CloseType.FAILED
    assert executor.get_custom_info()["reason"] == "gas_unpriced"
    assert executor.get_cum_fees_quote() == Decimal("0")
    info = executor.get_custom_info()
    assert info["fees_quote_source"] == "unpriced"
    assert info["gas_native_cost"] is None


@pytest.mark.asyncio
async def test_no_market_data_service_means_unpriced():
    executor = _executor(FakePipelineClient())

    await _run(executor)

    assert executor.get_cum_fees_quote() == Decimal("0")
    assert executor.get_custom_info()["fees_quote_source"] == "unpriced"


# ---------------------------------------------------------------------------- reporting


@pytest.mark.asyncio
async def test_executor_info_carries_the_onchain_config():
    executor = _executor(FakePipelineClient())

    await _run(executor)
    info = executor.executor_info

    assert isinstance(info, OnchainExecutorInfo)
    assert info.side is None
    assert info.type == "onchain_executor"
    assert info.close_type == CloseType.COMPLETED
    assert info.net_pnl_quote == Decimal("0")
    assert info.filled_amount_quote == Decimal("0")
    dumped = info.model_dump()
    assert dumped["config"]["chain_id"] == 8453
    assert dumped["custom_info"]["tx_hashes"] == [TX_HASH]
    assert info.connector_name == "base"
    assert info.trading_pair == "ETH-ETH"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [CONFIRMED, PENDING])
async def test_custom_info_never_looks_like_a_swap_or_an_lp_position(outcome):
    """ExecutorService records custom_info['transaction_hash'] as a Gateway swap and flags
    custom_info['position_address'] as an orphaned position."""
    executor = _executor(FakePipelineClient(outcome=outcome))
    snapshots = [executor.get_custom_info()]
    await executor.on_start()
    while not executor.is_closed:
        await executor.control_task()
        snapshots.append(executor.get_custom_info())

    for info in snapshots:
        assert "transaction_hash" not in info
        assert "position_address" not in info


@pytest.mark.asyncio
async def test_custom_info_is_json_serializable_at_every_phase():
    import json

    executor = _executor(FakePipelineClient(simulated=REVERTED))
    json.dumps(executor.get_custom_info())
    await executor.on_start()
    while not executor.is_closed:
        await executor.control_task()
        json.dumps(executor.get_custom_info())


@pytest.mark.asyncio
async def test_a_failure_is_captured_in_the_executor_log():
    capture = ExecutorLogCapture()
    capture.install()
    executor = _executor(FakePipelineClient(simulated=REVERTED))
    token = current_executor_id.set(executor.config.id)
    try:
        await _run(executor)
    finally:
        current_executor_id.reset(token)
        capture.uninstall()

    assert executor.close_type == CloseType.FAILED
    assert capture.get_error_count(executor.config.id) >= 1
    assert "simulation_failed" in capture.get_last_error(executor.config.id)


@pytest.mark.asyncio
async def test_start_runs_the_whole_lifecycle_on_the_control_loop():
    """The real entry point: start() spawns the control loop, and on_stop closes the client."""
    client = FakePipelineClient()
    executor = _executor(client, update_interval=0.01)

    executor.start()
    assert executor.status == RunnableStatus.RUNNING
    await asyncio.wait_for(executor.terminated.wait(), 5)
    await asyncio.sleep(0.05)  # on_stop runs after the loop exits and schedules close()

    assert executor.status == RunnableStatus.TERMINATED
    assert executor.close_type == CloseType.COMPLETED
    assert executor.close_timestamp == 1000.0
    assert executor.get_custom_info()["phase"] == Phase.DONE.value
    assert client.closed is True


@pytest.mark.asyncio
async def test_default_pair_does_not_change_gas_budget_currency():
    from hummingbot.core.rate_oracle.utils import find_rate

    market = SimpleNamespace(get_rate=lambda base, quote: find_rate({"ETH-USDT": Decimal("3000")}, f"{base}-{quote}"))
    client = FakePipelineClient()
    executor = _executor(client, config=_config(max_gas_quote=Decimal("0.01")), strategy=_strategy(market))
    await _run(executor)
    assert executor.get_cum_fees_quote() == Decimal("0.063")
    assert executor.get_custom_info()["reason"] == "gas_over_budget"
    assert "commit" not in client.methods_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [None, Decimal("NaN"), Decimal("Infinity"), Decimal("-1"), Decimal("0")])
async def test_unavailable_or_invalid_price_blocks_budgeted_commit(rate):
    client = FakePipelineClient()
    market = SimpleNamespace(get_rate=lambda base, quote: rate)
    executor = _executor(client, config=_config(max_gas_quote=Decimal("1")), strategy=_strategy(market))
    await _run(executor)
    assert executor.get_custom_info()["reason"] == "gas_unpriced"
    assert "commit" not in client.methods_called()


@pytest.mark.asyncio
async def test_svm_gas_uses_sol_usdt():
    pairs = []

    def rate(base, quote):
        pairs.append((base, quote))
        return Decimal("150")
    client = FakePipelineClient()
    config = _config(chain="svm", chain_id=1, mode="operation", calls=None, operation="jupiter_swap", max_gas_quote=1)
    executor = _executor(client, config=config, strategy=_strategy(SimpleNamespace(get_rate=rate)))
    await _run(executor)
    assert pairs and set(pairs) == {("SOL", "USDT")}
