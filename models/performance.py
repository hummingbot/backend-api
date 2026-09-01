"""One normalized performance row, for both the controller and the executor series.

The two populations are stored differently on purpose -- the controller's payload is an
opaque, core-versioned PerformanceReport that has to be blobbed, the executor's is
ExecutorInfo, whose fields this repo names all over the place -- but what a consumer
needs symmetric is the *response*, not the storage. So both are mapped into the shape
below and a client writes seriesFor(scope) once instead of two clients.

The normalization is additive, never lossy: everything the controller report carries that
has no executor counterpart (open_order_volume, inventory_imbalance, positions_summary,
close_type_counts) stays reachable in the `performance` passthrough.
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

SUBJECT_CONTROLLER = "controller"
SUBJECT_EXECUTOR = "executor"

# "Settled" for an executor means its position is really closed. A POSITION_HOLD close
# hands the position on to position_holds, so counting it as realized here would
# double-count it -- the same exclusion ExecutorRepository.get_performance_report applies.
_UNSETTLED_CLOSE_TYPE = "POSITION_HOLD"


class PerformanceRow(BaseModel):
    """A single point of a performance series, for either subject."""

    timestamp: str = Field(description="ISO 8601 timestamp of the snapshot")
    subject: str = Field(description='Which population this row came from: "controller" or "executor"')
    scope_id: str = Field(description="controller_id for controllers, executor_id for executors")
    status: str = Field(description="Status at the moment of the snapshot")
    is_terminal: bool = Field(
        default=False,
        description="True only on the final row of a completed executor's series. "
                    "Always false for controllers, which have no terminal row."
    )

    realized_pnl_quote: float = Field(description="Realized PnL in quote currency")
    unrealized_pnl_quote: float = Field(description="Unrealized PnL in quote currency")
    global_pnl_quote: float = Field(description="Total PnL (realized + unrealized) in quote currency")
    global_pnl_pct: float = Field(description="Total PnL as a fraction of the capital deployed")
    volume_quote: float = Field(
        description="Volume traded in quote currency. Volume GENERATED, not capital "
                    "deployed: an LP position's deposit is excluded."
    )
    cum_fees_quote: Optional[float] = Field(
        default=None,
        description="Cumulative fees in quote currency. NULL for controllers: their "
                    "PerformanceReport genuinely has no fees field, and unknown is not zero."
    )

    bot_name: Optional[str] = Field(default=None, description="Docker bot name; controllers only")
    controller_id: Optional[str] = Field(default=None, description="Controller ID, on both subjects")
    executor_id: Optional[str] = Field(default=None, description="Executor ID; executors only")
    executor_type: Optional[str] = Field(default=None, description="Executor type; executors only")
    account_name: Optional[str] = Field(default=None, description="Account name; executors only")
    connector_name: Optional[str] = Field(default=None, description="Connector name; executors only")
    trading_pair: Optional[str] = Field(default=None, description="Trading pair; executors only")
    close_type: Optional[str] = Field(default=None, description="Close type; set on an executor's terminal row")

    performance: Dict[str, Any] = Field(
        default_factory=dict,
        description="Controllers: the raw stored PerformanceReport. Empty for executors, "
                    "whose metrics are all typed columns above."
    )
    custom_info: Dict[str, Any] = Field(
        default_factory=dict,
        description="Controllers: the raw stored custom_info. Empty for executors -- these "
                    "payloads carry fill_events and grid levels, which have no business in "
                    "a per-minute row."
    )


class PerformanceHistoryResponse(BaseModel):
    """Envelope of GET /performance/history, matching the controller route's shape."""

    status: str = Field(default="success")
    data: List[PerformanceRow]
    pagination: Dict[str, Any] = Field(
        description="next_cursor, has_more, limit and the interval that was requested"
    )


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def controller_row_to_performance_row(row: Dict[str, Any]) -> PerformanceRow:
    """Map a ControllerPerformanceRepository row into the normalized shape.

    The report is read by name but never required: a controller that reported nothing
    yields a row of zeros with its raw payload still attached, rather than disappearing
    from the series.
    """
    performance = row.get("performance") or {}

    return PerformanceRow(
        timestamp=row["timestamp"],
        subject=SUBJECT_CONTROLLER,
        scope_id=row.get("controller_id") or "",
        status=row.get("status") or "unknown",
        is_terminal=False,
        realized_pnl_quote=_as_float(performance.get("realized_pnl_quote")),
        unrealized_pnl_quote=_as_float(performance.get("unrealized_pnl_quote")),
        global_pnl_quote=_as_float(performance.get("global_pnl_quote")),
        global_pnl_pct=_as_float(performance.get("global_pnl_pct")),
        volume_quote=_as_float(performance.get("volume_traded")),
        # Deliberately not 0.0: PerformanceReport has no fees field, and a consumer
        # charting fees has to be able to tell "not measured" from "measured and empty".
        cum_fees_quote=None,
        bot_name=row.get("bot_name"),
        controller_id=row.get("controller_id"),
        performance=performance,
        custom_info=row.get("custom_info") or {},
    )


def executor_row_to_performance_row(row: Dict[str, Any]) -> PerformanceRow:
    """Map an ExecutorPerformanceRepository row into the normalized shape.

    An executor reports one net PnL, not a realized/unrealized pair, so the split is made
    from whether the position is settled: everything is unrealized while it is open, and
    realized once it closes for real.
    """
    net_pnl_quote = _as_float(row.get("net_pnl_quote"))
    settled = bool(row.get("is_terminal")) and row.get("close_type") != _UNSETTLED_CLOSE_TYPE

    return PerformanceRow(
        timestamp=row["timestamp"],
        subject=SUBJECT_EXECUTOR,
        scope_id=row.get("executor_id") or "",
        status=row.get("status") or "unknown",
        is_terminal=bool(row.get("is_terminal")),
        realized_pnl_quote=net_pnl_quote if settled else 0.0,
        unrealized_pnl_quote=0.0 if settled else net_pnl_quote,
        global_pnl_quote=net_pnl_quote,
        global_pnl_pct=_as_float(row.get("net_pnl_pct")),
        # filled_amount_quote IS the volume traded, on every executor type including LP.
        # There is no second volume column to reach for -- see
        # test_executor_volume_is_the_filled_amount.py.
        volume_quote=_as_float(row.get("filled_amount_quote")),
        cum_fees_quote=_as_float(row.get("cum_fees_quote")),
        controller_id=row.get("controller_id"),
        executor_id=row.get("executor_id"),
        executor_type=row.get("executor_type"),
        account_name=row.get("account_name"),
        connector_name=row.get("connector_name"),
        trading_pair=row.get("trading_pair"),
        close_type=row.get("close_type"),
    )
