"""OnchainExecutorConfig validates its two modes and derives what the executors table needs.

An onchain executor has no connector. The executors table still requires ``connector_name``
and ``trading_pair`` (NOT NULL), so the config derives both from ``chain_id`` when the caller
leaves them out, and the record reads like any other executor's.
"""
import pytest

pytest.importorskip("hummingbot")

from pydantic import ValidationError  # noqa: E402

from models.executors import EXECUTOR_TYPES  # noqa: E402
from models.onchain_executor import OnchainExecutorConfig  # noqa: E402

WALLET = "0x1111111111111111111111111111111111111111"
A_CALL = {"to": WALLET, "value": "0", "data": {"signature": "", "args": [], "raw": ""}, "description": "self"}


def test_operation_mode_requires_an_operation():
    with pytest.raises(ValidationError, match="requires 'operation'"):
        OnchainExecutorConfig(chain_id=8453, mode="operation")


@pytest.mark.parametrize("value", [-1, 0.5, True, "5000"])
def test_svm_fee_limit_requires_nonnegative_integer_lamports(value):
    with pytest.raises(ValidationError):
        OnchainExecutorConfig(chain="svm", chain_id=1, mode="operation", operation="deposit",
                              max_svm_network_fee_lamports=value)


def test_svm_fee_limit_cannot_be_applied_to_evm():
    with pytest.raises(ValidationError, match="requires svm"):
        OnchainExecutorConfig(chain_id=8453, mode="calls", calls=[A_CALL], max_svm_network_fee_lamports=5000)


def test_calls_mode_requires_calls():
    with pytest.raises(ValidationError, match="non-empty 'calls'"):
        OnchainExecutorConfig(chain_id=8453, mode="calls")
    with pytest.raises(ValidationError, match="non-empty 'calls'"):
        OnchainExecutorConfig(chain_id=8453, mode="calls", calls=[])


def test_the_modes_are_exclusive():
    with pytest.raises(ValidationError, match="does not take 'calls'"):
        OnchainExecutorConfig(chain_id=8453, mode="operation", operation="transfer", calls=[A_CALL])
    with pytest.raises(ValidationError, match="does not take 'operation'"):
        OnchainExecutorConfig(chain_id=8453, mode="calls", calls=[A_CALL], operation="transfer")
    with pytest.raises(ValidationError, match="does not take 'operation'"):
        OnchainExecutorConfig(chain_id=8453, mode="calls", calls=[A_CALL], arguments={"x": 1})


def test_svm_cannot_stage_raw_calls():
    with pytest.raises(ValidationError, match="svm"):
        OnchainExecutorConfig(chain="svm", chain_id=1, mode="calls", calls=[A_CALL])


def test_chain_id_must_be_positive():
    with pytest.raises(ValidationError):
        OnchainExecutorConfig(chain_id=0, mode="calls", calls=[A_CALL])


def test_base_derives_its_record_fields():
    cfg = OnchainExecutorConfig(chain_id=8453, mode="calls", calls=[A_CALL])

    assert cfg.connector_name == "base"
    assert cfg.trading_pair == "ETH-ETH"


def test_polygon_pays_gas_in_pol():
    cfg = OnchainExecutorConfig(chain_id=137, mode="calls", calls=[A_CALL])

    assert cfg.connector_name == "polygon"
    assert cfg.trading_pair == "POL-POL"


def test_an_unknown_chain_still_gets_a_name():
    cfg = OnchainExecutorConfig(chain_id=999999, mode="calls", calls=[A_CALL])

    assert cfg.connector_name == "evm-999999"
    assert cfg.trading_pair == "ETH-ETH"


def test_explicit_record_fields_are_kept():
    cfg = OnchainExecutorConfig(chain_id=8453, mode="calls", calls=[A_CALL], connector_name="base-l2", trading_pair="X-Y")

    assert cfg.connector_name == "base-l2"
    assert cfg.trading_pair == "X-Y"


def test_chain_id_is_injected_into_calls_that_omit_it():
    cfg = OnchainExecutorConfig(chain_id=8453, mode="calls", calls=[A_CALL, {**A_CALL, "chain_id": 1}])

    assert [c["chain_id"] for c in cfg.calls] == [8453, 1]
    assert "chain_id" not in A_CALL  # the caller's dict is not mutated


def test_a_bare_operation_resolves_into_the_app_catalog():
    cfg = OnchainExecutorConfig(chain_id=8453, mode="operation", app="uniswap", operation="swap")

    assert cfg.operation_path == "/v1/pipeline/apps/uniswap/operations/swap"


def test_an_absolute_operation_path_is_kept():
    cfg = OnchainExecutorConfig(chain_id=8453, mode="operation", operation="/v1/pipeline/skills/erc20/operations/transfer")

    assert cfg.operation_path == "/v1/pipeline/skills/erc20/operations/transfer"


def test_defaults():
    cfg = OnchainExecutorConfig(chain_id=8453, mode="calls", calls=[A_CALL])

    assert cfg.type == "onchain_executor"
    assert cfg.chain == "evm"
    assert cfg.app == "default"
    assert cfg.skills == []
    assert cfg.commit is True
    assert cfg.keep_position is False
    assert cfg.timeout_sec == 120
    assert cfg.max_gas_quote is None
    assert cfg.notional_quote is None
    assert cfg.id  # ExecutorConfigBase assigns one
    assert isinstance(cfg.timestamp, float)  # ExecutorInfo needs one even when the caller gave none


def test_the_type_is_registered():
    from services.executor_service import ExecutorService
    from services.onchain_executor import OnchainExecutor

    assert ExecutorService.EXECUTOR_REGISTRY["onchain_executor"] == (OnchainExecutor, OnchainExecutorConfig)


def test_the_type_is_in_the_api_literal():
    assert "onchain_executor" in EXECUTOR_TYPES.__args__


def test_the_schema_endpoint_reads_the_config():
    """/executors/types/onchain_executor/config is derived from the JSON schema."""
    from routers.executors import _extract_field_info

    schema = OnchainExecutorConfig.model_json_schema()
    fields = {f["name"]: f for f in _extract_field_info(schema, schema.get("$defs", {}))}

    assert fields["chain_id"]["required"] is True
    assert fields["chain_id"]["constraints"]["minimum"] == 1
    assert fields["mode"]["type"] == "enum"
    assert set(fields["mode"]["enum_values"]) == {"operation", "calls", "instructions", "lending"}
    assert fields["mode"]["required"] is True
    assert fields["commit"]["default"] is True
    assert fields["commit"]["required"] is False
    assert fields["calls"]["required"] is False
    for name in ("chain_id", "mode", "calls", "operation", "arguments", "max_gas_quote", "commit"):
        assert fields[name].get("description"), f"{name} has no description for the schema endpoint"


def test_a_bare_operation_resolves_into_a_single_named_skill():
    cfg = OnchainExecutorConfig(chain_id=8453, mode="operation", skills=["aave"], operation="aave_supply")
    assert cfg.operation_path == "/v1/pipeline/skills/aave/operations/aave_supply"


def test_two_skills_leave_a_bare_operation_in_the_app_catalog():
    cfg = OnchainExecutorConfig(
        chain_id=8453, mode="operation", app="default", skills=["aave", "lido"], operation="call_v4_swap"
    )
    assert cfg.operation_path == "/v1/pipeline/apps/default/operations/call_v4_swap"


def test_a_named_app_wins_over_a_single_skill():
    cfg = OnchainExecutorConfig(chain_id=8453, mode="operation", app="erc20", skills=["gas"], operation="transfer")
    assert cfg.operation_path == "/v1/pipeline/apps/erc20/operations/transfer"


@pytest.mark.parametrize(
    "operation",
    ["ask_authorization", "schedule_cron", "evm_commit_txs", "svm_stage_ix", "dummy_echo", "get_account_info",
     "/v1/pipeline/apps/default/operations/brave_search"],
)
def test_harness_lifecycle_read_and_test_operations_are_refused(operation):
    with pytest.raises(ValidationError) as exc:
        OnchainExecutorConfig(chain_id=8453, mode="operation", operation=operation)
    assert "cannot be built by this executor" in str(exc.value)


@pytest.mark.parametrize("operation", ["call_v4_swap", "lifi_prepare_swap_tx", "prepare_lido_claim", "aave_supply"])
def test_executable_operations_are_accepted(operation):
    cfg = OnchainExecutorConfig(chain_id=8453, mode="operation", operation=operation)
    assert cfg.operation == operation


def test_svm_records_derive_solana_fields():
    cfg = OnchainExecutorConfig(chain="svm", chain_id=1, mode="operation", operation="jupiter_prepare_swap")
    assert cfg.connector_name == "solana-mainnet-beta"
    assert cfg.trading_pair == "SOL-SOL"
    devnet = OnchainExecutorConfig(chain="svm", chain_id=1, cluster="devnet", mode="operation", operation="jupiter_prepare_swap")
    assert devnet.connector_name == "solana-devnet"


@pytest.mark.parametrize("overrides", [
    {"chain": "evm"}, {"instructions": []}, {"instructions": [{"instructions": []}]},
    {"calls": [A_CALL]}, {"operation": "swap"}, {"arguments": {}},
    {"mode": "operation", "operation": "swap"},
])
def test_instruction_mode_rejects_ambiguous_or_empty_bundles(overrides):
    values = dict(chain="svm", chain_id=1, mode="instructions",
                  instructions=[{"description": "test", "instructions": [{"program_id": "p"}]}])
    with pytest.raises(ValidationError):
        OnchainExecutorConfig(**{**values, **overrides})
