"""Repository for the executor performance snapshot series.

Modelled on ControllerPerformanceRepository -- same descending-timestamp ordering, same
cursor semantics, same over-fetch-by-one has_more -- so a client pages either subject of
/performance/history with identical code.

The one structural difference is the grain. The controller repository hard-codes a
5-minute grain in two places because that is what its dump loop writes; executors are
snapshotted far more often (60s by default, because a position executor can live three
minutes), so the sampler takes the grain as a parameter. The controller repository is
deliberately left alone rather than generalized: it sits on the wire-compatible
/bot-orchestration/controller-performance-* path.
"""
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ControllerPerformanceSnapshot, ExecutorPerformanceSnapshot

# How often a live executor is snapshotted, in minutes. Only used when the caller does
# not say; ExecutorService passes the configured interval through.
DEFAULT_GRAIN_MINUTES = 1.0


class ExecutorPerformanceRepository:
    def __init__(self, session: AsyncSession, grain_minutes: float = DEFAULT_GRAIN_MINUTES):
        self.session = session
        # Guard against a zero or negative interval reaching the sampler's divisor.
        self.grain_minutes = grain_minutes if grain_minutes > 0 else DEFAULT_GRAIN_MINUTES

    @staticmethod
    def _interval_to_minutes(interval: str) -> int:
        interval_map = {
            "1m": 1, "5m": 5, "15m": 15, "30m": 30,
            "1h": 60, "4h": 240, "12h": 720, "1d": 1440
        }
        return interval_map.get(interval, 5)

    @staticmethod
    def _sample_by_interval(history: List[Dict], interval_minutes: int, grain_minutes: float) -> List[Dict]:
        """Thin a descending-timestamp series down to one row per interval PER EXECUTOR.

        The cursor is kept per executor_id, not once for the whole result. A single global
        cursor turns `interval` into a rate limit on the *merged* series: an unnarrowed
        query interleaves every live executor's rows on the same grain, so the executor
        that happens to own the newest row in each window survives and the rest are not
        thinned but dropped entirely -- absent from a 200 response, indistinguishable from
        executors that never reported. At the 60s write grain against the 5m default that
        is roughly one executor kept per five snapshot rows, fleet-wide.

        Grouping makes the interval mean what the parameter says: each executor's own
        series is thinned to its own resolution, and no scope disappears. Input order is
        preserved, so the result stays descending by timestamp across executors.

        A no-op when the requested interval is no coarser than what is stored: there is
        nothing to thin, and the caller gets the native grain.

        Thinning restarts at a page boundary -- the per-executor cursors do not survive in
        `next_cursor`, which is a timestamp -- so an executor's first row on page two can
        sit closer than `interval` to its last row on page one. That over-samples a scope
        by at most one row per page; it never drops one.
        """
        if not history or interval_minutes <= grain_minutes:
            return history

        sampled = []
        last_sampled_time: Dict[Optional[str], datetime] = {}

        for item in history:
            scope = item.get("executor_id")
            item_time = datetime.fromisoformat(item["timestamp"].replace('Z', '+00:00'))
            previous = last_sampled_time.get(scope)
            if previous is None or (previous - item_time).total_seconds() / 60 >= interval_minutes:
                sampled.append(item)
                last_sampled_time[scope] = item_time

        return sampled

    async def save_snapshots(self, snapshots: List[Dict]) -> List[ExecutorPerformanceSnapshot]:
        """Save a batch of executor performance snapshots with a single add_all/flush.

        Each item carries the identity columns, the status pair and the four metrics;
        `snapshot_timestamp` is optional and defaults to the server clock.
        """
        if not snapshots:
            return []

        rows = []
        for item in snapshots:
            data = {
                "executor_id": item["executor_id"],
                "executor_type": item["executor_type"],
                "account_name": item["account_name"],
                "connector_name": item["connector_name"],
                "trading_pair": item["trading_pair"],
                "controller_id": item.get("controller_id") or "main",
                "status": item["status"],
                "close_type": item.get("close_type"),
                "is_terminal": bool(item.get("is_terminal", False)),
                "net_pnl_quote": item.get("net_pnl_quote", 0),
                "net_pnl_pct": item.get("net_pnl_pct", 0),
                "cum_fees_quote": item.get("cum_fees_quote", 0),
                "filled_amount_quote": item.get("filled_amount_quote", 0),
            }
            if item.get("snapshot_timestamp"):
                data["timestamp"] = item["snapshot_timestamp"]
            rows.append(ExecutorPerformanceSnapshot(**data))

        self.session.add_all(rows)
        await self.session.flush()
        return rows

    async def get_latest_for(self, executor_ids: List[str]) -> Dict[str, Dict]:
        """The most recent snapshot of each of the given executors, keyed by executor_id.

        This is what lets the startup reap terminate an orphaned executor at its last
        observed figures instead of the creation-time zeros. Executors with no snapshot
        are simply absent from the result.
        """
        if not executor_ids:
            return {}

        latest = (
            select(
                ExecutorPerformanceSnapshot.executor_id,
                func.max(ExecutorPerformanceSnapshot.timestamp).label("max_timestamp"),
            )
            .where(ExecutorPerformanceSnapshot.executor_id.in_(executor_ids))
            .group_by(ExecutorPerformanceSnapshot.executor_id)
            .subquery()
        )

        query = (
            select(ExecutorPerformanceSnapshot)
            .join(
                latest,
                (ExecutorPerformanceSnapshot.executor_id == latest.c.executor_id) &
                (ExecutorPerformanceSnapshot.timestamp == latest.c.max_timestamp)
            )
        )

        result = await self.session.execute(query)
        return {s.executor_id: self._to_dict(s) for s in result.scalars().all()}

    async def get_latest(
        self,
        executor_id: Optional[str] = None,
        executor_type: Optional[str] = None,
        controller_id: Optional[str] = None,
        account_name: Optional[str] = None,
        connector_name: Optional[str] = None,
        trading_pair: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict]:
        """The most recent snapshot of every matching executor, newest first.

        The executor counterpart of ControllerPerformanceRepository.get_latest_performance,
        with two differences this population forces. Every executor that ever ran leaves a
        terminal row behind, so an unfiltered "latest per scope" grows without bound here
        in a way the controller one does not: this orders newest-first and takes a limit,
        which puts the live executors -- the only ones still being snapshotted -- at the
        top. And a closed executor's latest row IS its terminal row, so this answers "its
        final value" and "its current value" with the same query.

        This reads the series, not memory: an executor younger than one snapshot interval
        has no row yet, and a live one is up to an interval stale. That is deliberate --
        the last point of /performance/latest and the last point of /performance/history
        are the same row. In-memory current figures are what /executors/ serves.
        """
        latest = select(
            ExecutorPerformanceSnapshot.executor_id,
            func.max(ExecutorPerformanceSnapshot.timestamp).label("max_timestamp"),
        ).group_by(ExecutorPerformanceSnapshot.executor_id)

        # The filters go on the grouped subquery rather than the join: they decide which
        # executors are aggregated at all, instead of aggregating the whole table and
        # throwing most of it away afterwards. Every one of them is constant across an
        # executor's rows, so this cannot change which row wins the max.
        if executor_id:
            latest = latest.where(ExecutorPerformanceSnapshot.executor_id == executor_id)
        if executor_type:
            latest = latest.where(ExecutorPerformanceSnapshot.executor_type == executor_type)
        if controller_id:
            latest = latest.where(ExecutorPerformanceSnapshot.controller_id == controller_id)
        if account_name:
            latest = latest.where(ExecutorPerformanceSnapshot.account_name == account_name)
        if connector_name:
            latest = latest.where(ExecutorPerformanceSnapshot.connector_name == connector_name)
        if trading_pair:
            latest = latest.where(ExecutorPerformanceSnapshot.trading_pair == trading_pair)
        latest = latest.subquery()

        query = (
            select(ExecutorPerformanceSnapshot)
            .join(
                latest,
                (ExecutorPerformanceSnapshot.executor_id == latest.c.executor_id) &
                (ExecutorPerformanceSnapshot.timestamp == latest.c.max_timestamp)
            )
            .order_by(desc(ExecutorPerformanceSnapshot.timestamp))
        )
        if limit:
            query = query.limit(limit)

        result = await self.session.execute(query)
        return [self._to_dict(s) for s in result.scalars().all()]

    async def get_performance_history(
        self,
        executor_id: Optional[str] = None,
        executor_type: Optional[str] = None,
        controller_id: Optional[str] = None,
        account_name: Optional[str] = None,
        connector_name: Optional[str] = None,
        trading_pair: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        interval: str = "5m"
    ) -> Tuple[List[Dict], Optional[str], bool]:
        """Get a snapshot series with cursor pagination and interval sampling.

        `controller_id` filters within the executor population only. An in-process
        executor's controller_id and a Docker bot's MQTT controller_id are not guaranteed
        to name the same thing, so this is never a key to join the two subjects on.
        """
        interval_minutes = self._interval_to_minutes(interval)
        query = (
            select(ExecutorPerformanceSnapshot)
            .order_by(desc(ExecutorPerformanceSnapshot.timestamp))
        )

        if executor_id:
            query = query.filter(ExecutorPerformanceSnapshot.executor_id == executor_id)
        if executor_type:
            query = query.filter(ExecutorPerformanceSnapshot.executor_type == executor_type)
        if controller_id:
            query = query.filter(ExecutorPerformanceSnapshot.controller_id == controller_id)
        if account_name:
            query = query.filter(ExecutorPerformanceSnapshot.account_name == account_name)
        if connector_name:
            query = query.filter(ExecutorPerformanceSnapshot.connector_name == connector_name)
        if trading_pair:
            query = query.filter(ExecutorPerformanceSnapshot.trading_pair == trading_pair)
        if start_time:
            query = query.filter(ExecutorPerformanceSnapshot.timestamp >= start_time)
        if end_time:
            query = query.filter(ExecutorPerformanceSnapshot.timestamp <= end_time)
        if cursor:
            try:
                cursor_time = datetime.fromisoformat(cursor.replace('Z', '+00:00'))
                query = query.filter(ExecutorPerformanceSnapshot.timestamp < cursor_time)
            except (ValueError, TypeError):
                pass

        # Over-fetch by the thinning ratio so a sampled page still fills up, plus one row
        # to tell has_more apart from a page that happens to land exactly on the limit.
        # The ratio is the worst case: sampling is per executor, so a window covering many
        # executors retains more of what was fetched, never less.
        sampling_multiplier = max(1, int(interval_minutes // self.grain_minutes))
        fetch_limit = (limit * sampling_multiplier + 1) if limit else (100 * sampling_multiplier + 1)
        query = query.limit(fetch_limit)

        result = await self.session.execute(query)
        snapshots = result.scalars().all()

        history = [self._to_dict(s) for s in snapshots]

        sampled = self._sample_by_interval(history, interval_minutes, self.grain_minutes)

        has_more = len(sampled) > limit if limit else False
        if has_more:
            sampled = sampled[:limit]

        next_cursor = None
        if has_more and sampled:
            next_cursor = sampled[-1]["timestamp"]

        return sampled, next_cursor, has_more

    async def prune_older_than(self, cutoff: datetime) -> Tuple[int, int]:
        """Delete snapshots older than `cutoff` from both snapshot tables.

        Retention is one policy, not two: the operator sets how much performance history
        to keep, and both series obey it. It lives here rather than on the controller
        repository because that one is on the wire-compatible read path and this is where
        the growth is generated.

        Returns (executor rows deleted, controller rows deleted).
        """
        executor_result = await self.session.execute(
            delete(ExecutorPerformanceSnapshot).where(ExecutorPerformanceSnapshot.timestamp < cutoff)
        )
        controller_result = await self.session.execute(
            delete(ControllerPerformanceSnapshot).where(ControllerPerformanceSnapshot.timestamp < cutoff)
        )
        await self.session.flush()
        return executor_result.rowcount or 0, controller_result.rowcount or 0

    @staticmethod
    def _to_dict(snapshot: ExecutorPerformanceSnapshot) -> Dict:
        return {
            "timestamp": snapshot.timestamp.isoformat(),
            "executor_id": snapshot.executor_id,
            "executor_type": snapshot.executor_type,
            "account_name": snapshot.account_name,
            "connector_name": snapshot.connector_name,
            "trading_pair": snapshot.trading_pair,
            "controller_id": snapshot.controller_id,
            "status": snapshot.status,
            "close_type": snapshot.close_type,
            "is_terminal": bool(snapshot.is_terminal),
            "net_pnl_quote": float(snapshot.net_pnl_quote or 0),
            "net_pnl_pct": float(snapshot.net_pnl_pct or 0),
            "cum_fees_quote": float(snapshot.cum_fees_quote or 0),
            "filled_amount_quote": float(snapshot.filled_amount_quote or 0),
        }
