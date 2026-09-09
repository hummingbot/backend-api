"""
Pydantic models for executor API endpoints.

These models wrap Hummingbot's executor configuration types and provide
validation for the REST API.
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field

from .pagination import PaginationParams

# ========================================
# Position Hold for Aggregated Tracking
# ========================================


class PositionHold(BaseModel):
    """
    Tracks aggregated position from executors stopped with keep_position=True.

    Similar to hummingbot's PositionHold, this tracks:
    - Separate buy/sell amounts for proper breakeven calculation
    - Matched volume (realized PnL) vs unmatched volume (unrealized PnL)
    - Aggregation across multiple executors on the same trading pair
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    trading_pair: str = Field(description="Trading pair (e.g., 'BTC-USDT')")
    connector_name: str = Field(description="Connector name")
    account_name: str = Field(description="Account name")
    controller_id: str = Field(default="main", description="Controller that owns this position")

    # Buy side tracking
    buy_amount_base: Decimal = Field(default=Decimal("0"), description="Total bought amount in base currency")
    buy_amount_quote: Decimal = Field(default=Decimal("0"), description="Total spent on buys in quote currency")

    # Sell side tracking
    sell_amount_base: Decimal = Field(default=Decimal("0"), description="Total sold amount in base currency")
    sell_amount_quote: Decimal = Field(default=Decimal("0"), description="Total received from sells in quote currency")

    # Realized PnL from matched positions
    realized_pnl_quote: Decimal = Field(default=Decimal("0"), description="Realized PnL from matched buy/sell pairs")

    # Cumulative fees
    cum_fees_quote: Decimal = Field(default=Decimal("0"), description="Cumulative fees paid in quote currency")

    # Tracking
    executor_ids: List[str] = Field(default_factory=list, description="IDs of executors contributing to this position")
    last_updated: Optional[datetime] = Field(default=None, description="Last update timestamp")

    @computed_field
    @property
    def net_amount_base(self) -> Decimal:
        """Net position in base currency (positive = long, negative = short)."""
        return self.buy_amount_base - self.sell_amount_base

    @computed_field
    @property
    def buy_breakeven_price(self) -> Optional[Decimal]:
        """Average buy price (breakeven for long position)."""
        if self.buy_amount_base > 0:
            return self.buy_amount_quote / self.buy_amount_base
        return None

    @computed_field
    @property
    def sell_breakeven_price(self) -> Optional[Decimal]:
        """Average sell price (breakeven for short position)."""
        if self.sell_amount_base > 0:
            return self.sell_amount_quote / self.sell_amount_base
        return None

    @computed_field
    @property
    def matched_amount_base(self) -> Decimal:
        """Amount that has been matched (min of buy/sell)."""
        return min(self.buy_amount_base, self.sell_amount_base)

    @computed_field
    @property
    def unmatched_amount_base(self) -> Decimal:
        """Absolute unmatched position size."""
        return abs(self.net_amount_base)

    @computed_field
    @property
    def position_side(self) -> Optional[str]:
        """Current position side: LONG, SHORT, or FLAT."""
        if self.net_amount_base > 0:
            return "LONG"
        elif self.net_amount_base < 0:
            return "SHORT"
        return "FLAT"

    def add_fill(
        self,
        side: str,
        amount_base: Decimal,
        amount_quote: Decimal,
        executor_id: Optional[str] = None,
        fees_quote: Decimal = Decimal("0")
    ):
        """
        Add a fill to the position tracking.

        Args:
            side: "BUY" or "SELL"
            amount_base: Amount in base currency
            amount_quote: Amount in quote currency
            executor_id: Optional executor ID to track
            fees_quote: Fees paid for this fill in quote currency
        """
        if side.upper() == "BUY":
            self.buy_amount_base += amount_base
            self.buy_amount_quote += amount_quote
        else:
            self.sell_amount_base += amount_base
            self.sell_amount_quote += amount_quote

        self.cum_fees_quote += fees_quote

        # Calculate realized PnL when we have matched volume
        self._calculate_realized_pnl()

        if executor_id and executor_id not in self.executor_ids:
            self.executor_ids.append(executor_id)

        self.last_updated = datetime.now(timezone.utc)

    def _calculate_realized_pnl(self):
        """Calculate realized PnL from matched buy/sell pairs and settle matched volume.

        After settling, only the unmatched (open) position remains, so breakeven
        prices always reflect the current position, not historical closed trades.
        """
        matched = min(self.buy_amount_base, self.sell_amount_base)
        if matched > 0 and self.buy_amount_base > 0 and self.sell_amount_base > 0:
            # Average prices before settlement
            avg_buy = self.buy_amount_quote / self.buy_amount_base
            avg_sell = self.sell_amount_quote / self.sell_amount_base
            # Realized PnL = matched_amount * (avg_sell - avg_buy)
            self.realized_pnl_quote += matched * (avg_sell - avg_buy)

            # Settle matched volume: remove it from both sides
            self.buy_amount_base -= matched
            self.buy_amount_quote -= matched * avg_buy
            self.sell_amount_base -= matched
            self.sell_amount_quote -= matched * avg_sell

    def get_unrealized_pnl(self, current_price: Decimal) -> Decimal:
        """
        Calculate unrealized PnL for unmatched position (raw price movement).

        Fees are tracked separately in cum_fees_quote.

        Args:
            current_price: Current market price

        Returns:
            Unrealized PnL in quote currency (before fees)
        """
        if self.net_amount_base > 0:
            # Long position: profit if price goes up
            avg_buy = self.buy_breakeven_price or Decimal("0")
            return self.net_amount_base * (current_price - avg_buy)
        elif self.net_amount_base < 0:
            # Short position: profit if price goes down
            avg_sell = self.sell_breakeven_price or Decimal("0")
            return abs(self.net_amount_base) * (avg_sell - current_price)
        return Decimal("0")

    def merge(self, other: "PositionHold"):
        """Merge another PositionHold into this one."""
        self.buy_amount_base += other.buy_amount_base
        self.buy_amount_quote += other.buy_amount_quote
        self.sell_amount_base += other.sell_amount_base
        self.sell_amount_quote += other.sell_amount_quote
        self.cum_fees_quote += other.cum_fees_quote

        for eid in other.executor_ids:
            if eid not in self.executor_ids:
                self.executor_ids.append(eid)

        self._calculate_realized_pnl()
        self.last_updated = datetime.now(timezone.utc)


class PositionHoldResponse(BaseModel):
    """API response model for PositionHold."""
    trading_pair: str
    connector_name: str
    account_name: str
    controller_id: str = Field(default="main", description="Controller that owns this position")
    buy_amount_base: float
    buy_amount_quote: float
    sell_amount_base: float
    sell_amount_quote: float
    net_amount_base: float
    buy_breakeven_price: Optional[float]
    sell_breakeven_price: Optional[float]
    matched_amount_base: float
    unmatched_amount_base: float
    position_side: Optional[str]
    realized_pnl_quote: float
    cum_fees_quote: float = 0.0
    unrealized_pnl_quote: Optional[float] = None
    executor_count: int
    executor_ids: List[str]
    last_updated: Optional[str]


class PositionsSummaryResponse(BaseModel):
    """Summary of all held positions."""
    total_positions: int = Field(description="Number of active position holds")
    total_realized_pnl: float = Field(description="Total realized PnL across all positions")
    total_unrealized_pnl: Optional[float] = Field(
        default=None, description="Total unrealized PnL (None if no rates available)"
    )
    positions: List[PositionHoldResponse] = Field(description="List of position holds")


# ========================================
# Executor Type Definitions
# ========================================

EXECUTOR_TYPES = Literal[
    "position_executor",
    "grid_executor",
    "dca_executor",
    "arbitrage_executor",
    "twap_executor",
    "xemm_executor",
    "order_executor",
    "lp_executor",
]


# ========================================
# API Request Models
# ========================================

class CreateExecutorRequest(BaseModel):
    """Request to create a new executor."""
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "summary": "Position Executor",
                    "description": "Create a position executor with triple barrier",
                    "value": {
                        "account_name": "master_account",
                        "executor_config": {
                            "type": "position_executor",
                            "connector_name": "binance_perpetual",
                            "trading_pair": "BTC-USDT",
                            "side": "BUY",
                            "amount": "0.01",
                            "leverage": 10,
                            "triple_barrier_config": {
                                "stop_loss": "0.02",
                                "take_profit": "0.04",
                                "time_limit": 3600
                            }
                        }
                    }
                },
                {
                    "summary": "LP Executor",
                    "description": "Create an LP position on a CLMM DEX",
                    "value": {
                        "account_name": "master_account",
                        "executor_config": {
                            "type": "lp_executor",
                            "connector_name": "solana-mainnet-beta",
                            "lp_provider": "meteora/clmm",
                            "trading_pair": "SOL-USDC",
                            "pool_address": "HTvjzsfX3yU6BUodCjZ5vZkUrAxMDTrBs3CJaq43ashR",
                            "lower_price": "80",
                            "upper_price": "100",
                            "base_amount": "0",
                            "quote_amount": "10.0",
                            "side": "BUY",
                            "extra_params": {"strategyType": 0},
                            "keep_position": False
                        }
                    }
                }
            ]
        }
    )

    account_name: Optional[str] = Field(
        None,
        description="Account name to use (defaults to master_account)"
    )
    controller_id: str = Field(
        default="main",
        description="Controller ID that owns this executor (for per-agent isolation)"
    )
    executor_config: Dict[str, Any] = Field(
        ...,
        description="Executor configuration. Must include 'type' field and executor-specific parameters."
    )


class StopExecutorRequest(BaseModel):
    """Request to stop an executor."""
    keep_position: bool = Field(
        default=False,
        description="Whether to keep the position open (for position executors)"
    )


class ExecutorFilterRequest(PaginationParams):
    """Request to filter and list executors."""
    account_names: Optional[List[str]] = Field(
        None,
        description="Filter by account names"
    )
    connector_names: Optional[List[str]] = Field(
        None,
        description="Filter by connector names"
    )
    trading_pairs: Optional[List[str]] = Field(
        None,
        description="Filter by trading pairs"
    )
    executor_types: Optional[List[EXECUTOR_TYPES]] = Field(
        None,
        description="Filter by executor types"
    )
    status: Optional[str] = Field(
        None,
        description="Filter by status (RUNNING, TERMINATED, etc.)"
    )
    controller_ids: Optional[List[str]] = Field(
        None,
        description="Filter by controller IDs"
    )


# ========================================
# API Response Models
# ========================================

class ExecutorResponse(BaseModel):
    """Response for a single executor (summary view)."""
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "executor_id": "abc123...",
                "executor_type": "position_executor",
                "account_name": "master_account",
                "connector_name": "binance_perpetual",
                "trading_pair": "BTC-USDT",
                "side": "BUY",
                "status": "RUNNING",
                "is_active": True,
                "is_trading": True,
                "timestamp": 1705315800.0,
                "created_at": "2024-01-15T10:30:00Z",
                "close_type": None,
                "close_timestamp": None,
                "controller_id": None,
                "net_pnl_quote": 125.50,
                "net_pnl_pct": 2.5,
                "cum_fees_quote": 1.25,
                "filled_amount_quote": 5000.0
            }
        }
    )

    executor_id: str = Field(description="Unique executor identifier")
    executor_type: Optional[str] = Field(description="Type of executor")
    account_name: Optional[str] = Field(description="Account name")
    connector_name: Optional[str] = Field(description="Connector name")
    trading_pair: Optional[str] = Field(description="Trading pair")
    side: Optional[str] = Field(None, description="Trade side (BUY/SELL) if applicable")
    status: str = Field(description="Current status (RUNNING, TERMINATED, etc.)")
    is_active: bool = Field(description="Whether the executor is active")
    is_trading: bool = Field(description="Whether the executor has open trades")
    timestamp: Optional[float] = Field(None, description="Creation timestamp (Unix)")
    created_at: Optional[str] = Field(None, description="Creation timestamp (ISO format)")
    close_type: Optional[str] = Field(None, description="How the executor was closed (if applicable)")
    close_timestamp: Optional[float] = Field(None, description="Close timestamp (Unix)")
    controller_id: Optional[str] = Field(None, description="ID of the controller that spawned this executor")
    net_pnl_quote: float = Field(description="Net PnL in quote currency")
    net_pnl_pct: float = Field(description="Net PnL percentage")
    cum_fees_quote: float = Field(description="Cumulative fees in quote currency")
    filled_amount_quote: float = Field(
        description="Volume traded, in quote currency. For an executor that places "
                    "orders the amount it filled IS its volume. An LP executor reports "
                    "the same thing rather than the capital it deposited — depositing "
                    "trades nothing — deriving it from the fees it earned, which are a "
                    "fixed fraction of the swaps that crossed its range, and is 0 while "
                    "it has earned none.")
    error_count: int = Field(default=0, description="Number of ERROR-level log entries captured")
    last_error: Optional[str] = Field(default=None, description="Most recent error message, if any")


class ExecutorDetailResponse(ExecutorResponse):
    """Detailed response for a single executor."""
    config: Optional[Dict[str, Any]] = Field(
        None,
        description="Full executor configuration"
    )
    custom_info: Optional[Dict[str, Any]] = Field(
        None,
        description="Executor-specific custom information"
    )


class CreateExecutorResponse(BaseModel):
    """Response after creating an executor."""
    executor_id: str = Field(description="Unique executor identifier")
    executor_type: str = Field(description="Type of executor created")
    connector_name: str = Field(description="Connector name")
    trading_pair: str = Field(description="Trading pair")
    controller_id: str = Field(default="main", description="Controller that owns this executor")
    status: str = Field(description="Initial status")
    created_at: str = Field(description="Creation timestamp (ISO format)")


class StopExecutorResponse(BaseModel):
    """Response after stopping an executor."""
    executor_id: str = Field(description="Executor identifier")
    status: str = Field(description="New status: 'stopping', or 'already_terminated' when the stop was a no-op")
    keep_position: bool = Field(description="Whether position was kept open")
    close_type: Optional[str] = Field(default=None, description="Final close_type when already terminated")
    position_address: Optional[str] = Field(
        default=None, description="On-chain position address from the executor's final state, if any"
    )
    orphaned_position: bool = Field(
        default=False,
        description="True when the executor terminated with a live on-chain position that needs recovery"
    )
    hold_reason: Optional[str] = Field(
        default=None,
        description="Why a POSITION_HOLD terminal was involuntary (e.g. close_retries_exhausted); None for voluntary holds"
    )


class OrphanedPositionRecord(BaseModel):
    """A terminated executor that may still own an on-chain position."""
    executor_id: str = Field(description="Executor identifier")
    executor_type: str = Field(description="Executor type (e.g. lp_executor)")
    account_name: Optional[str] = Field(default=None, description="Account name")
    connector_name: Optional[str] = Field(
        default=None,
        description="Connector name. For lp_executor this is the network id (e.g. 'solana-mainnet-beta'), "
                    "not the DEX - see lp_provider for the DEX"
    )
    trading_pair: Optional[str] = Field(default=None, description="Trading pair")
    lp_provider: Optional[str] = Field(
        default=None,
        description="DEX connector that holds the position (e.g. 'orca/clmm'), read from the executor config. "
                    "Pass its base name to the CLMM close endpoint"
    )
    pool_address: Optional[str] = Field(
        default=None,
        description="Pool the position was opened against, read from the executor config. Required to close a "
                    "position that was never recorded in the API database (LP-executor positions never are)"
    )
    controller_id: str = Field(default="main", description="Controller/agent grouping label")
    close_type: Optional[str] = Field(
        default=None, description="POSITION_HOLD (involuntary hold), FAILED, or SYSTEM_CLEANUP"
    )
    closed_at: Optional[str] = Field(default=None, description="Termination timestamp (ISO format)")
    position_address: Optional[str] = Field(
        default=None, description="On-chain position address (None for restart cleanups, which never persisted state)"
    )
    state: Optional[str] = Field(default=None, description="Executor state at termination (e.g. FAILED, CLOSING)")
    hold_reason: Optional[str] = Field(
        default=None,
        description="Why the hold was involuntary (e.g. close_retries_exhausted); None for legacy FAILED/SYSTEM_CLEANUP records"
    )
    needs_onchain_reconciliation: bool = Field(
        default=False,
        description="True when the position address is unknown and on-chain state must be checked externally"
    )


class OrphanedPositionsResponse(BaseModel):
    """Terminated executors that may have stranded on-chain positions."""
    count: int = Field(description="Number of orphan candidates")
    orphans: List[OrphanedPositionRecord] = Field(description="Orphan candidate records")


class ExecutorsSummaryResponse(BaseModel):
    """Summary of active executors."""
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "total_active": 5,
                "total_pnl_quote": 1234.56,
                "total_volume_quote": 50000.00,
                "by_type": {"position_executor": 3, "grid_executor": 2},
                "by_connector": {"binance_perpetual": 4, "binance": 1},
                "by_status": {"RUNNING": 5}
            }
        }
    )

    total_active: int = Field(description="Number of active executors")
    total_pnl_quote: float = Field(description="Total PnL across active executors")
    total_volume_quote: float = Field(
        description="Total volume traded across active executors, summing each one's "
                    "filled_amount_quote. Volume GENERATED, not capital deployed: an LP "
                    "executor reports the swaps that crossed it, not its deposit.")
    by_type: Dict[str, int] = Field(description="Executor count by type")
    by_connector: Dict[str, int] = Field(description="Executor count by connector")
    by_status: Dict[str, int] = Field(description="Executor count by status")


class ExecutorTypeBreakdown(BaseModel):
    """Performance breakdown for a single executor type."""
    executor_type: str = Field(description="Executor type name")
    total: int = Field(description="Total executors of this type")
    completed: int = Field(description="Completed executors")
    running: int = Field(description="Currently running executors")
    pnl_quote: float = Field(description="Net PnL in quote currency")
    volume_quote: float = Field(
        description="Total volume traded in quote currency for this executor type. Volume "
                    "GENERATED, not capital deployed.")
    fees_quote: float = Field(description="Cumulative fees in quote currency")


class PerformanceReportResponse(BaseModel):
    """Performance report for executors, optionally filtered by controller_id."""
    controller_id: Optional[str] = Field(None, description="Controller ID filter (None = all)")
    total_executors: int = Field(description="Total executor count")
    by_status: Dict[str, int] = Field(description="Executor count by status")
    pnl_total_quote: float = Field(description="Realized PnL from completed executors in quote currency")
    unrealized_pnl_quote: float = Field(description="Unrealized PnL from active executors and position holds")
    global_pnl_quote: float = Field(description="Global PnL (realized + unrealized)")
    pnl_pct_avg: float = Field(description="Average PnL percentage across completed executors")
    fees_total_quote: float = Field(description="Total cumulative fees in quote currency")
    volume_total_quote: float = Field(
        description="Total volume traded in quote currency. Volume GENERATED, not capital "
                    "deployed: an LP position's deposit is excluded, and the volume its "
                    "range actually saw is derived from the fees it earned.")
    win_rate: float = Field(description="Win rate: fraction of completed executors with positive PnL")
    sharpe_ratio: Optional[float] = Field(None, description="Sharpe ratio of PnL returns (null if <2 executors)")
    by_type: List[ExecutorTypeBreakdown] = Field(description="Performance breakdown by executor type")
    active_positions: int = Field(description="Number of active position holds")


class ExecutorLogEntry(BaseModel):
    """A single log entry from an executor."""
    timestamp: str = Field(description="ISO-format timestamp")
    level: str = Field(description="Log level (DEBUG, INFO, WARNING, ERROR)")
    message: str = Field(description="Log message")
    exc_info: Optional[str] = Field(default=None, description="Exception traceback if present")


class ExecutorLogsResponse(BaseModel):
    """Response for executor log entries."""
    executor_id: str = Field(description="Executor identifier")
    logs: List[ExecutorLogEntry] = Field(description="Log entries")
    total_count: int = Field(description="Total number of log entries (before limit)")
