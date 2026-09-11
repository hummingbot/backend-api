"""OnchainExecutor: one Aomi Pipeline lifecycle as a hummingbot executor.

The executor walks stage (or build) -> simulate -> risk check -> commit -> confirm, one phase per
control tick, against ``/v1/pipeline`` through the ``aomi`` client. It holds no connector: the
fork simulation is what proves balances and the kernel signs on commit, so ``ExecutorBase`` is
constructed with ``connectors=[]`` and the balance check is a no-op.

Two things it is careful about:

* A commit is sent at most once. ``_commit_sent`` flips before the request leaves, and a retry
  after a transport error replays the same idempotency key (the Build digest), so a lost response
  cannot turn into a second on-chain transaction. ``early_stop`` after that point is a no-op.
* ``custom_info`` never carries ``transaction_hash`` or ``position_address``: the ExecutorService
  reads the first as a Gateway swap to record and the second as an orphaned LP position.
"""

import dataclasses
import hashlib
import inspect
import json
import logging
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from aomi.pipeline.client import PipelineClient
from aomi.pipeline.errors import PipelineError
from aomi.pipeline.models import Build, CommitOutcome
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.strategy_v2.executors.executor_base import ExecutorBase
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo

from config import settings
from models.onchain_executor import OnchainExecutorConfig, native_symbol

LOGGER_NAME = "hummingbot.strategy_v2.executors.onchain_executor"


class OnchainExecutorInfo(ExecutorInfo):
    """ExecutorInfo whose config is an OnchainExecutorConfig.

    Core's ``ExecutorInfo.config`` is a discriminated union of the eight core configs, so a config
    typed ``onchain_executor`` cannot pass through it.
    """

    config: OnchainExecutorConfig


class Phase(str, Enum):
    STAGING = "staging"
    SIMULATING = "simulating"
    RISK_CHECK = "risk_check"
    COMMITTING = "committing"
    CONFIRMING = "confirming"
    DONE = "done"


class OnchainExecutor(ExecutorBase):
    """Stage, simulate and commit one on-chain bundle through the Aomi Pipeline."""

    # The ExecutorService skips connector/market preparation for executors that say so.
    USES_CONNECTORS = False
    _logger = None

    @classmethod
    def logger(cls):
        # RunnableBase logs under hummingbot.strategy_v2.runnable_base, which the API's per-executor
        # log capture (attached to hummingbot.strategy_v2.executors) never sees.
        if cls._logger is None:
            cls._logger = logging.getLogger(LOGGER_NAME)
        return cls._logger

    def __init__(
        self,
        strategy,
        config: OnchainExecutorConfig,
        update_interval: float = 1.0,
        max_retries: int = 10,
        client_factory: Optional[Callable[[], PipelineClient]] = None,
    ):
        super().__init__(
            strategy=strategy, connectors=[], config=config, update_interval=update_interval, max_retries=max_retries
        )
        self.config: OnchainExecutorConfig = config
        self._client_factory = client_factory
        self._client: Optional[PipelineClient] = None
        self._phase = Phase.STAGING
        self._build: Optional[Build] = None
        self._outcome: Optional[CommitOutcome] = None
        self._error: Optional[Dict[str, Any]] = None
        self._started_at: Optional[float] = None
        self._commit_sent = False
        self._stop_requested = False

    # ------------------------------------------------------------------ lifecycle

    @staticmethod
    def _default_client() -> PipelineClient:
        aomi = settings.aomi
        return PipelineClient(aomi.url, aomi.token_provider(), timeout=aomi.timeout)

    async def validate_sufficient_balance(self):
        # The fork simulation proves the wallet can pay; a failing one ends the executor at RISK_CHECK.
        return

    async def on_start(self):
        try:
            if self._client is None:
                if self._client_factory is None and not settings.aomi.configured:
                    raise RuntimeError("Aomi is not configured: set AOMI_TOKEN or AOMI_TOKEN_FILE")
                self._client = (self._client_factory or self._default_client)()
            self._started_at = self._strategy.current_timestamp
            await self.validate_sufficient_balance()
            self.logger().info(
                f"onchain_executor {self.config.id} starting: chain_id={self.config.chain_id} mode={self.config.mode} "
                f"app={self.config.app} operation={self.config.operation}"
            )
        except Exception as exc:
            self._fail("startup", exc)

    def on_stop(self):
        client, self._client = self._client, None
        if client is not None:
            safe_ensure_future(client.close())

    def early_stop(self, keep_position: bool = False):
        if self._commit_sent:
            self.logger().warning(
                f"onchain_executor {self.config.id}: commit already sent (digest {self._digest}); an on-chain commit "
                "cannot be cancelled, letting it finish"
            )
            return
        self._stop_requested = True
        self.close_type = CloseType.EARLY_STOP
        self.logger().info(f"onchain_executor {self.config.id} stopped early during {self._phase.value}")
        self.stop()

    async def control_task(self):
        if self.is_closed or self._phase == Phase.DONE:
            return
        if not self._commit_sent and self._timed_out():
            self._fail("timeout", message=f"no commit within {self.config.timeout_sec}s (phase {self._phase.value})")
            return
        phase = self._phase
        try:
            if phase == Phase.STAGING:
                await self._stage()
            elif phase == Phase.SIMULATING:
                await self._simulate()
            elif phase == Phase.RISK_CHECK:
                self._risk_check()
            elif phase == Phase.COMMITTING:
                await self._commit()
            elif phase == Phase.CONFIRMING:
                self._confirm()
        except PipelineError as err:
            if err.retryable:
                self._current_retries += 1
                self.logger().warning(
                    f"onchain_executor {self.config.id}: {phase.value} failed with a retryable error "
                    f"({err.status} {err.code}: {err.message}); retry {self._current_retries}/{self._max_retries}"
                )
            else:
                self._fail(f"{phase.value}_rejected", err)
        except Exception as exc:
            self._fail("unexpected", exc)

    # ------------------------------------------------------------------ phases

    async def _stage(self):
        cfg = self.config
        if cfg.mode == "lending":
            self._build = await self._client.stage_evm(cfg.lending.calls(), app=cfg.app, skills=cfg.skills)
        elif cfg.mode == "calls":
            self._build = await self._client.stage_evm(cfg.calls, app=cfg.app, skills=cfg.skills)
        elif cfg.mode == "instructions":
            self._build = await self._client.stage_svm(instructions=cfg.instructions, app=cfg.app, skills=cfg.skills)
        else:
            self._build = await self._client.build(
                cfg.chain, app=cfg.app, skills=cfg.skills, operation=cfg.operation_path, arguments=cfg.arguments or {}
            )
        self.logger().info(
            f"onchain_executor {cfg.id}: staged {len(self._build.actions)} action(s), digest {self._build.digest}, "
            f"status {self._build.status}"
        )
        self._phase = Phase.RISK_CHECK if self._build.is_simulated else Phase.SIMULATING

    async def _simulate(self):
        self._build = await self._client.simulate(self._build)
        self._phase = Phase.RISK_CHECK

    def _risk_check(self):
        build = self._build
        simulation = build.simulation if build is not None else None
        if simulation is None or not simulation.passed:
            status = simulation.status if simulation is not None else "missing"
            warnings = simulation.warnings if simulation is not None else []
            self._fail(
                "simulation_failed",
                message=f"simulation {status}" + (f": {'; '.join(warnings)}" if warnings else ""),
                evidence=simulation.raw if simulation is not None else None,
            )
            return
        if self.config.lending is not None:
            try:
                self.config.lending.verify(build)
            except ValueError as exc:
                self._fail("lending_plan_changed", exc)
                return
        if self.config.reviewed_svm_plan_hash is not None:
            if self._svm_plan_hash() != self.config.reviewed_svm_plan_hash:
                self._fail("reviewed_plan_changed", message="Solana execution plan differs from the reviewed preview")
                return
        if self.config.svm_spending_policy is not None:
            try:
                self.config.svm_spending_policy.verify(build, self.config.cluster)
            except ValueError as exc:
                self._fail("spending_policy_refused", exc)
                return
        for warning in simulation.warnings:
            self.logger().warning(f"onchain_executor {self.config.id}: simulation warning: {warning}")
        if self.config.max_svm_network_fee_lamports is not None:
            fee = self._svm_network_fee_lamports()
            if fee is None:
                self._fail("network_fee_unavailable", message="Complete Solana network fee evidence is unavailable")
                return
            if fee > self.config.max_svm_network_fee_lamports:
                self._fail(
                    "network_fee_over_budget",
                    message=f"Simulated network fee {fee} lamports exceeds the "
                            f"{self.config.max_svm_network_fee_lamports}-lamport limit",
                )
                return
        if self.config.max_gas_quote is not None:
            fees = self._estimated_gas_quote()
            if self._fees_are_priced() and fees > self.config.max_gas_quote:
                self._fail(
                    "gas_over_budget",
                    message=f"simulated gas {fees} quote exceeds max_gas_quote {self.config.max_gas_quote}",
                )
                return
            if not self._fees_are_priced():
                self._fail("gas_unpriced", message="Cannot verify max_gas_quote in USDT: gas estimate or price unavailable")
                return
        if not self.config.commit:
            self.logger().info(f"onchain_executor {self.config.id}: dry run, simulation passed, not committing")
            self._finish(CloseType.COMPLETED)
            return
        self._phase = Phase.COMMITTING

    async def _commit(self):
        # Flip before the request leaves so a lost response cannot lead to a second commit; the
        # idempotency key is the Build digest, so a retry replays the ledger entry instead.
        self._commit_sent = True
        self._outcome = await self._client.commit(self._build)
        self._phase = Phase.CONFIRMING

    def _confirm(self):
        outcome = self._outcome
        if outcome is not None and outcome.confirmed:
            self.logger().info(f"onchain_executor {self.config.id}: confirmed {', '.join(outcome.tx_hashes)}")
            self._finish(CloseType.COMPLETED)
            return
        kind = outcome.kind if outcome is not None else "unknown"
        self._error = {
            "reason": "awaiting_wallet",
            "phase": Phase.CONFIRMING.value,
            "outcome_kind": kind,
            "message": f"commit returned {kind}; the bundle needs a wallet signature this executor cannot give",
        }
        self.logger().error(f"onchain_executor {self.config.id}: {self._error['message']}")
        self._finish(CloseType.FAILED)

    def _finish(self, close_type: CloseType):
        self.close_type = close_type
        self._phase = Phase.DONE
        self.stop()

    def _fail(
        self,
        reason: str,
        exc: Optional[BaseException] = None,
        *,
        message: Optional[str] = None,
        evidence: Any = None,
    ):
        error: Dict[str, Any] = {"reason": reason, "phase": self._phase.value}
        if isinstance(exc, PipelineError):
            error.update(
                {
                    "status": exc.status,
                    "code": exc.code,
                    "backend_code": exc.backend_code,
                    "message": exc.message,
                    "request_id": exc.request_id,
                }
            )
        elif exc is not None:
            error["message"] = f"{type(exc).__name__}: {exc}"
        if message:
            error["message"] = message
        if evidence is not None:
            error["evidence"] = evidence
        self._error = error
        self.logger().error(
            f"onchain_executor {self.config.id} failed at {self._phase.value}: {reason}: {error.get('message', '')}",
            exc_info=exc if exc is not None and not isinstance(exc, PipelineError) else None,
        )
        self._finish(CloseType.FAILED)

    def _timed_out(self) -> bool:
        if self._started_at is None:
            return False
        return (self._strategy.current_timestamp - self._started_at) > self.config.timeout_sec

    # ------------------------------------------------------------------ metrics

    def get_net_pnl_quote(self) -> Decimal:
        return Decimal("0")

    def get_net_pnl_pct(self) -> Decimal:
        return Decimal("0")

    @property
    def filled_amount_quote(self) -> Decimal:
        return Decimal("0")

    def get_cum_fees_quote(self) -> Decimal:
        # Pipeline exposes a simulation estimate, not receipt-derived fees.
        # Never book hypothetical gas as incurred trading fees.
        return Decimal("0")

    def _estimated_gas_quote(self) -> Decimal:
        cost = self._gas_native_cost()
        rate = self._quote_rate()
        if cost is None or rate is None:
            return Decimal("0")
        return cost * rate

    def _fees_are_priced(self) -> bool:
        return self._gas_native_cost() is not None and self._quote_rate() is not None

    def _svm_network_fee_lamports(self) -> Optional[int]:
        """Return a complete fee estimate, never a partial sum from missing simulation steps."""
        build = self._build
        simulation = build.simulation if build is not None else None
        if build is None or build.chain != "svm" or simulation is None or not simulation.passed:
            return None
        guards = [g for g in simulation.guards if isinstance(g, dict) and g.get("name") == "svm_network_fees"]
        if len(guards) != 1 or guards[0].get("status") != "passed" or not simulation.fees:
            return None
        wallet = build.from_address
        if not wallet or not build.actions:
            return None
        for action in build.actions:
            if not isinstance(action, dict):
                return None
            inner = action.get("instruction") or action.get("transaction")
            if not isinstance(inner, dict) or inner.get("cluster") != self.config.cluster:
                return None
            if (inner.get("payer") or inner.get("fee_payer") or inner.get("feePayer")) != wallet:
                return None
        total = 0
        for fee in simulation.fees:
            if not isinstance(fee, dict) or (
                fee.get("kind") != "network" or fee.get("asset") != "native"
                or type(fee.get("decimals")) is not int or fee["decimals"] != 9
                or fee.get("cluster") != self.config.cluster or fee.get("account") != wallet
            ):
                return None
            amount = fee.get("amount")
            if not isinstance(amount, str) or not amount.isascii() or not amount.isdecimal() or len(amount) > 20:
                return None
            value = int(amount)
            if value > 2**64 - 1:
                return None
            total += value
        return total

    def _svm_plan_hash(self) -> Optional[str]:
        """Seal an instruction plan across restaging, excluding only queue metadata and labels.

        Unlike the Build digest this is stable when pending IDs and expiry change.
        Include unknown instruction fields conservatively, including fee and assembly
        metadata; a backend extension must never silently broaden reviewed authority.
        """
        build = self._build
        if build is None or build.chain != "svm" or not build.actions:
            return None
        plan = []
        for action in build.actions:
            if not isinstance(action, dict):
                return None
            inner = action.get("instruction")
            if action.get("lane") != "instruction" or not isinstance(inner, dict):
                return None
            if any(not isinstance(inner.get(key), str) or not inner[key] for key in ("payer", "cluster", "program_id")):
                return None
            if not isinstance(inner.get("data_base64"), str) or not isinstance(inner.get("accounts"), list):
                return None
            plan.append({key: value for key, value in inner.items() if key not in {
                "pending_ix_id", "last_batch_status", "current_lifecycle", "description",
            }})
        encoded = json.dumps({"version": 1, "instructions": plan}, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode()).hexdigest()

    def _gas_native_cost(self) -> Optional[Decimal]:
        simulation = self._build.simulation if self._build is not None else None
        gas = simulation.gas if simulation is not None else None
        if gas is None or gas.native_cost is None:
            return None
        try:
            cost = Decimal(str(gas.native_cost))
            return cost if cost.is_finite() and cost >= 0 else None
        except (InvalidOperation, ValueError):
            return None

    def _quote_rate(self) -> Optional[Decimal]:
        """Price of the native gas token in USDT, shared with Condor risk accounting."""
        market_data = getattr(self._strategy, "_market_data_service", None)
        get_rate = getattr(market_data, "get_rate", None)
        if not callable(get_rate):
            return None
        try:
            symbol = "SOL" if self.config.chain == "svm" else native_symbol(self.config.chain_id)
            rate = get_rate(symbol, "USDT")
        except Exception:  # a malformed pair (InvalidTradingPair) or a rate source that throws: unpriced
            return None
        if inspect.isawaitable(rate):
            rate.close()
            return None
        if rate is None:
            return None
        try:
            rate = Decimal(str(rate))
        except (InvalidOperation, ValueError):
            return None
        return rate if rate.is_finite() and rate > 0 else None

    # ------------------------------------------------------------------ reporting

    @property
    def _digest(self) -> Optional[str]:
        return self._build.digest if self._build is not None else None

    @property
    def executor_info(self) -> OnchainExecutorInfo:
        def _safe_decimal(value) -> Decimal:
            d = Decimal(str(value))
            return d if d.is_finite() else Decimal("0")

        return OnchainExecutorInfo(
            id=self.config.id,
            timestamp=self.config.timestamp,
            type=self.config.type,
            status=self.status,
            close_type=self.close_type,
            close_timestamp=self.close_timestamp,
            config=self.config,
            net_pnl_pct=_safe_decimal(self.net_pnl_pct),
            net_pnl_quote=_safe_decimal(self.net_pnl_quote),
            cum_fees_quote=_safe_decimal(self.cum_fees_quote),
            filled_amount_quote=_safe_decimal(self.filled_amount_quote),
            is_active=self.is_active,
            is_trading=self.is_trading,
            custom_info=self.get_custom_info(),
            controller_id=self.config.controller_id,
        )

    def get_custom_info(self) -> Dict[str, Any]:
        cfg = self.config
        build = self._build
        outcome = self._outcome
        simulation = build.simulation if build is not None else None
        gas = simulation.gas if simulation is not None else None
        actions: List[Dict[str, Any]] = build.action_summaries if build is not None else []
        clusters = {action.get("cluster") for action in actions}
        svm_fee = self._svm_network_fee_lamports()
        return {
            "phase": self._phase.value,
            "chain": cfg.chain,
            "chain_id": cfg.chain_id,
            "mode": cfg.mode,
            "app": cfg.app,
            "operation": cfg.operation,
            "wallet_address": build.from_address if build is not None else None,
            "cluster": next(iter(clusters)) if cfg.chain == "svm" and len(clusters) == 1 else None,
            "requested_cluster": cfg.cluster if cfg.chain == "svm" else None,
            "digest": self._digest,
            "svm_plan_hash": self._svm_plan_hash(),
            "svm_spending_policy": cfg.svm_spending_policy.model_dump() if cfg.svm_spending_policy is not None else None,
            "build_expires_at": build.expires_at if build is not None else None,
            "approvals": [dataclasses.asdict(change) for change in simulation.approvals] if simulation is not None else [],
            "action_count": len(actions),
            "actions": actions,
            "simulation_passed": simulation.passed if simulation is not None else None,
            "simulation_warnings": list(simulation.warnings) if simulation is not None else [],
            "simulation_guards": list(simulation.guards) if simulation is not None else [],
            "simulation_fees": list(simulation.fees) if simulation is not None else [],
            "estimated_svm_network_fee_lamports": (
                str(svm_fee) if svm_fee is not None else None
            ),
            "max_svm_network_fee_lamports": (
                str(cfg.max_svm_network_fee_lamports) if cfg.max_svm_network_fee_lamports is not None else None
            ),
            "balance_changes": (
                [dataclasses.asdict(change) for change in simulation.balance_changes] if simulation is not None else []
            ),
            "gas_units": gas.units if gas is not None else None,
            "gas_price_wei": gas.price_wei if gas is not None else None,
            "gas_native_cost": gas.native_cost if gas is not None else None,
            "quote_asset": "USDT",
            "fees_quote_source": "unavailable",
            "estimated_gas_quote": str(self._estimated_gas_quote()) if self._fees_are_priced() else None,
            "committed": bool(outcome is not None and outcome.confirmed),
            "commit_attempted": self._commit_sent,
            "outcome_kind": outcome.kind if outcome is not None else None,
            "tx_hashes": list(outcome.tx_hashes) if outcome is not None else [],
            "tx_ids": list(outcome.tx_ids) if outcome is not None else [],
            "commit_requests": list(outcome.requests) if outcome is not None else [],
            "keep_position": cfg.keep_position,
            "error": self._error,
            "reason": self._error.get("reason") if self._error else None,
        }
