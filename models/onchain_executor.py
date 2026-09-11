"""Configuration for the onchain_executor.

An onchain executor runs one Aomi Pipeline lifecycle -- stage (or build), simulate, commit --
for a single on-chain transaction bundle. It has no exchange connector: the chain is named by
``chain_id`` and the bundle is either a catalog operation (``mode="operation"``) or a list of
raw calls (``mode="calls"``).

``connector_name`` and ``trading_pair`` exist because the executors table requires them; both
are derived from ``chain_id`` when not given so the executor records like any other.
"""
import time
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional

from aomi.pipeline.lending import LendingPlan
from hummingbot.strategy_v2.executors.data_types import ExecutorConfigBase
from pydantic import Field, model_validator

# chain_id -> connector_name used for the executors table and the API listing.
CHAIN_NAMES: Dict[int, str] = {
    1: "ethereum",
    10: "optimism",
    56: "bsc",
    137: "polygon",
    8453: "base",
    42161: "arbitrum",
    59144: "linea",
}

# chain_id -> native gas token symbol; ETH unless the chain says otherwise.
NATIVE_SYMBOLS: Dict[int, str] = {
    137: "POL",
    56: "BNB",
}

PIPELINE_PREFIX = "/v1/pipeline"


def _reject_reason(operation: str) -> Optional[str]:
    """Why the Aomi policy refuses this operation as a build target, or None.

    Harness plumbing (authorization, scheduling, threads), the raw stage/simulate/commit
    primitives, reads, and test fixtures are never valid executor operations.
    """
    try:
        from aomi.pipeline import policy
    except ImportError:  # the client ships it; without the client nothing runs anyway
        return None
    return policy.reject_reason(operation)


def chain_name(chain: str, chain_id: int) -> str:
    return CHAIN_NAMES.get(chain_id, f"{chain}-{chain_id}")


def native_symbol(chain_id: int) -> str:
    return NATIVE_SYMBOLS.get(chain_id, "ETH")


class OnchainExecutorConfig(ExecutorConfigBase):
    """Run one on-chain transaction bundle through the Aomi Pipeline (stage, simulate, commit)."""

    type: Literal["onchain_executor"] = "onchain_executor"
    connector_name: str = Field(
        default="",
        description="Chain name for record keeping (derived from chain_id when empty, e.g. 'base'); "
                    "no connector is used"
    )
    trading_pair: str = Field(
        default="",
        description="Pair for record keeping (derived as '<native>-<native>' when empty, e.g. 'ETH-ETH')"
    )
    chain: Literal["evm", "svm"] = Field(default="evm", description="Chain family the bundle targets")
    chain_id: int = Field(
        ..., ge=1, description="EVM chain id (1 ethereum, 10 optimism, 8453 base, 42161 arbitrum, ...); pass 1 for svm"
    )
    cluster: str = Field(default="mainnet-beta", description="svm only: Solana cluster the bundle targets")
    mode: Literal["operation", "calls", "lending"] = Field(
        ...,
        description="'operation' builds a catalog operation; 'calls' stages raw EVM calls; "
                    "'lending' verifies an exact Aave V3 plan"
    )
    lending: Optional[LendingPlan] = Field(
        default=None, description="Exact Aave V3 supply or withdrawal plan, with amounts in raw token units"
    )
    app: str = Field(default="default", description="Aomi app whose catalog and skills the pipeline uses")
    skills: List[str] = Field(default_factory=list, description="Skills to load alongside the app")
    operation: Optional[str] = Field(
        default=None,
        description="operation mode: operation name in the app catalog, or an absolute /v1/pipeline/... path"
    )
    arguments: Optional[Dict[str, Any]] = Field(
        default=None,
        description="operation mode: arguments for the operation, as its input schema describes them"
    )
    calls: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="calls mode: evm_stage_tx maps ({to, value, data: {signature, args, raw}, description, ...}); "
                    "chain_id is filled in from the config when a call omits it"
    )
    notional_quote: Optional[Decimal] = Field(
        default=None,
        description="Notional value of the bundle in USDT, for the caller's own risk accounting (not enforced here)"
    )
    max_gas_quote: Optional[Decimal] = Field(
        default=None,
        gt=0,
        description="Gas ceiling in USDT; refuse to commit if simulated gas exceeds it or cannot be priced"
    )
    keep_position: bool = Field(
        default=False,
        description="Reported back in custom_info for the caller; an on-chain bundle has no position to unwind"
    )
    timeout_sec: int = Field(
        default=120,
        ge=5,
        le=3600,
        description="Seconds allowed to reach the commit; the executor fails if staging/simulation takes longer"
    )
    commit: bool = Field(
        default=True,
        description="Commit after a passing simulation; False stops after simulation (dry run)"
    )

    @model_validator(mode="after")
    def _check_mode_and_derive(self):
        if self.mode == "lending":
            if self.lending is None or self.chain != "evm" or self.chain_id != self.lending.chain_id:
                raise ValueError("lending mode requires a plan on the configured EVM chain")
            if self.calls is not None or self.operation is not None or self.arguments is not None:
                raise ValueError("lending mode takes only a lending plan")
        elif self.lending is not None:
            raise ValueError("lending plan requires lending mode")
        elif self.mode == "operation":
            if not self.operation:
                raise ValueError("mode 'operation' requires 'operation'")
            if self.calls is not None:
                raise ValueError("mode 'operation' does not take 'calls'")
            reason = _reject_reason(self.operation.rsplit("/", 1)[-1])
            if reason:
                raise ValueError(f"operation cannot be built by this executor: {reason}")
        else:
            if not self.calls:
                raise ValueError("mode 'calls' requires a non-empty 'calls' list")
            if self.operation is not None or self.arguments is not None:
                raise ValueError("mode 'calls' does not take 'operation' or 'arguments'")
            if self.chain == "svm":
                raise ValueError("mode 'calls' stages EVM calls; use mode 'operation' for svm")
            self.calls = [
                call if call.get("chain_id") is not None else {**call, "chain_id": self.chain_id}
                for call in self.calls
            ]
        if self.timestamp is None:
            # The base validator only fills this when the field is passed; ExecutorInfo needs a float.
            self.timestamp = time.time()
        if not self.connector_name:
            self.connector_name = (
                f"solana-{self.cluster}" if self.chain == "svm" else chain_name(self.chain, self.chain_id)
            )
        if not self.trading_pair:
            symbol = "SOL" if self.chain == "svm" else native_symbol(self.chain_id)
            self.trading_pair = f"{symbol}-{symbol}"
        return self

    @property
    def operation_path(self) -> Optional[str]:
        """The pipeline path for ``operation``.

        A bare name resolves into the named app's catalog. When the app is left at ``default``
        and exactly one skill is named, it resolves into that skill instead, which is where
        protocol operations such as Aave or Morpho live.
        """
        if not self.operation:
            return None
        if self.operation.startswith("/"):
            return self.operation
        if self.app == "default" and len(self.skills) == 1:
            return f"{PIPELINE_PREFIX}/skills/{self.skills[0]}/operations/{self.operation}"
        return f"{PIPELINE_PREFIX}/apps/{self.app}/operations/{self.operation}"
