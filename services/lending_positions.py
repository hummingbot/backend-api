"""Persistent lending contributions reconstructed from executor history.

This is an attribution ledger, not a token balance or a yield calculation. A
completed supply remains here until an explicit withdrawal, and even a zero net
contribution requires a live receipt-token read before declaring a position closed.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List

from aomi.pipeline import LendingPlan

# https://github.com/bgd-labs/aave-address-book/blob/main/src/AaveV3Base.sol
# A receipt-token balance is wallet-wide; it is never silently attributed to one controller.
BASE_AAVE_USDC = (
    8453, "0xa238dd80c259a72e81d7e4664a9801593f98d1c5", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
)


@dataclass
class LendingPosition:
    account_name: str
    controller_id: str
    chain_id: int
    wallet: str
    pool: str
    asset: str
    supplied: int = 0
    withdrawn: int = 0
    pending_supply: int = 0
    pending_withdraw: int = 0
    executor_ids: List[str] = field(default_factory=list)
    unresolved_executor_ids: List[str] = field(default_factory=list)

    def report(self) -> Dict[str, Any]:
        return {
            "account_name": self.account_name,
            "controller_id": self.controller_id,
            "chain_id": self.chain_id,
            "wallet": self.wallet,
            "pool": self.pool,
            "asset": self.asset,
            "supplied_raw": str(self.supplied),
            "withdrawn_raw": str(self.withdrawn),
            "net_contributed_raw": str(max(0, self.supplied - self.withdrawn)),
            "pending_supply_raw": str(self.pending_supply),
            "pending_withdraw_raw": str(self.pending_withdraw),
            "executor_ids": sorted(self.executor_ids),
            "unresolved_executor_ids": sorted(self.unresolved_executor_ids),
            "requires_balance_reconciliation": True,
        }

    @staticmethod
    async def read_wallet_balances(positions: List[Dict[str, Any]], client) -> List[Dict[str, Any]]:
        balances = {}
        for position in positions:
            market = (position["chain_id"], position["pool"], position["asset"])
            if market != BASE_AAVE_USDC:
                position["balance_status"] = "unsupported_market"
                continue
            key = (*market, position["wallet"])
            if key not in balances:
                token = "0x4e65fe4dba92790696d040ac24aa414708f5c0ab"
                result = await client.evm_token_holdings(8453, token, position["wallet"])
                if (
                    not isinstance(result, dict)
                    or result.get("chain_id") != 8453
                    or str(result.get("token", "")).lower() != token
                    or str(result.get("holder", "")).lower() != position["wallet"]
                    or result.get("decimals") != 6
                    or not isinstance(result.get("balance_raw"), str)
                    or not re.fullmatch(r"[0-9]+", result["balance_raw"])
                ):
                    raise ValueError("Receipt-token balance response does not match the requested market")
                balances[key] = result["balance_raw"]
            position.update({
                "balance_status": "verified_wallet_balance",
                "wallet_receipt_balance_raw": balances[key],
                "receipt_token": "0x4e65fe4dba92790696d040ac24aa414708f5c0ab",
                "decimals": 6,
                "symbol": "USDC",
                "balance_scope": "wallet",
                # Do not distribute interest or untracked external deposits among controllers.
                "requires_balance_reconciliation": bool(position["unresolved_executor_ids"])
                or int(balances[key]) != 0,
            })
        return positions

    @classmethod
    def from_executors(cls, records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        positions = {}
        seen_ids = set()
        seen_receipts = {}
        for record in records:
            cfg = record.get("config") or {}
            if cfg.get("mode") != "lending" or cfg.get("commit") is False:
                continue
            raw_plan = cfg.get("lending")
            if not isinstance(raw_plan, dict):
                raise ValueError("Lending history contains an invalid plan")
            raw_plan = dict(raw_plan)
            amount = raw_plan.get("amount")
            if isinstance(amount, str) and re.fullmatch(r"[0-9]+", amount):
                raw_plan["amount"] = int(amount)
            plan = LendingPlan(**raw_plan)
            executor_id = record.get("executor_id") or record.get("id")
            if not isinstance(executor_id, str) or not executor_id or executor_id in seen_ids:
                raise ValueError("Lending history contains missing or duplicate executor IDs")
            seen_ids.add(executor_id)
            info = record.get("custom_info") or {}
            confirmed = info.get("committed") is True
            # An explicitly unsent terminal attempt cannot hold capital. Older
            # records without this evidence stay unresolved rather than becoming zero.
            if (not confirmed and str(record.get("status")).upper() == "TERMINATED"
                    and info.get("commit_attempted") is False):
                continue
            account = record.get("account_name")
            controller = record.get("controller_id")
            if not isinstance(account, str) or not account or not isinstance(controller, str) or not controller:
                raise ValueError("Lending history is missing account attribution")
            key = (account, controller, plan.chain_id, plan.wallet, plan.pool, plan.asset)
            position = positions.setdefault(key, cls(*key))
            if confirmed:
                hashes = info.get("tx_hashes")
                if not isinstance(hashes, list) or not hashes or any(
                    not isinstance(h, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", h) for h in hashes
                ):
                    raise ValueError("Confirmed lending history is missing transaction receipts")
                identity = (key, plan.action, plan.amount, tuple(sorted(h.lower() for h in hashes)))
                receipts = [(plan.chain_id, h.lower()) for h in hashes]
                prior = [seen_receipts[r] for r in receipts if r in seen_receipts]
                if prior:
                    if len(prior) != len(receipts) or any(p != identity for p in prior):
                        raise ValueError("Lending history contains conflicting receipt attribution")
                    position.executor_ids.append(executor_id)
                    continue  # idempotent commit replay, not another deposit
                seen_receipts.update({r: identity for r in receipts})
                if plan.action == "supply":
                    position.supplied += plan.amount
                else:
                    position.withdrawn += plan.amount
            else:
                if plan.action == "supply":
                    position.pending_supply += plan.amount
                else:
                    position.pending_withdraw += plan.amount
                position.unresolved_executor_ids.append(executor_id)
            position.executor_ids.append(executor_id)
        return [positions[key].report() for key in sorted(positions)]
