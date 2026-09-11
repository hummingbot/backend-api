import copy

import pytest
from aomi.pipeline import Build
from pydantic import ValidationError

from models.svm_policy import SvmSpendingPolicy


def policy(**overrides):
    return SvmSpendingPolicy(**{
        "wallet": "wallet", "market": "vault", "protocol_program": "protocol",
        "allowed_programs": ["protocol"], "max_debits_raw": {"native": "10000", "USDC": "2000000"},
        **overrides,
    })


def evidence():
    return {"status": "simulated", "actions": [{"lane": "instruction", "instruction": {
        "payer": "wallet", "cluster": "mainnet-beta", "program_id": "protocol", "accounts": [{"pubkey": "vault"}],
    }}], "simulation": {"status": "passed", "fees": [{}], "guards": [
        {"name": "svm_balance_changes", "status": "passed"},
        {"name": "svm_network_fees", "status": "passed"},
    ], "balanceChanges": [
        {"account": "wallet", "asset": "native", "direction": "out", "amount": "5000", "cluster": "mainnet-beta", "step": 0},
        {"account": "wallet", "asset": "USDC", "direction": "out", "amount": "2000000", "cluster": "mainnet-beta", "step": 0},
    ]}}


def verify(raw, limits=None):
    return (limits or policy()).verify(Build.from_json(raw, "svm"), "mainnet-beta")


def test_exact_inclusive_debit_ceiling():
    assert verify(evidence()) == {"native": "5000", "USDC": "2000000"}
    with pytest.raises(ValueError, match="exceeds"):
        verify(evidence(), policy(max_debits_raw={"native": "10000", "USDC": "1999999"}))


def test_credits_and_other_wallets_cannot_offset_our_debits():
    raw = evidence()
    credit = copy.deepcopy(raw["simulation"]["balanceChanges"][1])
    credit.update(direction="in", amount="2000000")
    raw["simulation"]["balanceChanges"].append(credit)
    with pytest.raises(ValueError, match="exceeds"):
        verify(raw, policy(max_debits_raw={"native": "10000", "USDC": "1"}))
    raw["simulation"]["balanceChanges"][1]["account"] = "another-wallet"
    assert verify(raw)["USDC"] == "0"


def test_multiple_account_debits_are_summed():
    raw = evidence()
    raw["simulation"]["balanceChanges"].append(copy.deepcopy(raw["simulation"]["balanceChanges"][1]))
    with pytest.raises(ValueError, match="exceeds"):
        verify(raw)


@pytest.mark.parametrize("mutation", [
    lambda r: r["simulation"].update(guards=[]),
    lambda r: r["simulation"]["guards"].append({"name": "svm_balance_changes", "status": "passed"}),
    lambda r: r["simulation"]["guards"][0].update(status="failed"),
    lambda r: r["simulation"]["fees"].append({}),
    lambda r: r["simulation"]["guards"][1].update(status="failed"),
    lambda r: r["simulation"]["balanceChanges"][0].update(account=None),
    lambda r: r["simulation"]["balanceChanges"][0].update(cluster="devnet"),
    lambda r: r["simulation"]["balanceChanges"][0].update(step=True),
    lambda r: r["simulation"]["balanceChanges"][0].update(amount="-1"),
    lambda r: r["simulation"]["balanceChanges"][1].update(asset="unapproved-asset"),
    lambda r: r["actions"][0]["instruction"].update(program_id="other-protocol"),
    lambda r: r["actions"][0]["instruction"].update(accounts=[{"pubkey": "other-vault"}]),
    lambda r: r["actions"][0]["instruction"].update(payer="another-wallet"),
])
def test_incomplete_evidence_or_changed_venue_refuses(mutation):
    raw = evidence()
    mutation(raw)
    with pytest.raises(ValueError):
        verify(raw)


@pytest.mark.parametrize("limits", [{"USDC": "1"}, {"native": "-1"}, {"native": 1}, {"native": "18446744073709551616"}])
def test_invalid_raw_limits_refuse_at_configuration(limits):
    with pytest.raises(ValidationError):
        policy(max_debits_raw=limits)


@pytest.mark.parametrize("unapproved_first", [False, True])
def test_mixed_markets_refuse_even_when_another_instruction_matches(unapproved_first):
    raw = evidence()
    other = copy.deepcopy(raw["actions"][0])
    other["instruction"]["accounts"] = [{"pubkey": "other-vault"}]
    raw["actions"].insert(0 if unapproved_first else 1, other)
    with pytest.raises(ValueError, match="Every selected-protocol instruction"):
        verify(raw)


def test_multiple_selected_market_instructions_and_support_programs_remain_allowed():
    raw = evidence()
    raw["actions"].append(copy.deepcopy(raw["actions"][0]))
    support = copy.deepcopy(raw["actions"][0])
    support["instruction"].update(program_id="token", accounts=[{"pubkey": "wallet-token"}])
    raw["actions"].append(support)
    assert verify(raw, policy(allowed_programs=["protocol", "token"]))["USDC"] == "2000000"
