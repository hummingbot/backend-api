"""
ExecutorService manages executor lifecycle and orchestration.
This service enables running Hummingbot executors directly via API
without Docker containers or full strategy setup.
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Type

from fastapi import HTTPException
from hummingbot.strategy_v2.executors.arbitrage_executor.arbitrage_executor import ArbitrageExecutor
from hummingbot.strategy_v2.executors.arbitrage_executor.data_types import ArbitrageExecutorConfig
from hummingbot.strategy_v2.executors.data_types import ExecutorConfigBase
from hummingbot.strategy_v2.executors.dca_executor.data_types import DCAExecutorConfig
from hummingbot.strategy_v2.executors.dca_executor.dca_executor import DCAExecutor
from hummingbot.strategy_v2.executors.executor_base import ExecutorBase
from hummingbot.strategy_v2.executors.grid_executor.data_types import GridExecutorConfig
from hummingbot.strategy_v2.executors.grid_executor.grid_executor import GridExecutor
from hummingbot.strategy_v2.executors.lp_executor.data_types import LPExecutorConfig
from hummingbot.strategy_v2.executors.lp_executor.lp_executor import LPExecutor
from hummingbot.strategy_v2.executors.order_executor.data_types import OrderExecutorConfig
from hummingbot.strategy_v2.executors.order_executor.order_executor import OrderExecutor
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig
from hummingbot.strategy_v2.executors.position_executor.position_executor import PositionExecutor
from hummingbot.strategy_v2.executors.twap_executor.data_types import TWAPExecutorConfig
from hummingbot.strategy_v2.executors.twap_executor.twap_executor import TWAPExecutor
from hummingbot.strategy_v2.executors.xemm_executor.data_types import XEMMExecutorConfig
from hummingbot.strategy_v2.executors.xemm_executor.xemm_executor import XEMMExecutor
from hummingbot.strategy_v2.models.executors import CloseType, TrackedOrder
from sqlalchemy.exc import IntegrityError

from database import AsyncDatabaseManager, ExecutorRepository, GatewayCLMMRepository, GatewaySwapRepository
from models.executors import PositionHold
from services.gateway_client import get_native_gas_token
from services.trading_service import AccountTradingInterface, TradingService
from utils.executor_log_capture import ExecutorLogCapture, current_executor_id
from utils.trading_pair import InvalidTradingPair, split_trading_pair

logger = logging.getLogger(__name__)


def _json_default(obj):
    """JSON serializer for objects not serializable by default."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, Enum):
        return obj.name
    if isinstance(obj, TrackedOrder):
        return {
            "order_id": obj.order_id,
            "price": float(obj.price) if obj.price else None,
            "executed_amount_base": float(obj.executed_amount_base) if obj.executed_amount_base else 0.0,
            "executed_amount_quote": float(obj.executed_amount_quote) if obj.executed_amount_quote else 0.0,
            "is_filled": obj.is_filled if hasattr(obj, 'is_filled') else False,
            "is_open": obj.is_open if hasattr(obj, 'is_open') else False,
        }
    # Handle Pydantic models
    if hasattr(obj, 'model_dump'):
        return obj.model_dump(mode='json')
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _coerce_json_compatible(obj):
    """Recursively coerce a value into JSON-compatible primitives.

    Mirrors the result of ``json.loads(json.dumps(obj, default=_json_default))``
    without the string round-trip: containers are walked recursively and any
    object handled by ``_json_default`` is coerced to the same output type.
    """
    # JSON-native primitives are returned as-is.
    if obj is None or isinstance(obj, (str, bool, int, float)):
        return obj
    if isinstance(obj, dict):
        # json.dumps coerces non-string scalar keys (int/float/bool/None) to
        # strings; replicate that so the output shape is identical.
        coerced = {}
        for key, value in obj.items():
            if isinstance(key, str):
                str_key = key
            elif isinstance(key, bool):
                str_key = "true" if key else "false"
            elif key is None:
                str_key = "null"
            elif isinstance(key, (int, float)):
                str_key = json.dumps(key)
            else:
                raise TypeError(
                    f"keys must be str, int, float, bool or None, not {type(key).__name__}"
                )
            coerced[str_key] = _coerce_json_compatible(value)
        return coerced
    if isinstance(obj, (list, tuple)):
        # json.dumps serializes tuples as JSON arrays (-> lists on decode).
        return [_coerce_json_compatible(item) for item in obj]
    # Non-native types: route through the same coercion as the JSON encoder,
    # then recurse into the (possibly nested) replacement value.
    return _coerce_json_compatible(_json_default(obj))


class ExecutorService:
    """
    Service for managing trading executors without Docker containers.

    This service provides:
    - Dynamic executor creation for any market/connector
    - Executor lifecycle management (start, stop, cleanup)
    - Real-time executor status monitoring
    - Database persistence of executor state and history
    """

    # How long to wait before looking again for a position row that was not there. The
    # poller creates it on discovery, which is far slower than the control loop's tick.
    LP_RENT_RETRY_SECONDS = 30.0

    # Mapping of executor type strings to (executor_class, config_class)
    EXECUTOR_REGISTRY: Dict[str, tuple[Type[ExecutorBase], Type[ExecutorConfigBase]]] = {
        "position_executor": (PositionExecutor, PositionExecutorConfig),
        "grid_executor": (GridExecutor, GridExecutorConfig),
        "dca_executor": (DCAExecutor, DCAExecutorConfig),
        "arbitrage_executor": (ArbitrageExecutor, ArbitrageExecutorConfig),
        "twap_executor": (TWAPExecutor, TWAPExecutorConfig),
        "xemm_executor": (XEMMExecutor, XEMMExecutorConfig),
        "order_executor": (OrderExecutor, OrderExecutorConfig),
        "lp_executor": (LPExecutor, LPExecutorConfig),
    }

    def __init__(
        self,
        trading_service: TradingService,
        db_manager: AsyncDatabaseManager,
        default_account: str = "master_account",
        update_interval: float = 1.0,
        max_retries: int = 10
    ):
        """
        Initialize ExecutorService.

        Args:
            trading_service: TradingService for trading operations and interfaces
            db_manager: AsyncDatabaseManager for persistence
            default_account: Default account to use
            update_interval: Executor update interval in seconds
            max_retries: Maximum retries for executor operations
        """
        self._trading_service = trading_service
        self.db_manager = db_manager
        self.default_account = default_account
        self.update_interval = update_interval
        self.max_retries = max_retries

        # Trading interfaces per account (lazy initialized via TradingService)
        self._trading_interfaces: Dict[str, AccountTradingInterface] = {}

        # Active executors: executor_id -> executor instance
        self._active_executors: Dict[str, ExecutorBase] = {}

        # Executor metadata: executor_id -> metadata dict
        self._executor_metadata: Dict[str, Dict[str, Any]] = {}

        # Position holds: key = "account_name|connector_name|trading_pair"
        # Tracks aggregated positions from executors stopped with keep_position=True
        self._positions_held: Dict[str, PositionHold] = {}

        # Executor log capture
        self._log_capture = ExecutorLogCapture()
        self._log_capture.install()

        # An LP executor's position address, learned while the executor is live. A
        # successful close clears it from custom_info before the executor terminates, so
        # by completion — which is when the rent refund is finally known — there is
        # nothing left to file it under. See _record_lp_position_rent.
        self._lp_position_addresses: Dict[str, str] = {}
        # Executors whose locked rent is already stored, so the control loop stops
        # re-reading and re-writing a figure that cannot change.
        self._lp_rent_recorded: set = set()
        # Earliest monotonic time to retry an executor whose position row did not exist
        # yet. Discovery runs on its own schedule, so retrying at the control loop's 1 Hz
        # would be a query a second against a row that appears about once a minute.
        self._lp_rent_retry_after: Dict[str, float] = {}

        # Control loop task
        self._control_loop_task: Optional[asyncio.Task] = None
        self._is_running = False

    def start(self):
        """Start the executor service control loop."""
        if not self._is_running:
            self._is_running = True
            self._control_loop_task = asyncio.create_task(self._control_loop())
            logger.info("ExecutorService started")

    async def recover_positions_from_db(self):
        """
        Recover position holds from the dedicated position_holds table on startup.
        """
        if not self.db_manager:
            return

        try:
            async with self.db_manager.get_session_context() as session:
                repo = ExecutorRepository(session)

                records = await repo.get_active_position_holds()

                for record in records:
                    controller_id = record.controller_id or "main"
                    position_key = self._get_position_key(
                        record.account_name,
                        record.connector_name,
                        record.trading_pair,
                        controller_id
                    )

                    executor_ids = []
                    if record.executor_ids:
                        try:
                            executor_ids = json.loads(record.executor_ids)
                        except (json.JSONDecodeError, TypeError):
                            pass

                    position = PositionHold(
                        trading_pair=record.trading_pair,
                        connector_name=record.connector_name,
                        account_name=record.account_name,
                        controller_id=controller_id,
                        buy_amount_base=Decimal(str(record.buy_amount_base or 0)),
                        buy_amount_quote=Decimal(str(record.buy_amount_quote or 0)),
                        sell_amount_base=Decimal(str(record.sell_amount_base or 0)),
                        sell_amount_quote=Decimal(str(record.sell_amount_quote or 0)),
                        realized_pnl_quote=Decimal(str(record.realized_pnl_quote or 0)),
                        cum_fees_quote=Decimal(str(record.cum_fees_quote or 0)),
                        executor_ids=executor_ids,
                        last_updated=record.last_updated,
                    )
                    # Settle any matched volume from legacy unsettled data
                    position._calculate_realized_pnl()
                    self._positions_held[position_key] = position

                if self._positions_held:
                    logger.info(f"Recovered {len(self._positions_held)} position holds from database")

        except Exception as e:
            logger.error(f"Error recovering positions from database: {e}", exc_info=True)

    async def cleanup_orphaned_executors(self):
        """
        Clean up orphaned executors from database on startup.

        Identifies executors marked as RUNNING in the database but not present
        in memory (i.e., from previous API sessions that were terminated).
        """
        if not self.db_manager:
            logger.debug("No database manager available, skipping orphaned executor cleanup")
            return

        try:
            # Get list of currently active executor IDs in memory
            active_executor_ids = list(self._active_executors.keys())

            async with self.db_manager.get_session_context() as session:
                repo = ExecutorRepository(session)

                # Clean up orphaned executors
                cleaned_count = await repo.cleanup_orphaned_executors(
                    active_executor_ids=active_executor_ids,
                    close_type="SYSTEM_CLEANUP"
                )

                if cleaned_count > 0:
                    logger.info(f"Cleaned up {cleaned_count} orphaned executors from database")
                else:
                    logger.debug("No orphaned executors found in database")

        except Exception as e:
            logger.error(f"Error cleaning up orphaned executors: {e}", exc_info=True)

    async def stop(self):
        """Stop the executor service and all active executors."""
        self._is_running = False

        if self._control_loop_task:
            self._control_loop_task.cancel()
            try:
                await self._control_loop_task
            except asyncio.CancelledError:
                pass
            self._control_loop_task = None

        # Stop all active executors
        for executor_id in list(self._active_executors.keys()):
            try:
                executor = self._active_executors.get(executor_id)
                if executor:
                    executor.stop()
            except Exception as e:
                logger.error(f"Error stopping executor {executor_id}: {e}")

        # Clear active executors
        self._active_executors.clear()
        self._executor_metadata.clear()

        # Cleanup trading interfaces
        for trading_interface in self._trading_interfaces.values():
            await trading_interface.cleanup()
        self._trading_interfaces.clear()

        logger.info("ExecutorService stopped")

    async def _control_loop(self):
        """Main control loop that updates all active executors."""
        while self._is_running:
            try:
                # Update timestamps for all trading interfaces via TradingService
                self._trading_service.update_all_timestamps()

                # Check for completed executors. Iterate a snapshot: the rent recording
                # below awaits, and create_executor runs in a request task that can add to
                # or remove from _active_executors while this loop is suspended.
                completed_ids = []
                for executor_id, executor in list(self._active_executors.items()):
                    if executor.is_closed:
                        completed_ids.append(executor_id)
                    elif (
                        self._executor_metadata.get(executor_id, {}).get("executor_type") == "lp_executor"
                        and executor_id not in self._lp_rent_recorded
                        and time.monotonic() >= self._lp_rent_retry_after.get(executor_id, 0.0)
                    ):
                        # Locked rent, stored while the position is still held rather than
                        # at completion — a position open for days should be answerable
                        # for the whole time it is open. Drops out of this branch as soon
                        # as it lands, so it is one read per executor, not one per tick.
                        await self._record_lp_position_rent(executor_id, executor)

                # Handle completed executors
                for executor_id in completed_ids:
                    await self._handle_executor_completion(executor_id)

            except Exception as e:
                logger.error(f"Error in executor control loop: {e}", exc_info=True)

            await asyncio.sleep(self.update_interval)

    @staticmethod
    def _measured_rent(custom_info: Dict[str, Any], key: str) -> Optional[Decimal]:
        """A rent figure the executor actually observed, or None.

        The LP executor reports these as plain floats and defaults them to 0.0, so zero
        means "never measured" far more often than it means "measured and empty": a
        position still open has no refund yet, and an EVM CLMM has no rent at all. Rent
        that was genuinely locked is never zero. Storing the 0.0 would put a figure in
        the table that reads as an observation and is not one — the mistake GW-18 made,
        where a hardcoded 0 refund was indistinguishable from a position that earned
        nothing.
        """
        value = custom_info.get(key)
        if value is None:
            return None
        try:
            amount = Decimal(str(value))
        except (ArithmeticError, ValueError):
            return None
        return amount if amount > 0 else None

    async def _record_lp_position_rent(
        self, executor_id: str, executor: ExecutorBase, *, final: bool = False
    ) -> None:
        """Carry an LP executor's rent figures into the CLMM position table.

        ``final`` marks the call made from executor completion, which is the last one
        this executor will ever get: its retry state is torn down immediately after,
        so there is no later tick to hand the work to.

        `gateway_clmm_positions.position_rent` is written by hummingbot-api's OPEN route
        and `position_rent_refunded` by its CLOSE route. An executor holds its position
        through the wheel, talking to Gateway directly, so neither route runs: the poller
        discovers the position and files it with both columns NULL, and the close leaves
        no refund behind either. That is the recommended way to hold a CLMM position and
        the one path whose rent went unrecorded — ~0.0100572 SOL on Orca, more than the
        liquidity in a small position.

        The executor knows both figures; nothing was asking it. Locked rent is written as
        soon as the position row exists, rather than at completion, so a position held for
        days is answerable while it is held. The row may not exist yet on an early tick —
        discovery runs on its own schedule — in which case this is a no-op and the next
        tick tries again.
        """
        try:
            custom_info = executor.get_custom_info()
        except Exception as e:
            logger.debug(f"Could not read custom_info for {executor_id} while recording rent: {e}")
            return

        position_address = custom_info.get("position_address")
        if position_address:
            self._lp_position_addresses[executor_id] = position_address
        else:
            # Cleared by a successful close. The refund is only known now, so fall back to
            # the address this executor was holding.
            position_address = self._lp_position_addresses.get(executor_id)
        if not position_address:
            return

        position_rent = self._measured_rent(custom_info, "position_rent")
        position_rent_refunded = self._measured_rent(custom_info, "position_rent_refunded")
        if position_rent is None and position_rent_refunded is None:
            return

        try:
            async with self.db_manager.get_session_context() as session:
                position = await GatewayCLMMRepository(session).record_position_rent(
                    position_address,
                    position_rent=position_rent,
                    position_rent_refunded=position_rent_refunded,
                )
        except Exception as e:
            logger.error(f"Error recording rent for LP position {position_address}: {e}", exc_info=True)
            return

        if position is None:
            if not final:
                # The poller has simply not discovered the position yet; back off and let a
                # later tick try again. Not worth a warning while the executor still runs.
                self._lp_rent_retry_after[executor_id] = time.monotonic() + self.LP_RENT_RETRY_SECONDS
                return
            # Last call: scheduling a retry here would be theatre, since the caller clears
            # this executor's retry state on the next line. Say plainly what was lost and
            # what it was worth, so the figures can be recovered from the log.
            logger.error(
                f"LP executor {executor_id} finished holding position {position_address} "
                f"(rent={position_rent}, refund={position_rent_refunded}), but no row exists "
                "for it: the position was opened and closed inside a single discovery "
                "sweep, so it was never filed and these figures are not recorded anywhere "
                "but this log line."
            )
            return

        if position_rent is not None:
            self._lp_rent_recorded.add(executor_id)
        logger.debug(
            f"Recorded rent for LP position {position_address}: locked={position_rent}, "
            f"refunded={position_rent_refunded}"
        )

    async def _record_executor_swap(self, executor_id: str, executor: ExecutorBase) -> None:
        """Record a swap an executor made, so the swap history covers the recommended path.

        `gateway_swaps` is written by hummingbot-api's own /gateway/swap/* routes. An
        executor holds its connector through the wheel and talks to Gateway directly, so
        hummingbot-api never sees the call and had nothing to record. The table was not
        wrong, it was silently partial: POST /gateway/swaps/search and the swap summary
        described only swaps made by hand, and a caller reading "9 SOL-USDC swaps" had no
        way to learn that the most recent ones were missing.

        Same shape as _record_lp_position_rent: the executor knows what it did, so ask it
        at completion rather than waiting for a route that will never be called. Keyed on
        the transaction hash, which is what GW-43 added to custom_info — `order_id` is
        internal (buy-SOL-USDC-1787271213996599) and appears nowhere on chain, so before
        that there was nothing to key a row on.

        Skipped silently when there is no hash: a Gateway swap that never reached the
        chain has nothing to record, and an executor on a CEX is not a Gateway swap at all.
        """
        try:
            custom_info = executor.get_custom_info()
        except Exception as e:
            logger.debug(f"Could not read custom_info for {executor_id} while recording its swap: {e}")
            return

        transaction_hash = custom_info.get("transaction_hash")
        swap_provider = custom_info.get("swap_provider")
        if not transaction_hash or not swap_provider:
            return

        metadata = self._executor_metadata.get(executor_id, {})
        network = metadata.get("connector_name") or ""
        trading_pair = metadata.get("trading_pair") or ""
        if not network or "-" not in trading_pair:
            logger.warning(
                f"Executor {executor_id} swapped in {transaction_hash} but reports "
                f"network={network!r} pair={trading_pair!r}; not recorded."
            )
            return

        base_token, quote_token = split_trading_pair(trading_pair)
        side = str(custom_info.get("side") or "").upper()
        side = "BUY" if "BUY" in side else "SELL"

        amount_base = Decimal(str(custom_info.get("executed_amount_base") or 0))
        price = Decimal(str(custom_info.get("average_executed_price") or 0))
        if amount_base <= 0 or price <= 0:
            logger.warning(
                f"Executor {executor_id} swapped in {transaction_hash} but reports no "
                f"realized amounts (base={amount_base}, price={price}); not recorded."
            )
            return
        amount_quote = amount_base * price

        # A BUY spends quote to receive base; a SELL is the mirror image.
        input_amount, output_amount = (
            (amount_quote, amount_base) if side == "BUY" else (amount_base, amount_quote)
        )

        chain = network.split("-", 1)[0]
        # The provider travels as "jupiter/router"; the table stores the bare DEX name,
        # which is what the /gateway/swap routes write.
        connector = swap_provider.split("/", 1)[0]

        try:
            async with self.db_manager.get_session_context() as session:
                repo = GatewaySwapRepository(session)
                if await repo.get_swap_by_tx_hash(transaction_hash):
                    return
                await repo.create_swap({
                    "transaction_hash": transaction_hash,
                    "network": network,
                    "connector": connector,
                    "wallet_address": custom_info.get("wallet_address") or "",
                    "trading_pair": trading_pair,
                    "base_token": base_token,
                    "quote_token": quote_token,
                    "side": side,
                    "input_amount": input_amount,
                    "output_amount": output_amount,
                    "price": price,
                    # The LIVE tolerance the swap went out with, which is not
                    # config.slippage_pct when earlier attempts failed and widened it.
                    "slippage_pct": (Decimal(str(custom_info["slippage_pct"]))
                                     if custom_info.get("slippage_pct") is not None else None),
                    "gas_token": get_native_gas_token(chain),
                    "status": "CONFIRMED",
                })
            logger.info(
                f"Recorded executor swap {transaction_hash}: {side} {amount_base} "
                f"{base_token} @ {price} on {connector}/{network}"
            )
        except Exception as e:
            logger.error(f"Error recording executor swap {transaction_hash}: {e}", exc_info=True)

    def _get_trading_interface(self, account_name: str) -> AccountTradingInterface:
        """Get or create an AccountTradingInterface for the account."""
        if account_name not in self._trading_interfaces:
            self._trading_interfaces[account_name] = self._trading_service.get_trading_interface(account_name)
        return self._trading_interfaces[account_name]

    def _validate_executor_config(
        self,
        executor_config: Dict[str, Any],
        default_timestamp: Optional[float] = None
    ) -> tuple[Type[ExecutorBase], Type[ExecutorConfigBase], ExecutorConfigBase]:
        """
        Validate the executor type and build the typed executor config.

        Pure validation step: no IO, no executor started, no DB access.

        Args:
            executor_config: Executor configuration dictionary (must include 'type')
            default_timestamp: Timestamp to set on the config if not provided
                (required for time-based features like time_limit)

        Returns:
            Tuple of (executor_class, config_class, typed_config)

        Raises:
            HTTPException: 400 if the type is missing/invalid or the config is invalid
        """
        executor_type = executor_config.get("type")
        if not executor_type:
            raise HTTPException(
                status_code=400,
                detail="executor_config must include 'type' field"
            )

        if executor_type not in self.EXECUTOR_REGISTRY:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid executor type '{executor_type}'. Valid types: {list(self.EXECUTOR_REGISTRY.keys())}"
            )

        if "timestamp" not in executor_config or executor_config["timestamp"] is None:
            executor_config["timestamp"] = default_timestamp

        executor_class, config_class = self.EXECUTOR_REGISTRY[executor_type]
        try:
            typed_config = config_class(**executor_config)
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid executor config: {str(e)}"
            )

        return executor_class, config_class, typed_config

    async def _prepare_market(self, account: str, connector_name: Optional[str], trading_pair: Optional[str]):
        """Ensure the connector and market for the executor are ready on the account's trading interface."""
        trading_interface = self._get_trading_interface(account)
        if connector_name:
            if trading_pair:
                await trading_interface.add_market(connector_name, trading_pair)
            else:
                await trading_interface.ensure_connector(connector_name)

    def _instantiate_and_register(
        self,
        executor_class: Type[ExecutorBase],
        typed_config: ExecutorConfigBase,
        trading_interface: AccountTradingInterface,
        metadata: Dict[str, Any]
    ) -> tuple[str, ExecutorBase]:
        """
        Instantiate the executor, register it in memory and start it.

        Args:
            executor_class: Executor class to instantiate
            typed_config: Validated typed executor config
            trading_interface: Trading interface acting as the executor's strategy
            metadata: Metadata dict to register for the executor

        Returns:
            Tuple of (executor_id, executor)

        Raises:
            HTTPException: 400 if the executor fails to instantiate
        """
        try:
            executor = executor_class(
                strategy=trading_interface,
                config=typed_config,
                update_interval=self.update_interval,
                max_retries=self.max_retries
            )
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to create executor: {str(e)}"
            )

        executor_id = typed_config.id
        self._active_executors[executor_id] = executor
        self._executor_metadata[executor_id] = metadata

        # Set ContextVar so the asyncio Task created by start() inherits it
        token = current_executor_id.set(executor_id)
        executor.start()
        current_executor_id.reset(token)

        return executor_id, executor

    async def create_executor(
        self,
        executor_config: Dict[str, Any],
        account_name: Optional[str] = None,
        controller_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Create and start a new executor.

        Args:
            executor_config: Executor configuration dictionary (must include 'type')
            account_name: Account to use (defaults to master_account)

        Returns:
            Dictionary with executor_id and initial status
        """
        account = account_name or self.default_account
        trading_interface = self._get_trading_interface(account)

        # Validate executor type and build the typed config
        executor_class, _config_class, typed_config = self._validate_executor_config(
            executor_config, default_timestamp=trading_interface.current_timestamp
        )
        executor_type = executor_config["type"]

        # Ensure connector and market are ready
        connector_name = executor_config.get("connector_name")
        trading_pair = executor_config.get("trading_pair")
        await self._prepare_market(account, connector_name, trading_pair)

        # Instantiate the executor, register it in memory and start it
        controller_id = controller_id or getattr(typed_config, "controller_id", "main") or "main"
        metadata = {
            "account_name": account,
            "connector_name": connector_name,
            "trading_pair": trading_pair,
            "executor_type": executor_type,
            "controller_id": controller_id,
            "created_at": datetime.now(timezone.utc),
            "config": executor_config
        }
        executor_id, executor = self._instantiate_and_register(executor_class, typed_config, trading_interface, metadata)

        # Persist to database
        await self._persist_executor_created(executor_id, executor)

        # Capture created_at before potential cleanup
        created_at = metadata["created_at"].isoformat()

        # Check if executor terminated immediately (e.g., insufficient balance)
        # If so, handle completion now rather than waiting for control loop
        if executor.is_closed:
            await self._handle_executor_completion(executor_id)

        logger.info(f"Created {executor_type} executor {executor_id} for {connector_name}/{trading_pair}")

        return {
            "executor_id": executor_id,
            "executor_type": executor_type,
            "connector_name": connector_name,
            "trading_pair": trading_pair,
            "controller_id": controller_id,
            "status": executor.status.name,
            "created_at": created_at
        }

    async def get_executors(
        self,
        account_name: Optional[str] = None,
        connector_name: Optional[str] = None,
        trading_pair: Optional[str] = None,
        executor_type: Optional[str] = None,
        status: Optional[str] = None,
        controller_id: Optional[str] = None,
        limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get list of executors with optional filtering.

        Combines active executors from memory with completed executors from database.

        Args:
            account_name: Filter by account name
            connector_name: Filter by connector name
            trading_pair: Filter by trading pair
            executor_type: Filter by executor type
            status: Filter by status
            controller_id: Filter by controller ID

        Returns:
            List of executor information dictionaries
        """
        result = []

        # Process active executors from memory
        for executor_id, executor in self._active_executors.items():
            metadata = self._executor_metadata.get(executor_id, {})

            # Apply filters
            if account_name and metadata.get("account_name") != account_name:
                continue
            if connector_name and metadata.get("connector_name") != connector_name:
                continue
            if trading_pair and metadata.get("trading_pair") != trading_pair:
                continue
            if executor_type and metadata.get("executor_type") != executor_type:
                continue
            if status and executor.status.name != status:
                continue
            if controller_id and metadata.get("controller_id", "main") != controller_id:
                continue

            result.append(self._format_executor_info(executor_id, executor))

        # Get completed executors from database
        if self.db_manager:
            try:
                async with self.db_manager.get_session_context() as session:
                    repo = ExecutorRepository(session)

                    db_executors = await repo.get_executors(
                        account_name=account_name,
                        connector_name=connector_name,
                        trading_pair=trading_pair,
                        executor_type=executor_type,
                        status=status,
                        controller_id=controller_id,
                        limit=limit
                    )

                    for record in db_executors:
                        # Skip if already in active executors (safety check)
                        if record.executor_id not in self._active_executors:
                            result.append(self._format_db_record(record))
            except Exception as e:
                logger.error(f"Error fetching executors from database: {e}")

        return result

    async def get_executor(self, executor_id: str) -> Optional[Dict[str, Any]]:
        """
        Get detailed information about a specific executor.

        Checks active executors in memory first, then falls back to database.

        Args:
            executor_id: The executor ID

        Returns:
            Detailed executor information or None if not found
        """
        # Check active executors first (memory)
        executor = self._active_executors.get(executor_id)
        if executor:
            return self._format_executor_info(executor_id, executor)

        # Fallback to database for completed executors
        if self.db_manager:
            try:
                async with self.db_manager.get_session_context() as session:
                    repo = ExecutorRepository(session)

                    record = await repo.get_executor_by_id(executor_id)
                    if record:
                        return self._format_db_record(record)
            except Exception as e:
                logger.error(f"Error fetching executor from database: {e}")

        return None

    def get_executor_logs(
        self,
        executor_id: str,
        level: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[dict]:
        """
        Get captured log entries for an executor.

        Only available for active executors (logs are cleared on completion).

        Args:
            executor_id: The executor ID
            level: Optional filter by level (ERROR, WARNING, INFO, DEBUG)
            limit: Maximum number of entries to return

        Returns:
            List of log entry dicts
        """
        return self._log_capture.get_logs(executor_id, level=level, limit=limit)

    async def stop_executor(
        self,
        executor_id: str,
        keep_position: bool = False
    ) -> Dict[str, Any]:
        """
        Stop an active executor.

        Args:
            executor_id: The executor ID to stop
            keep_position: Whether to keep the position open

        Returns:
            Dictionary with stop confirmation
        """
        executor = self._active_executors.get(executor_id)
        if not executor:
            # Terminal executors are popped from memory within one control-loop tick,
            # so "not in memory" usually means "already terminated", not "unknown".
            # Fall back to the DB and answer with the final state as a no-op success.
            # This deliberately includes rows still marked RUNNING in the DB: an
            # executor known to the DB but absent from memory is dead regardless of
            # its stored status (completion race, failed persist, restart window),
            # and answering 404 there is the gateway#678 dead end. 404 is reserved
            # for executor ids the DB has never seen (or a DB outage, which
            # get_executor logs and swallows to None).
            db_record = await self.get_executor(executor_id)
            if db_record:
                custom_info = db_record.get("custom_info") or {}
                logger.info(
                    f"Stop requested for already-terminated executor {executor_id} "
                    f"(db status: {db_record.get('status')}, close_type: {db_record.get('close_type')}) - no-op"
                )
                return {
                    "executor_id": executor_id,
                    "status": "already_terminated",
                    "keep_position": keep_position,
                    "close_type": db_record.get("close_type"),
                    "position_address": custom_info.get("position_address"),
                    "orphaned_position": bool(custom_info.get("orphaned_position", False)),
                    "hold_reason": custom_info.get("hold_reason"),
                }
            raise HTTPException(status_code=404, detail=f"Executor {executor_id} not found")

        if executor.is_closed:
            raise HTTPException(status_code=400, detail=f"Executor {executor_id} is already closed")

        # Trigger early stop
        try:
            executor.early_stop(keep_position=keep_position)
        except Exception as e:
            logger.error(f"Error stopping executor {executor_id}: {e}")
            raise HTTPException(status_code=500, detail=f"Error stopping executor: {str(e)}")

        logger.info(f"Initiated stop for executor {executor_id} (keep_position={keep_position})")

        return {
            "executor_id": executor_id,
            "status": "stopping",
            "keep_position": keep_position
        }

    async def get_orphaned_positions(self) -> List[Dict[str, Any]]:
        """
        List executors that terminated while potentially still owning an on-chain position.

        Covers both orphan classes:
        - close_type FAILED with a position_address in the persisted final state
          (e.g. an LP close that exhausted retries - gateway#678)
        - close_type SYSTEM_CLEANUP (RUNNING rows rewritten after an API restart);
          these have no final state, so the position address is unknown and the
          on-chain state must be reconciled externally

        This listing is DB-side only: cross-check candidates against on-chain reality
        (gateway CLMM/AMM positions-owned endpoints) before recovering. Recovered
        orphans are silenced with resolve_orphaned_position().

        Raises on DB errors rather than returning [] - "no orphans" from a broken DB
        would read as all-clear on a safety endpoint.
        """
        if not self.db_manager:
            raise RuntimeError("Orphan listing unavailable: no database configured")

        # lp_executor is the only executor type that owns an on-chain position
        # account; filtering in SQL keeps the limit meaningful (a Python-side filter
        # over the newest N mixed candidates can silently drop older real orphans).
        async with self.db_manager.get_session_context() as session:
            repo = ExecutorRepository(session)
            records = await repo.get_executors_by_close_types(
                ["FAILED", "SYSTEM_CLEANUP", "POSITION_HOLD"], executor_type="lp_executor"
            )

        orphans: List[Dict[str, Any]] = []
        for record in records:
            final_state: Dict[str, Any] = {}
            if record.final_state:
                try:
                    final_state = json.loads(record.final_state)
                except (json.JSONDecodeError, TypeError):
                    final_state = {}

            if final_state.get("orphan_resolved"):
                continue

            position_address = final_state.get("position_address")
            if record.close_type == "FAILED" and not position_address:
                # Failed without on-chain exposure - not an orphan
                continue
            if record.close_type == "POSITION_HOLD" and not (
                final_state.get("orphaned_position") or final_state.get("hold_reason")
            ):
                # Voluntary hold (keep_position=True stop) - position was closed on-chain
                continue

            # The DEX and pool live in the executor config, not in any column: for lp_executor the
            # connector_name column carries the network id. Both are needed to close the position,
            # and LP-executor positions are opened by the bot straight against Gateway so they are
            # never in the API's own CLMM position table to be looked up there.
            config: Dict[str, Any] = {}
            if record.config:
                try:
                    config = json.loads(record.config)
                except (json.JSONDecodeError, TypeError):
                    config = {}

            orphans.append({
                "executor_id": record.executor_id,
                "executor_type": record.executor_type,
                "account_name": record.account_name,
                "connector_name": record.connector_name,
                "trading_pair": record.trading_pair,
                "lp_provider": config.get("lp_provider"),
                "pool_address": config.get("pool_address"),
                "controller_id": record.controller_id or "main",
                "close_type": record.close_type,
                "closed_at": record.closed_at.isoformat() if record.closed_at else None,
                "position_address": position_address,
                "state": final_state.get("state"),
                "hold_reason": final_state.get("hold_reason"),
                "needs_onchain_reconciliation": position_address is None,
            })

        return orphans

    async def resolve_orphaned_position(self, executor_id: str) -> Dict[str, Any]:
        """
        Mark an orphaned position as recovered so it stops surfacing.

        Call this after the stranded on-chain position has been closed (or adopted)
        externally. Sets orphan_resolved in the persisted final state, which removes
        the record from get_orphaned_positions() and from agent-facing warnings.
        """
        if not self.db_manager:
            raise HTTPException(status_code=503, detail="No database configured")

        async with self.db_manager.get_session_context() as session:
            repo = ExecutorRepository(session)
            record = await repo.get_executor_by_id(executor_id)
            if not record:
                raise HTTPException(status_code=404, detail=f"Executor {executor_id} not found")
            if record.status == "RUNNING":
                raise HTTPException(status_code=400, detail=f"Executor {executor_id} is still running")

            final_state: Dict[str, Any] = {}
            if record.final_state:
                try:
                    final_state = json.loads(record.final_state)
                except (json.JSONDecodeError, TypeError):
                    final_state = {}

            is_involuntary_hold = record.close_type == "POSITION_HOLD" and (
                final_state.get("orphaned_position") or final_state.get("hold_reason")
            )
            if record.close_type not in ("FAILED", "SYSTEM_CLEANUP") and not is_involuntary_hold:
                raise HTTPException(
                    status_code=400,
                    detail=f"Executor {executor_id} (close_type: {record.close_type}) is not an orphan candidate",
                )
            final_state["orphaned_position"] = False
            final_state["orphan_resolved"] = True
            await repo.update_executor(executor_id=executor_id, final_state=json.dumps(final_state))

        logger.info(f"Orphaned position for executor {executor_id} marked resolved")
        return {"executor_id": executor_id, "orphan_resolved": True}

    async def _handle_executor_completion(self, executor_id: str):
        """Handle cleanup when an executor completes."""
        # Atomically claim the executor so a concurrent completion (e.g. the
        # control loop racing with the synchronous call in create_executor)
        # returns early instead of double-persisting / double-aggregating.
        executor = self._active_executors.pop(executor_id, None)
        if executor is None:
            return

        metadata = self._executor_metadata.get(executor_id, {})

        # Check if this is a POSITION_HOLD close type (keep_position=True)
        if executor.close_type == CloseType.POSITION_HOLD:
            await self._aggregate_position_hold(executor_id, executor, metadata)

        # Persist final state to database
        await self._persist_executor_completed(executor_id, executor)

        # The rent refund is only known once the close confirms, which is here. A
        # successful close has already cleared position_address from custom_info, so this
        # relies on the address remembered while the executor was live.
        if metadata.get("executor_type") == "lp_executor":
            await self._record_lp_position_rent(executor_id, executor, final=True)
        # A Gateway swap an executor made, which no /gateway/swap route ever saw. See
        # _record_executor_swap; non-Gateway executors carry no transaction hash and fall
        # straight back out.
        if self.db_manager:
            await self._record_executor_swap(executor_id, executor)
        self._lp_position_addresses.pop(executor_id, None)
        self._lp_rent_recorded.discard(executor_id)
        self._lp_rent_retry_after.pop(executor_id, None)

        # Active executor already claimed via pop above; drop its metadata last
        # (metadata is read above and re-fetched inside the persist/aggregate
        # helpers, so it must stay until after those awaits complete).
        if executor_id in self._executor_metadata:
            del self._executor_metadata[executor_id]

        # Clean up captured logs
        self._log_capture.clear(executor_id)

        close_type = executor.close_type.name if executor.close_type else "UNKNOWN"
        logger.info(f"Executor {executor_id} completed with close_type: {close_type}")

        # Surface stranded on-chain exposure loudly: a live position address on an
        # involuntary hold (hold_reason set) or a legacy FAILED means the position
        # has no automated owner from this point on.
        if executor.close_type in (CloseType.FAILED, CloseType.POSITION_HOLD):
            try:
                completion_info = executor.get_custom_info()
                position_address = completion_info.get("position_address")
                hold_reason = completion_info.get("hold_reason")
            except Exception:
                position_address = None
                hold_reason = None
            if position_address and (hold_reason or executor.close_type == CloseType.FAILED):
                logger.error(
                    f"Executor {executor_id} ended {close_type} with position {position_address} "
                    f"still open on-chain (hold_reason: {hold_reason}) - orphaned position requires "
                    "recovery (flagged in DB record; see /executors/positions/orphaned)"
                )

    def _format_executor_info(
        self,
        executor_id: str,
        executor: ExecutorBase
    ) -> Dict[str, Any]:
        """Format executor information for API response."""
        metadata = self._executor_metadata.get(executor_id, {})
        executor_type = metadata.get("executor_type")

        # Get executor_info as a dict and strip heavy custom_info fields BEFORE
        # serialization so they never get coerced (fill_events, grid
        # levels_by_state, etc.); then coerce in-place to JSON-compatible
        # primitives instead of doing a json.dumps/json.loads string round-trip.
        executor_info = executor.executor_info
        dumped = executor_info.model_dump()
        dumped["custom_info"] = self._strip_heavy_fields(dumped.get("custom_info"), executor_type)
        result = _coerce_json_compatible(dumped)

        # Add metadata
        result["executor_id"] = executor_id
        result["executor_type"] = executor_type
        result["account_name"] = metadata.get("account_name")
        result["created_at"] = metadata.get("created_at").isoformat() if metadata.get("created_at") else None

        if metadata.get("connector_name"):
            result["connector_name"] = metadata.get("connector_name")
        if metadata.get("trading_pair"):
            result["trading_pair"] = metadata.get("trading_pair")
        result["controller_id"] = metadata.get("controller_id", "main")

        # Read status/close_type directly from executor
        result["status"] = executor.status.name
        result["close_type"] = executor.close_type.name if executor.close_type else None
        result["is_active"] = not executor.is_closed

        # Add side from executor_info (it's a property, not serialized by model_dump)
        side = executor_info.side
        if side is not None:
            # Convert TradeType enum or int to string
            result["side"] = side.name if hasattr(side, 'name') else str(side)

        # Add log capture info
        result["error_count"] = self._log_capture.get_error_count(executor_id)
        result["last_error"] = self._log_capture.get_last_error(executor_id)

        return result

    @staticmethod
    def _strip_heavy_fields(custom_info: Optional[Dict], executor_type: Optional[str] = None) -> Optional[Dict]:
        """Remove heavy fields from custom_info to reduce payload size."""
        if not custom_info:
            return custom_info
        heavy_fields = {"fill_events"}
        if executor_type == "grid_executor":
            heavy_fields |= {"levels_by_state", "filled_orders", "failed_orders", "canceled_orders"}
        return {k: v for k, v in custom_info.items() if k not in heavy_fields}

    def _format_db_record(self, record) -> Dict[str, Any]:
        """Format a database ExecutorRecord for API response."""
        # Parse error_log from DB for completed executors
        error_count = 0
        last_error = None
        if record.error_log:
            try:
                errors = json.loads(record.error_log)
                error_count = len(errors)
                if errors:
                    last_error = errors[-1].get("message")
            except (json.JSONDecodeError, TypeError):
                pass

        return {
            "executor_id": record.executor_id,
            "executor_type": record.executor_type,
            "account_name": record.account_name,
            "connector_name": record.connector_name,
            "trading_pair": record.trading_pair,
            "side": None,
            "status": record.status,
            "close_type": record.close_type,
            "is_active": record.status == "RUNNING",
            "is_trading": False,
            "timestamp": None,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "close_timestamp": record.closed_at.timestamp() if record.closed_at else None,
            "closed_at": record.closed_at.isoformat() if record.closed_at else None,
            "controller_id": record.controller_id or "main",
            "net_pnl_quote": float(record.net_pnl_quote) if record.net_pnl_quote else 0.0,
            "net_pnl_pct": float(record.net_pnl_pct) if record.net_pnl_pct else 0.0,
            "cum_fees_quote": float(record.cum_fees_quote) if record.cum_fees_quote else 0.0,
            "filled_amount_quote": float(record.filled_amount_quote) if record.filled_amount_quote else 0.0,
            "config": json.loads(record.config) if record.config else None,
            "custom_info": self._strip_heavy_fields(
                json.loads(record.final_state), record.executor_type
            ) if record.final_state else None,
            "error_count": error_count,
            "last_error": last_error,
        }

    def get_summary(self) -> Dict[str, Any]:
        """
        Get summary statistics for active executors.

        Returns:
            Dictionary with aggregate statistics for active executors only.
        """
        executors = []

        # Get active executors from memory
        for executor_id, executor in self._active_executors.items():
            executors.append(self._format_executor_info(executor_id, executor))

        active_count = len(executors)
        total_pnl = sum(e.get("net_pnl_quote", 0) for e in executors)
        # filled_amount_quote is the volume traded on every executor type — an LP
        # executor derives it from the fees it earned rather than the capital it put up,
        # so this sums like with like and no separate field is needed.
        total_volume = sum(e.get("filled_amount_quote", 0) for e in executors)

        by_type: Dict[str, int] = {}
        by_connector: Dict[str, int] = {}
        by_status: Dict[str, int] = {}

        for e in executors:
            ex_type = e.get("executor_type", "unknown")
            connector = e.get("connector_name", "unknown")
            status = e.get("status", "unknown")

            by_type[ex_type] = by_type.get(ex_type, 0) + 1
            by_connector[connector] = by_connector.get(connector, 0) + 1
            by_status[status] = by_status.get(status, 0) + 1

        return {
            "total_active": active_count,
            "total_pnl_quote": total_pnl,
            "total_volume_quote": total_volume,
            "by_type": by_type,
            "by_connector": by_connector,
            "by_status": by_status
        }

    async def get_performance_report(
        self,
        controller_id: Optional[str] = None,
        market_data_service=None
    ) -> Dict[str, Any]:
        """
        Generate a performance report aggregating executor metrics.

        Combines database aggregations (completed executors) with in-memory
        active executor and position hold unrealized PnL.
        Excludes POSITION_HOLD close_type from realized PnL to avoid double-counting.

        Args:
            controller_id: Filter by controller ID (None = all)
            market_data_service: MarketDataService for position hold unrealized PnL

        Returns:
            Dictionary with performance metrics ready for PerformanceReportResponse.
        """
        import math

        report: Dict[str, Any] = {
            "controller_id": controller_id,
            "total_executors": 0,
            "by_status": {},
            "pnl_total_quote": 0.0,
            "unrealized_pnl_quote": 0.0,
            "global_pnl_quote": 0.0,
            "pnl_pct_avg": 0.0,
            "fees_total_quote": 0.0,
            "volume_total_quote": 0.0,
            "win_rate": 0.0,
            "sharpe_ratio": None,
            "by_type": [],
            "active_positions": 0,
        }

        if self.db_manager:
            try:
                async with self.db_manager.get_session_context() as session:
                    repo = ExecutorRepository(session)
                    db_data = await repo.get_performance_report(controller_id=controller_id)

                report["total_executors"] = db_data["total_executors"]
                report["by_status"] = db_data["status_counts"]
                report["pnl_total_quote"] = db_data["pnl_total_quote"]
                report["pnl_pct_avg"] = db_data["pnl_pct_avg"]
                report["fees_total_quote"] = db_data["fees_total_quote"]
                report["volume_total_quote"] = db_data["volume_total_quote"]
                report["win_rate"] = db_data["win_rate"]
                report["by_type"] = db_data["by_type"]

                # Sharpe ratio: mean(pnl) / std(pnl), requires >= 2 values
                pnl_values = db_data.get("pnl_values", [])
                if len(pnl_values) >= 2:
                    mean_pnl = sum(pnl_values) / len(pnl_values)
                    variance = sum((v - mean_pnl) ** 2 for v in pnl_values) / (len(pnl_values) - 1)
                    std_pnl = math.sqrt(variance)
                    if std_pnl > 0:
                        report["sharpe_ratio"] = round(mean_pnl / std_pnl, 4)

            except Exception as e:
                logger.error(f"Error generating performance report: {e}", exc_info=True)

        # --- Unrealized PnL from active executors ---
        unrealized_pnl = 0.0
        for executor_id, executor in self._active_executors.items():
            metadata = self._executor_metadata.get(executor_id, {})
            if controller_id and metadata.get("controller_id", "main") != controller_id:
                continue
            try:
                unrealized_pnl += float(executor.executor_info.net_pnl_quote)
            except Exception:
                pass

        # --- Unrealized PnL from position holds ---
        positions = self.get_positions_held(controller_id=controller_id)
        report["active_positions"] = len(positions)

        # Accumulate fees from position holds (already paid, reduce PnL)
        position_hold_fees = sum(float(p.cum_fees_quote) for p in positions)

        if market_data_service:
            # First pass: try oracle for each position, collect misses grouped by connector
            missing_by_connector: Dict[str, List[tuple]] = {}  # connector_key -> [(position, trading_pair)]
            for p in positions:
                # A hyphenated base symbol produced three parts and was skipped here,
                # so the position simply contributed nothing to unrealized PnL.
                try:
                    base, quote = split_trading_pair(p.trading_pair)
                except InvalidTradingPair:
                    continue
                rate = market_data_service.get_rate(base, quote)
                if rate is not None:
                    unrealized_pnl += float(p.get_unrealized_pnl(rate))
                else:
                    # Group by connector+account for batch fallback
                    connector_key = f"{p.connector_name}|{p.account_name}"
                    missing_by_connector.setdefault(connector_key, []).append((p, p.trading_pair))

            # Second pass: batch-fetch missing prices from the actual connectors
            for connector_key, items in missing_by_connector.items():
                connector_name, account_name = connector_key.split("|", 1)
                trading_pairs = [tp for _, tp in items]
                try:
                    prices = await market_data_service.get_prices(
                        connector_name=connector_name,
                        trading_pairs=trading_pairs,
                        account_name=account_name,
                    )
                    if isinstance(prices, dict) and "error" not in prices:
                        for pos, tp in items:
                            price = prices.get(tp)
                            if price is not None and price > 0:
                                unrealized_pnl += float(pos.get_unrealized_pnl(Decimal(str(price))))
                except Exception as e:
                    logger.warning(f"Fallback price fetch failed for {connector_name}: {e}")

        # Subtract position hold fees from unrealized PnL
        unrealized_pnl -= position_hold_fees

        report["unrealized_pnl_quote"] = round(unrealized_pnl, 8)
        report["position_hold_fees_quote"] = round(position_hold_fees, 8)
        report["global_pnl_quote"] = round(report["pnl_total_quote"] + unrealized_pnl, 8)

        return report

    async def _persist_executor_created(self, executor_id: str, executor: ExecutorBase):
        """Persist executor creation to database."""
        if not self.db_manager:
            return

        try:
            metadata = self._executor_metadata.get(executor_id, {})

            async with self.db_manager.get_session_context() as session:
                repo = ExecutorRepository(session)

                await repo.create_executor(
                    executor_id=executor_id,
                    executor_type=metadata.get("executor_type"),
                    account_name=metadata.get("account_name"),
                    connector_name=metadata.get("connector_name"),
                    trading_pair=metadata.get("trading_pair"),
                    config=json.dumps(metadata.get("config", {}), default=_json_default),
                    status=executor.status.name,
                    controller_id=metadata.get("controller_id", "main")
                )

            logger.debug(f"Persisted executor {executor_id} creation to database")

        except IntegrityError:
            # The executor closed before this INSERT landed and its completion already
            # wrote the row (see upsert_executor_completion). That row carries the final
            # state; re-inserting a RUNNING one is exactly what must not happen.
            logger.debug(
                f"Executor {executor_id} row already written by its completion; "
                f"skipping the creation insert"
            )
        except Exception as e:
            logger.error(f"Error persisting executor creation: {e}")

    async def _persist_executor_completed(self, executor_id: str, executor: ExecutorBase):
        """Persist executor completion to database."""
        if not self.db_manager:
            return

        try:
            # Read status/close_type directly from executor (most reliable)
            status_name = executor.status.name
            close_type = executor.close_type.name if executor.close_type else None

            # Get PnL values from executor_info
            try:
                executor_info = executor.executor_info
                net_pnl_quote = executor_info.net_pnl_quote
                net_pnl_pct = executor_info.net_pnl_pct
                cum_fees_quote = executor_info.cum_fees_quote
                filled_amount_quote = executor_info.filled_amount_quote
            except Exception as e:
                logger.debug(f"Error accessing executor_info for persistence: {e}")
                net_pnl_quote = Decimal("0")
                net_pnl_pct = Decimal("0")
                cum_fees_quote = Decimal("0")
                filled_amount_quote = Decimal("0")

            # Get custom_info directly from executor to avoid Pydantic serialization issues
            # with TrackedOrder and other complex types
            custom_info = executor.get_custom_info()

            # A stranded live on-chain position: an involuntary hold (close retries
            # exhausted -> POSITION_HOLD with hold_reason set, gateway#678) or a legacy
            # FAILED-with-position (force-stop straggler, older wheel). Flag it in the
            # persisted final_state so /executors/positions/orphaned, dashboards, and
            # agents can find and recover it. Voluntary holds never match: a successful
            # close clears position_address before the executor terminates.
            if custom_info.get("position_address") and (
                custom_info.get("hold_reason") or close_type == "FAILED"
            ):
                custom_info["orphaned_position"] = True
            # Serialize custom_info, fallback to None if serialization fails
            final_state_json = None
            metadata = self._executor_metadata.get(executor_id, {})
            executor_type = metadata.get("executor_type")
            if executor_type == "grid_executor":
                heavy_fields = {
                    "levels_by_state",
                    "filled_orders",
                    "failed_orders",
                    "canceled_orders",
                }
                custom_info = {k: v for k, v in custom_info.items() if k not in heavy_fields}

            try:
                final_state_json = json.dumps(custom_info, default=_json_default)
            except Exception as e:
                logger.warning(f"Failed to serialize custom_info for {executor_id}: {e}")
                # Try a simpler serialization without complex objects
                try:
                    simple_info = {k: v for k, v in custom_info.items()
                                   if isinstance(v, (str, int, float, bool, list, dict, type(None)))}
                    final_state_json = json.dumps(simple_info)
                except Exception:
                    final_state_json = None

            # Capture error logs before persisting
            error_log_json = None
            error_count = self._log_capture.get_error_count(executor_id)
            if error_count > 0:
                try:
                    error_entries = self._log_capture.get_logs(executor_id, level="ERROR")
                    error_log_json = json.dumps([
                        {
                            "timestamp": entry.get("timestamp"),
                            "message": entry.get("message"),
                            "exc_info": entry.get("exc_info"),
                        }
                        for entry in error_entries
                    ])
                except Exception as e:
                    logger.debug(f"Failed to serialize error logs for {executor_id}: {e}")

            async with self.db_manager.get_session_context() as session:
                repo = ExecutorRepository(session)

                # Upsert, not update: an executor that closes in milliseconds can reach
                # here before _persist_executor_created's INSERT has landed (or after it
                # failed outright), and a plain select-then-update would silently drop
                # this final state, leaving a phantom RUNNING executor forever.
                record, repaired = await repo.upsert_executor_completion(
                    executor_id=executor_id,
                    executor_type=executor_type,
                    account_name=metadata.get("account_name"),
                    connector_name=metadata.get("connector_name"),
                    trading_pair=metadata.get("trading_pair"),
                    controller_id=metadata.get("controller_id", "main"),
                    config=json.dumps(metadata.get("config", {}), default=_json_default),
                    created_at=metadata.get("created_at"),
                    status=status_name,
                    close_type=close_type,
                    net_pnl_quote=net_pnl_quote,
                    net_pnl_pct=net_pnl_pct,
                    cum_fees_quote=cum_fees_quote,
                    filled_amount_quote=filled_amount_quote,
                    final_state=final_state_json,
                    error_log=error_log_json
                )

            if record is None:
                logger.error(
                    f"Could not persist completion for executor {executor_id}: no row to "
                    f"update and the repair insert did not take"
                )
            elif repaired:
                logger.warning(
                    f"Executor {executor_id} completed before its creation row existed; "
                    f"inserted the record from its final state"
                )
            else:
                logger.debug(f"Persisted executor {executor_id} completion to database")

        except Exception as e:
            logger.error(f"Error persisting executor completion: {e}")

    # ========================================
    # Position Hold Tracking Methods
    # ========================================

    def _get_position_key(
        self,
        account_name: str,
        connector_name: str,
        trading_pair: str,
        controller_id: str = "main"
    ) -> str:
        """Generate a unique key for position tracking."""
        return f"{account_name}|{connector_name}|{trading_pair}|{controller_id}"

    async def _aggregate_position_hold(
        self,
        executor_id: str,
        executor: ExecutorBase,
        metadata: Dict[str, Any]
    ):
        """
        Aggregate position data from an executor stopped with keep_position=True.

        This extracts the filled amounts from the executor and adds them to
        the aggregated position tracking.
        """
        account_name = metadata.get("account_name", self.default_account)
        connector_name = metadata.get("connector_name", "")
        trading_pair = metadata.get("trading_pair", "")
        controller_id = metadata.get("controller_id", "main")

        if not connector_name or not trading_pair:
            logger.warning(f"Cannot aggregate position for executor {executor_id}: missing connector/pair info")
            return

        position_key = self._get_position_key(account_name, connector_name, trading_pair, controller_id)

        # Get or create position hold
        if position_key not in self._positions_held:
            self._positions_held[position_key] = PositionHold(
                trading_pair=trading_pair,
                connector_name=connector_name,
                account_name=account_name,
                controller_id=controller_id
            )

        position = self._positions_held[position_key]

        # Extract filled amounts from executor
        try:
            # Try to get executor info
            try:
                executor_info = executor.executor_info
                custom_info = executor_info.custom_info or {}
            except Exception:
                custom_info = executor.get_custom_info() if hasattr(executor, 'get_custom_info') else {}

            # Get side from config or custom_info
            config = metadata.get("config", {})
            side = config.get("side", custom_info.get("side", "BUY"))

            # Extract filled amounts - try different sources
            filled_amount_base = Decimal("0")
            filled_amount_quote = Decimal("0")

            # Try from executor attributes directly
            if hasattr(executor, 'filled_amount_base'):
                filled_amount_base = Decimal(str(executor.filled_amount_base or 0))
            if hasattr(executor, 'filled_amount_quote'):
                filled_amount_quote = Decimal(str(executor.filled_amount_quote or 0))

            # Fallback to custom_info
            if filled_amount_base == 0 and custom_info:
                filled_amount_base = Decimal(str(custom_info.get("filled_amount_base", 0)))
            if filled_amount_quote == 0 and custom_info:
                filled_amount_quote = Decimal(str(custom_info.get("filled_amount_quote", 0)))

            # Check for held_position_orders (used by grid_executor, position_executor, etc.)
            held_orders = custom_info.get("held_position_orders", []) if custom_info else []

            # Extract cumulative fees from the executor
            executor_fees = Decimal("0")
            try:
                executor_fees = Decimal(str(executor.cum_fees_quote or 0))
            except Exception:
                pass

            if held_orders:
                buy_filled_base = Decimal("0")
                buy_filled_quote = Decimal("0")
                sell_filled_base = Decimal("0")
                sell_filled_quote = Decimal("0")
                orders_fees = Decimal("0")

                for order in held_orders:
                    if isinstance(order, dict):
                        trade_type = order.get("trade_type", "BUY")
                        exec_base = Decimal(str(order.get("executed_amount_base", 0)))
                        exec_quote = Decimal(str(order.get("executed_amount_quote", 0)))
                        orders_fees += Decimal(str(order.get("cumulative_fee_paid_quote", 0)))

                        if trade_type == "BUY":
                            buy_filled_base += exec_base
                            buy_filled_quote += exec_quote
                        else:
                            sell_filled_base += exec_base
                            sell_filled_quote += exec_quote

                # Use order-level fees if available, otherwise fall back to executor-level
                fees = orders_fees if orders_fees > 0 else executor_fees

                # Add buy and sell fills separately
                if buy_filled_base > 0:
                    # Split fees proportionally between buy and sell by quote volume
                    total_quote = buy_filled_quote + sell_filled_quote
                    buy_fee_share = fees * (buy_filled_quote / total_quote) if total_quote > 0 else fees
                    position.add_fill("BUY", buy_filled_base, buy_filled_quote, executor_id, fees_quote=buy_fee_share)
                if sell_filled_base > 0:
                    total_quote = buy_filled_quote + sell_filled_quote
                    sell_fee_share = fees * (sell_filled_quote / total_quote) if total_quote > 0 else fees
                    position.add_fill("SELL", sell_filled_base, sell_filled_quote, executor_id, fees_quote=sell_fee_share)

                logger.info(
                    f"Aggregated executor {executor_id} to position {position_key}: "
                    f"buy={buy_filled_base} base, sell={sell_filled_base} base, fees={fees} quote"
                )

            elif filled_amount_base > 0:
                # For non-grid executors with a single side
                position.add_fill(side, filled_amount_base, filled_amount_quote, executor_id, fees_quote=executor_fees)
                logger.info(
                    f"Aggregated executor {executor_id} to position {position_key}: "
                    f"{side} {filled_amount_base} base @ {filled_amount_quote} quote"
                )
            else:
                logger.debug(f"Executor {executor_id} has no filled amounts to aggregate")

            # Persist position hold to the dedicated table
            await self._persist_position_hold(position)

        except Exception as e:
            logger.error(f"Error aggregating position for executor {executor_id}: {e}", exc_info=True)

    async def _persist_position_hold(self, position: PositionHold):
        """Persist a position hold to the dedicated position_holds table."""
        if not self.db_manager:
            return
        try:
            async with self.db_manager.get_session_context() as session:
                repo = ExecutorRepository(session)
                await repo.upsert_position_hold(
                    account_name=position.account_name,
                    connector_name=position.connector_name,
                    trading_pair=position.trading_pair,
                    controller_id=position.controller_id,
                    buy_amount_base=position.buy_amount_base,
                    buy_amount_quote=position.buy_amount_quote,
                    sell_amount_base=position.sell_amount_base,
                    sell_amount_quote=position.sell_amount_quote,
                    realized_pnl_quote=position.realized_pnl_quote,
                    cum_fees_quote=position.cum_fees_quote,
                    executor_ids=position.executor_ids,
                )
        except Exception as e:
            logger.error(f"Error persisting position hold: {e}", exc_info=True)

    def get_positions_held(
        self,
        account_name: Optional[str] = None,
        connector_name: Optional[str] = None,
        trading_pair: Optional[str] = None,
        controller_id: Optional[str] = None
    ) -> List[PositionHold]:
        """
        Get held positions with optional filtering.

        Args:
            account_name: Filter by account name
            connector_name: Filter by connector name
            trading_pair: Filter by trading pair
            controller_id: Filter by controller ID

        Returns:
            List of PositionHold objects matching the filters
        """
        positions = []

        for position in self._positions_held.values():
            # Apply filters
            if account_name and position.account_name != account_name:
                continue
            if connector_name and position.connector_name != connector_name:
                continue
            if trading_pair and position.trading_pair != trading_pair:
                continue
            if controller_id and position.controller_id != controller_id:
                continue

            # Only include positions with actual volume
            if position.buy_amount_base > 0 or position.sell_amount_base > 0:
                positions.append(position)

        return positions

    def get_position_held(
        self,
        account_name: str,
        connector_name: str,
        trading_pair: str,
        controller_id: str = "main"
    ) -> Optional[PositionHold]:
        """
        Get a specific held position.

        Args:
            account_name: Account name
            connector_name: Connector name
            trading_pair: Trading pair
            controller_id: Controller ID

        Returns:
            PositionHold or None if not found
        """
        position_key = self._get_position_key(account_name, connector_name, trading_pair, controller_id)
        return self._positions_held.get(position_key)

    async def clear_position_held(
        self,
        account_name: str,
        connector_name: str,
        trading_pair: str,
        controller_id: str = "main"
    ) -> bool:
        """
        Clear a specific held position (after manual close or full exit).

        Args:
            account_name: Account name
            connector_name: Connector name
            trading_pair: Trading pair
            controller_id: Controller ID

        Returns:
            True if cleared, False if not found
        """
        position_key = self._get_position_key(account_name, connector_name, trading_pair, controller_id)
        if position_key in self._positions_held:
            del self._positions_held[position_key]
            # Mark position hold as CLEARED in the dedicated table
            if self.db_manager:
                try:
                    async with self.db_manager.get_session_context() as session:
                        repo = ExecutorRepository(session)
                        cleared = await repo.clear_position_hold(
                            account_name=account_name,
                            connector_name=connector_name,
                            trading_pair=trading_pair,
                            controller_id=controller_id
                        )
                        logger.info(f"Cleared position hold record from database for {position_key}: {cleared}")
                except Exception as e:
                    logger.error(f"Failed to clear position hold from database: {e}", exc_info=True)
            logger.info(f"Cleared position hold for {position_key}")
            return True
        return False

    def get_positions_summary(self) -> Dict[str, Any]:
        """
        Get summary of all held positions.

        Returns:
            Dictionary with total positions, PnL, and position list
        """
        positions = self.get_positions_held()
        total_realized_pnl = sum(float(p.realized_pnl_quote) for p in positions)

        return {
            "total_positions": len(positions),
            "total_realized_pnl": total_realized_pnl,
            "positions": [
                {
                    "trading_pair": p.trading_pair,
                    "connector_name": p.connector_name,
                    "account_name": p.account_name,
                    "buy_amount_base": float(p.buy_amount_base),
                    "buy_amount_quote": float(p.buy_amount_quote),
                    "sell_amount_base": float(p.sell_amount_base),
                    "sell_amount_quote": float(p.sell_amount_quote),
                    "net_amount_base": float(p.net_amount_base),
                    "buy_breakeven_price": float(p.buy_breakeven_price) if p.buy_breakeven_price else None,
                    "sell_breakeven_price": float(p.sell_breakeven_price) if p.sell_breakeven_price else None,
                    "matched_amount_base": float(p.matched_amount_base),
                    "unmatched_amount_base": float(p.unmatched_amount_base),
                    "position_side": p.position_side,
                    "realized_pnl_quote": float(p.realized_pnl_quote),
                    "cum_fees_quote": float(p.cum_fees_quote),
                    "executor_count": len(p.executor_ids),
                    "executor_ids": p.executor_ids,
                    "last_updated": p.last_updated.isoformat() if p.last_updated else None
                }
                for p in positions
            ]
        }
