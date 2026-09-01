"""Performance Router - one performance surface for both controllers and executors.

The whole point of these routes is that a consumer writes seriesFor(scope) and
latestFor(scope) once each. The two populations live in different tables with different
shapes, and the branch between them is a query parameter rather than a path so it stays a
parameter to a client too.

Two routes, mirroring the pair this replaces: `/history` for the series and `/latest` for
the current value of every scope. Together they are a complete substitute for
/bot-orchestration/controller-performance-history and -latest, so a consumer can move off
those entirely rather than straddling both surfaces.

The controller subject of each goes through the existing BotsOrchestrator method the old
route already calls -- get_controller_performance_history and
get_latest_controller_performance -- so old and new share one query path by construction
and cannot drift. Those two existing routes are untouched and stay wire-compatible; this
is new surface only.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from deps import get_bots_orchestrator, get_executor_service
from models.performance import (
    SUBJECT_CONTROLLER,
    PerformanceHistoryResponse,
    PerformanceLatestResponse,
    controller_row_to_performance_row,
    executor_row_to_performance_row,
)
from services.bots_orchestrator import BotsOrchestrator
from services.executor_service import ExecutorService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Performance"], prefix="/performance")

# Which filters belong to which population. A controller_id means different things on
# either side -- an MQTT bot's controller and an in-process executor's controller tag are
# not guaranteed to name the same thing -- so it is a filter WITHIN a subject and never a
# key to join the two on.
_CONTROLLER_ONLY_FILTERS = ("bot_name",)
_EXECUTOR_ONLY_FILTERS = ("executor_id", "executor_type", "account_name", "connector_name", "trading_pair")


def _reject_foreign_filters(subject: str, **supplied) -> None:
    """400 when a filter belongs to the other population.

    FastAPI cannot express "executor_id is only legal when subject=executor", so the
    cross-check is explicit: a filter aimed at the wrong population would otherwise be
    accepted and silently ignored, which reads as an empty result rather than a mistake.
    Both routes share this one rule so they cannot drift into disagreeing about which
    filter belongs where.
    """
    wrong_subject = (
        _EXECUTOR_ONLY_FILTERS if subject == SUBJECT_CONTROLLER else _CONTROLLER_ONLY_FILTERS
    )
    offending = [name for name in wrong_subject if supplied.get(name) is not None]
    if offending:
        raise HTTPException(
            status_code=400,
            detail=f"{', '.join(offending)} {'is' if len(offending) == 1 else 'are'} "
                   f"not a valid filter for subject={subject}",
        )


def _timestamp_key(row: dict) -> datetime:
    """Sort key for the controller rows, whose timestamps arrive as ISO strings.

    An unparseable timestamp sorts last rather than raising: one malformed row must not
    take down a dashboard's whole tile set.
    """
    try:
        return datetime.fromisoformat(str(row.get("timestamp")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


@router.get("/history", response_model=PerformanceHistoryResponse)
async def get_performance_history(
    subject: str = Query(description='Which population to read: "controller" or "executor"',
                         pattern="^(controller|executor)$"),
    bot_name: Optional[str] = Query(default=None, description="Filter by bot name (controller subject only)"),
    controller_id: Optional[str] = Query(default=None, description="Filter by controller ID (either subject)"),
    executor_id: Optional[str] = Query(default=None, description="Filter by executor ID (executor subject only)"),
    executor_type: Optional[str] = Query(default=None, description="Filter by executor type (executor subject only)"),
    account_name: Optional[str] = Query(default=None, description="Filter by account name (executor subject only)"),
    connector_name: Optional[str] = Query(default=None, description="Filter by connector (executor subject only)"),
    trading_pair: Optional[str] = Query(default=None, description="Filter by trading pair (executor subject only)"),
    start_time: Optional[str] = Query(default=None, description="ISO 8601 start of the window"),
    end_time: Optional[str] = Query(default=None, description="ISO 8601 end of the window"),
    interval: str = Query(default="5m", pattern="^(1m|5m|15m|30m|1h|4h|12h|1d)$"),
    limit: int = Query(default=100, le=1000),
    cursor: Optional[str] = Query(default=None, description="Cursor from a previous page's next_cursor"),
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator),
    executor_service: ExecutorService = Depends(get_executor_service),
):
    """Historical performance for one subject, newest first, in one normalized row shape.

    Both subjects page identically: descending timestamp, `cursor` is the last row's
    timestamp, `has_more` says whether another page exists.

    `interval` is a floor, not a guarantee. The controller series is written on a
    5-minute grain, so asking it for `1m` returns that native grain; the executor series
    is written on PERFORMANCE_EXECUTOR_SNAPSHOT_INTERVAL (60s by default). The echoed
    `interval` says what was asked for, the timestamps say what was served.

    An executor's series is answered from executor_performance_snapshots alone, including
    its final value: completion writes a terminal row, so there is no join to the
    executors table and no "and then append the last point" rule.
    """
    _reject_foreign_filters(
        subject,
        bot_name=bot_name,
        executor_id=executor_id,
        executor_type=executor_type,
        account_name=account_name,
        connector_name=connector_name,
        trading_pair=trading_pair,
    )

    try:
        parsed_start = datetime.fromisoformat(start_time) if start_time else None
        parsed_end = datetime.fromisoformat(end_time) if end_time else None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid datetime format: {e}")

    try:
        if subject == SUBJECT_CONTROLLER:
            history, next_cursor, has_more = await bots_manager.get_controller_performance_history(
                bot_name=bot_name,
                controller_id=controller_id,
                limit=limit,
                cursor=cursor,
                start_time=parsed_start,
                end_time=parsed_end,
                interval=interval,
            )
            rows = [controller_row_to_performance_row(row) for row in history]
        else:
            history, next_cursor, has_more = await executor_service.get_executor_performance_history(
                executor_id=executor_id,
                executor_type=executor_type,
                controller_id=controller_id,
                account_name=account_name,
                connector_name=connector_name,
                trading_pair=trading_pair,
                limit=limit,
                cursor=cursor,
                start_time=parsed_start,
                end_time=parsed_end,
                interval=interval,
            )
            rows = [executor_row_to_performance_row(row) for row in history]
    except Exception as e:
        logger.error(f"Failed to get {subject} performance history: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

    return PerformanceHistoryResponse(
        status="success",
        data=rows,
        pagination={
            "next_cursor": next_cursor,
            "has_more": has_more,
            "limit": limit,
            "interval": interval,
        },
    )


@router.get("/latest", response_model=PerformanceLatestResponse)
async def get_latest_performance(
    subject: str = Query(description='Which population to read: "controller" or "executor"',
                         pattern="^(controller|executor)$"),
    bot_name: Optional[str] = Query(default=None, description="Filter by bot name (controller subject only)"),
    controller_id: Optional[str] = Query(default=None, description="Filter by controller ID (either subject)"),
    executor_id: Optional[str] = Query(default=None, description="Filter by executor ID (executor subject only)"),
    executor_type: Optional[str] = Query(default=None, description="Filter by executor type (executor subject only)"),
    account_name: Optional[str] = Query(default=None, description="Filter by account name (executor subject only)"),
    connector_name: Optional[str] = Query(default=None, description="Filter by connector (executor subject only)"),
    trading_pair: Optional[str] = Query(default=None, description="Filter by trading pair (executor subject only)"),
    limit: int = Query(default=100, le=1000, description="Cap on how many scopes come back, newest first"),
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator),
    executor_service: ExecutorService = Depends(get_executor_service),
):
    """The most recent snapshot of every scope in one subject, newest first.

    One row per scope -- per (bot, controller), or per executor -- in the same normalized
    shape `/history` returns, so a dashboard's live tiles and its charts read the same
    fields off the same client.

    `limit` caps how many scopes come back, it is not a page boundary: there is no cursor
    here because this is not a series. Newest-first ordering means the scopes that are
    still reporting come first, which matters far more for executors than for controllers
    -- every executor that ever ran leaves a terminal row behind, so the executor
    population grows without bound while the controller one does not.

    This reads the stored series, not live memory. An executor younger than one snapshot
    interval has no row yet and does not appear, and a live one's figures are up to one
    interval stale -- by design, so that this row and the last row of `/history` are the
    same row. Live in-memory figures are what `/executors/` serves.

    A closed executor's latest row is its terminal row, carrying `is_terminal: true` and
    its `close_type`, so "the final value" needs no separate call.
    """
    _reject_foreign_filters(
        subject,
        bot_name=bot_name,
        executor_id=executor_id,
        executor_type=executor_type,
        account_name=account_name,
        connector_name=connector_name,
        trading_pair=trading_pair,
    )

    try:
        if subject == SUBJECT_CONTROLLER:
            # get_latest_controller_performance only narrows by bot_name -- it is the
            # method the old route calls and is deliberately not changed. controller_id
            # and the limit are applied here instead. That is cheap: the result is one row
            # per (bot, controller), which is small by construction.
            snapshots = await bots_manager.get_latest_controller_performance(bot_name=bot_name)
            if controller_id:
                snapshots = [s for s in snapshots if s.get("controller_id") == controller_id]
            # sorted(), not .sort(): the list belongs to the orchestrator's caller and
            # this route has no business reordering it in place.
            ordered = sorted(snapshots, key=_timestamp_key, reverse=True)
            rows = [controller_row_to_performance_row(row) for row in ordered[:limit]]
        else:
            snapshots = await executor_service.get_latest_executor_performance(
                executor_id=executor_id,
                executor_type=executor_type,
                controller_id=controller_id,
                account_name=account_name,
                connector_name=connector_name,
                trading_pair=trading_pair,
                limit=limit,
            )
            rows = [executor_row_to_performance_row(row) for row in snapshots]
    except Exception as e:
        logger.error(f"Failed to get latest {subject} performance: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

    return PerformanceLatestResponse(status="success", data=rows)
