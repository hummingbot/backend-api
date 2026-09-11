import pytest

from models.onchain_executor import OnchainExecutorConfig
from services.lending_policy import LendingPolicy
from services.lending_positions import BASE_AAVE_USDC

WALLET = "0x" + "3" * 40


def policy(**over):
    return LendingPolicy({
        "wallet": WALLET, "account_name": "master_account", "controller_limits_raw": {"a": "100", "b": "100"},
        "max_total_supply_raw": "150", "max_action_raw": "100", "max_gas_quote": "1", **over,
    })


def config(amount=60, action="supply", **over):
    return OnchainExecutorConfig(chain_id=8453, mode="lending", lending={
        "chain_id": 8453, "pool": BASE_AAVE_USDC[1], "asset": BASE_AAVE_USDC[2],
        "wallet": WALLET, "amount": amount, "action": action,
    }, max_gas_quote="1", **over)


def record(ident="first", amount=60, controller="a", action="supply", confirmed=False):
    return {"executor_id": ident, "account_name": "master_account", "controller_id": controller,
            "status": "RUNNING", "config": config(amount, action).model_dump(mode="json"),
            "custom_info": {"committed": confirmed, "tx_hashes": ["0x" + ident.encode().hex().ljust(64, "0")]}}


def admit(cfg, records=(), controller="a", account="master_account", rules=None):
    return (rules or policy()).admit(cfg, account, controller, records)


def test_pending_and_completed_supplies_both_consume_the_controller_grant():
    for confirmed in (False, True):
        history = [record(confirmed=confirmed)]
        admit(config(40), history)
        with pytest.raises(ValueError, match="remaining"):
            admit(config(41), history)


def test_total_limit_is_shared_across_controllers_and_hummingbot_accounts():
    history = [record(amount=100)]
    admit(config(50), history, "b")
    with pytest.raises(ValueError, match="remaining"):
        admit(config(51), history, "b")
    history[0]["account_name"] = "different_account"
    with pytest.raises(ValueError, match="remaining"):
        admit(config(51), history, "b")


def test_withdrawal_does_not_spend_another_controllers_position_or_pending_deposit():
    with pytest.raises(ValueError, match="Withdrawal"):
        admit(config(1, "withdraw"), [record()])
    with pytest.raises(ValueError, match="Withdrawal"):
        admit(config(1, "withdraw"), [record(confirmed=True)], "b")
    history = [record(confirmed=True), record("withdraw", 20, action="withdraw")]
    admit(config(40, "withdraw"), history)
    with pytest.raises(ValueError, match="Withdrawal"):
        admit(config(41, "withdraw"), history)


def test_only_confirmed_withdrawals_release_supply_capacity():
    history = [record(amount=100, confirmed=True), record("withdraw", 60, action="withdraw")]
    with pytest.raises(ValueError, match="remaining"):
        admit(config(1), history)
    history[1]["custom_info"]["committed"] = True
    admit(config(60), history)


def test_policy_refuses_changed_authority_ungranted_controller_and_untracked_history():
    with pytest.raises(ValueError, match="grant"):
        admit(config(), controller="stranger")
    with pytest.raises(ValueError, match="grant"):
        admit(config(), account="other")
    for field in ("wallet", "pool", "asset"):
        cfg = config().model_dump()
        cfg["lending"][field] = "0x" + "4" * 40
        with pytest.raises(ValueError, match="approved"):
            admit(OnchainExecutorConfig(**cfg))
    with pytest.raises(ValueError, match="reconciliation"):
        admit(config(), [{"config": {"mode": "calls", "commit": True}}])


def test_policy_limits_action_and_gas_even_for_explicit_creates():
    with pytest.raises(ValueError, match="action or gas"):
        admit(config(101))
    for gas in (None, "2"):
        cfg = config().model_dump()
        cfg["max_gas_quote"] = gas
        with pytest.raises(ValueError, match="action or gas"):
            admit(OnchainExecutorConfig(**cfg))


def test_policy_exact_raw_units_and_fail_closed_configuration(tmp_path):
    for bad in (0, "0", "-1", "1.0", str(2 ** 256 - 1)):
        with pytest.raises(ValueError):
            policy(max_total_supply_raw=bad)
    with pytest.raises(ValueError):
        policy(max_gas_quote="NaN")
    with pytest.raises(ValueError):
        policy(extra="unexpected")
    assert LendingPolicy.load("") is None
    with pytest.raises(FileNotFoundError):
        LendingPolicy.load(str(tmp_path / "missing.json"))
    assert policy().report()["max_total_supply_raw"] == "150"


def test_dry_runs_reserve_no_capital():
    history = [record(amount=100)]
    history[0]["config"]["commit"] = False
    admit(config(100), history)
