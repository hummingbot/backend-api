"""Operator limits for one simulated Solana transaction, before wallet handoff."""
from typing import Dict, List

from aomi.pipeline import Build
from pydantic import BaseModel, ConfigDict, Field, model_validator


class SvmSpendingPolicy(BaseModel):
    wallet: str = Field(min_length=1, strict=True)
    market: str = Field(min_length=1, strict=True)
    protocol_program: str = Field(min_length=1, strict=True)
    allowed_programs: List[str] = Field(min_length=1)
    max_debits_raw: Dict[str, str] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_limits(self):
        if self.protocol_program not in self.allowed_programs:
            raise ValueError("The selected protocol must be an allowed program")
        if len(set(self.allowed_programs)) != len(self.allowed_programs) or any(not p for p in self.allowed_programs):
            raise ValueError("Allowed programs must be nonempty and unique")
        if "native" not in self.max_debits_raw:
            raise ValueError("Include a native SOL debit ceiling for network fees and account funding")
        for asset, amount in self.max_debits_raw.items():
            if not asset or not amount or not amount.isascii() or not amount.isdecimal() or len(amount) > 20:
                raise ValueError("Debit limits must be unsigned raw integer strings keyed by asset")
            if int(amount) > 2**64 - 1:
                raise ValueError("Debit limit exceeds u64")
        return self

    def verify(self, build: Build, cluster: str) -> Dict[str, str]:
        """Cap observed per-account net debits; do not offset them with other credits.

        This is a preflight policy, not an on-chain spending grant. A complete
        balance-evidence guard is mandatory; missing rows never imply completeness.
        Independent multi-transaction simulations cannot prove a sequential budget.
        """
        simulation = build.simulation
        if build.chain != "svm" or build.from_address != self.wallet or not build.actions:
            raise ValueError("Spending policy wallet or chain differs from the staged plan")
        found_market = False
        for action in build.actions:
            instruction = action.get("instruction") if isinstance(action, dict) else None
            if not isinstance(instruction, dict) or action.get("lane") != "instruction":
                raise ValueError("Spending policy requires explicit instruction actions")
            if instruction.get("payer") != self.wallet or instruction.get("cluster") != cluster:
                raise ValueError("Staged wallet or network differs from the spending policy")
            program = instruction.get("program_id")
            if program not in self.allowed_programs:
                raise ValueError("Staged instruction uses a program outside the reviewed venue policy")
            accounts = instruction.get("accounts")
            if not isinstance(accounts, list) or any(not isinstance(a, dict) for a in accounts):
                raise ValueError("Staged account evidence is malformed")
            if program == self.protocol_program and any(a.get("pubkey") == self.market for a in accounts):
                found_market = True
        if not found_market:
            raise ValueError("Selected market is absent from the selected protocol's instructions")
        if simulation is None or not simulation.passed:
            raise ValueError("Spending policy requires a successful simulation")
        guards = [g for g in simulation.guards if isinstance(g, dict) and g.get("name") == "svm_balance_changes"]
        if len(guards) != 1 or guards[0].get("status") != "passed":
            raise ValueError("Complete Solana balance evidence is unavailable")
        fee_guards = [g for g in simulation.guards if isinstance(g, dict) and g.get("name") == "svm_network_fees"]
        if len(fee_guards) != 1 or fee_guards[0].get("status") != "passed" or len(simulation.fees) != 1:
            raise ValueError("Spending policy requires a single simulated transaction")
        totals: Dict[str, int] = {}
        for row in simulation.balance_changes:
            if row.cluster != cluster or type(row.step) is not int or row.step != 0 or not row.account or not row.asset:
                raise ValueError("Simulation balance identity is incomplete")
            if row.direction not in ("in", "out") or row.amount is None or not row.amount.isascii() or not row.amount.isdecimal():
                raise ValueError("Simulation balance amount is malformed")
            if len(row.amount) > 20 or int(row.amount) > 2**64 - 1:
                raise ValueError("Simulation balance amount exceeds u64")
            if row.account != self.wallet or row.direction != "out":
                continue
            if row.asset not in self.max_debits_raw:
                raise ValueError("Simulation debits a wallet asset outside the spending policy")
            totals[row.asset] = totals.get(row.asset, 0) + int(row.amount)
            if totals[row.asset] > int(self.max_debits_raw[row.asset]):
                raise ValueError(f"Simulated debit exceeds the operator limit for {row.asset}")
        return {asset: str(totals.get(asset, 0)) for asset in self.max_debits_raw}
