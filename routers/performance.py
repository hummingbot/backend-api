"""Performance Router - one performance series route for both controllers and executors.

The whole point of this route is that a consumer writes seriesFor(scope) once. The two
populations live in different tables with different shapes, and the branch between them
is a query parameter rather than a path so it stays a parameter to a client too.

`subject=controller` goes through BotsOrchestrator.get_controller_performance_history
unchanged, which is the same call
/bot-orchestration/controller-performance-history makes -- so the two routes share one
query path by construction and cannot drift. Those two existing routes are untouched and
stay wire-compatible; this is new surface only.
"""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from deps import get_bots_orchestrator, get_executor_service
from models.performance import (
    SUBJECT_CONTROLLER,
    PerformanceHistoryResponse,
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
    # FastAPI cannot express "executor_id is only legal when subject=executor", so the
    # cross-check is explicit: a filter aimed at the wrong population would otherwise be
    # accepted and silently ignored, which reads as an empty result rather than a mistake.
    supplied = {
        "bot_name": bot_name,
        "executor_id": executor_id,
        "executor_type": executor_type,
        "account_name": account_name,
        "connector_name": connector_name,
        "trading_pair": trading_pair,
    }
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
