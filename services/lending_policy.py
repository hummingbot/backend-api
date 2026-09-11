"""Operator-owned Base USDC allocation limits, enforced before durable admission."""
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable

from aomi.pipeline import LendingPlan

from services.lending_positions import BASE_AAVE_USDC, LendingPosition


class LendingPolicy:
    def __init__(self, data: Dict[str, Any]):
        allowed = {"wallet", "account_name", "controller_limits_raw", "max_total_supply_raw", "max_action_raw", "max_gas_quote"}
        if not isinstance(data, dict) or set(data) != allowed:
            raise ValueError("Lending policy must specify wallet, account, controller limits, total, action and gas limits")
        self.wallet = data["wallet"]
        if not isinstance(self.wallet, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", self.wallet):
            raise ValueError("Invalid policy wallet")
        self.wallet = self.wallet.lower()
        self.account = data["account_name"]
        if not isinstance(self.account, str) or not self.account:
            raise ValueError("Invalid policy account")
        grants = data["controller_limits_raw"]
        if not isinstance(grants, dict) or not grants or any(not isinstance(k, str) or not k for k in grants):
            raise ValueError("Policy requires named controller grants")
        self.controllers = {k: self.units(v) for k, v in grants.items()}
        self.total = self.units(data["max_total_supply_raw"])
        self.action = self.units(data["max_action_raw"])
        self.gas = Decimal(str(data["max_gas_quote"]))
        if not self.gas.is_finite() or self.gas <= 0:
            raise ValueError("Policy gas limit must be positive and finite")

    @staticmethod
    def units(value: Any) -> int:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value) or not 0 < int(value) < 2 ** 256 - 1:
            raise ValueError("Policy amounts must be positive bounded raw-unit strings")
        return int(value)

    @classmethod
    def load(cls, path: str):
        return cls(json.loads(Path(path).read_text())) if path else None

    def report(self) -> Dict[str, Any]:
        return {
            "enabled": True, "chain_id": BASE_AAVE_USDC[0], "pool": BASE_AAVE_USDC[1], "asset": BASE_AAVE_USDC[2],
            "wallet": self.wallet, "account_name": self.account, "decimals": 6,
            "controller_limits_raw": {k: str(v) for k, v in self.controllers.items()},
            "max_total_supply_raw": str(self.total), "max_action_raw": str(self.action), "max_gas_quote": str(self.gas),
            "scope": "net_contributions_and_pending_supplies", "automatic_admission": "database_serialized",
        }

    def admit(self, config, account: str, controller: str, records: Iterable[Dict[str, Any]]):
        if config.commit is False:
            return
        plan = config.lending
        if config.mode != "lending" or not isinstance(plan, LendingPlan):
            raise ValueError("Configured lending policy permits only exact lending plans")
        if account != self.account or controller not in self.controllers:
            raise ValueError("Controller or account has no lending grant")
        if (plan.chain_id, plan.pool, plan.asset) != BASE_AAVE_USDC or plan.wallet != self.wallet:
            raise ValueError("Lending plan is outside the approved wallet and market")
        if plan.amount > self.action or config.max_gas_quote is None or config.max_gas_quote > self.gas:
            raise ValueError("Lending action or gas limit exceeds policy")
        records = list(records)
        # Raw historical commits cannot be reconstructed as lending contributions.
        if any((r.get("config") or {}).get("commit") is not False
               and (r.get("config") or {}).get("mode") != "lending" for r in records):
            raise ValueError("Non-lending history requires reconciliation before automatic allocation")
        positions = LendingPosition.from_executors(records)
        total = own = controller_reserved = pending_withdraw = 0
        for row in positions:
            if (row["chain_id"], row["pool"], row["asset"], row["wallet"]) != (*BASE_AAVE_USDC, self.wallet):
                continue
            reserved = int(row["net_contributed_raw"]) + int(row["pending_supply_raw"])
            total += reserved
            if row["account_name"] == account and row["controller_id"] == controller:
                controller_reserved += reserved
                own += int(row["net_contributed_raw"])
                pending_withdraw += int(row["pending_withdraw_raw"])
        if plan.action == "supply":
            if total + plan.amount > self.total or controller_reserved + plan.amount > self.controllers[controller]:
                raise ValueError("Lending allocation exceeds remaining total or controller grant")
        elif plan.amount > max(0, own - pending_withdraw):
            raise ValueError("Withdrawal exceeds this controller's unreserved contributions")
