from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.lending_positions import BASE_AAVE_USDC, LendingPosition


def row(executor_id="supply", action="supply", amount=100_000_000, **info):
    return {
        "executor_id": executor_id, "account_name": "master_account", "controller_id": "reserves",
        "status": "TERMINATED", "config": {"mode": "lending", "commit": True, "lending": {
            "chain_id": 8453, "pool": "0x" + "1" * 40, "asset": "0x" + "2" * 40,
            "wallet": "0x" + "3" * 40, "action": action, "amount": amount,
        }}, "custom_info": {"committed": True, "tx_hashes": ["0x" + "a" * 64], **info},
    }


def test_completed_supply_survives_history_reconstruction_and_preserves_precision():
    history = [row(amount=123456789012345678901234567890)]
    position = LendingPosition.from_executors(history)[0]
    assert position["net_contributed_raw"] == "123456789012345678901234567890"
    assert position["requires_balance_reconciliation"] is True
    assert LendingPosition.from_executors(deepcopy(history)) == [position]


def test_confirmed_withdrawals_reduce_contributions_but_never_claim_a_closed_position():
    history = [row(), row("withdraw", "withdraw", 100_000_001, tx_hashes=["0x" + "b" * 64])]
    position = LendingPosition.from_executors(history)[0]
    assert position["net_contributed_raw"] == "0"
    assert position["withdrawn_raw"] == "100000001"
    assert position["requires_balance_reconciliation"] is True
    assert "pnl" not in position


def test_pending_and_failed_unknown_attempts_reserve_capital_and_do_not_reduce_contributions():
    supply = row("pending", amount=50, committed=False, commit_attempted=True)
    withdraw = row("withdraw", "withdraw", 20, committed=False)
    position = LendingPosition.from_executors([row(), supply, withdraw])[0]
    assert position["net_contributed_raw"] == "100000000"
    assert position["pending_supply_raw"] == "50"
    assert position["pending_withdraw_raw"] == "20"
    assert position["unresolved_executor_ids"] == ["pending", "withdraw"]


def test_dry_runs_and_proven_unsent_failures_allocate_nothing():
    dry = row()
    dry["config"]["commit"] = False
    failed = row("failed", committed=False, commit_attempted=False)
    assert LendingPosition.from_executors([dry, failed]) == []


def test_idempotent_receipt_replay_is_counted_once():
    position = LendingPosition.from_executors([row(), row("replay")])[0]
    assert position["supplied_raw"] == "100000000"
    assert position["executor_ids"] == ["replay", "supply"]


def test_receipts_cannot_be_reassigned_to_a_different_controller_or_amount():
    for changed in [row("changed", amount=1), row("changed")]:
        changed["controller_id"] = "another-agent"
        with pytest.raises(ValueError, match="conflicting receipt"):
            LendingPosition.from_executors([row(), changed])


def test_partial_receipt_overlap_cannot_double_count_a_batch():
    with pytest.raises(ValueError, match="conflicting receipt"):
        LendingPosition.from_executors([row(), row("partial", tx_hashes=["0x" + "a" * 64, "0x" + "b" * 64])])


@pytest.mark.parametrize("hashes", [[], ["bad"], None])
def test_confirmed_history_without_receipts_is_not_treated_as_a_balance(hashes):
    with pytest.raises(ValueError, match="missing transaction receipts"):
        LendingPosition.from_executors([row(tx_hashes=hashes)])


@pytest.mark.asyncio
async def test_position_route_fails_closed_when_storage_fails():
    from fastapi import HTTPException

    from routers.executors import get_lending_positions

    service = SimpleNamespace(get_lending_positions=AsyncMock(side_effect=RuntimeError("db unavailable")))
    with pytest.raises(HTTPException) as error:
        await get_lending_positions(service)
    assert error.value.status_code == 503
    assert "db unavailable" not in error.value.detail


@pytest.mark.asyncio
async def test_wallet_balance_is_not_distributed_or_double_fetched_between_controllers():
    first, second = row(), row("other", tx_hashes=["0x" + "b" * 64])
    second["controller_id"] = "another-agent"
    for record in (first, second):
        record["config"]["lending"].update(pool=BASE_AAVE_USDC[1], asset=BASE_AAVE_USDC[2])
    positions = LendingPosition.from_executors([first, second])
    client = SimpleNamespace(evm_token_holdings=AsyncMock(return_value={
        "chain_id": 8453, "token": "0x4e65fe4dba92790696d040ac24aa414708f5c0ab",
        "holder": "0x" + "3" * 40, "decimals": 6, "balance_raw": "200000020",
    }))
    result = await LendingPosition.read_wallet_balances(positions, client)
    assert client.evm_token_holdings.await_count == 1
    assert all(p["wallet_receipt_balance_raw"] == "200000020" for p in result)
    assert all(p["balance_scope"] == "wallet" for p in result)
    assert all(p["net_contributed_raw"] == "100000000" for p in result)


@pytest.mark.asyncio
async def test_wrong_wallet_balance_is_rejected():
    record = row()
    record["config"]["lending"].update(pool=BASE_AAVE_USDC[1], asset=BASE_AAVE_USDC[2])
    client = SimpleNamespace(evm_token_holdings=AsyncMock(return_value={
        "chain_id": 8453, "token": "0x4e65fe4dba92790696d040ac24aa414708f5c0ab",
        "holder": "0x" + "4" * 40, "decimals": 6, "balance_raw": "0",
    }))
    with pytest.raises(ValueError, match="does not match"):
        await LendingPosition.read_wallet_balances(LendingPosition.from_executors([record]), client)


@pytest.mark.asyncio
async def test_service_requires_full_history_and_propagates_a_database_failure(monkeypatch):
    from contextlib import asynccontextmanager

    from services.executor_service import ExecutorService

    @asynccontextmanager
    async def session_context():
        yield object()

    repository = SimpleNamespace(get_executors=AsyncMock(side_effect=RuntimeError("storage down")))
    monkeypatch.setattr("services.executor_service.ExecutorRepository", lambda session: repository)
    service = SimpleNamespace(db_manager=SimpleNamespace(get_session_context=session_context))
    with pytest.raises(RuntimeError, match="storage down"):
        await ExecutorService.get_lending_positions(service)
    repository.get_executors.assert_awaited_once_with(executor_type="onchain_executor", limit=None)
