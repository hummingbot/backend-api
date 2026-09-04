import json
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ControllerPerformanceSnapshot


class ControllerPerformanceRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _interval_to_minutes(interval: str) -> int:
        interval_map = {
            "5m": 5, "15m": 15, "30m": 30,
            "1h": 60, "4h": 240, "12h": 720, "1d": 1440
        }
        return interval_map.get(interval, 5)

    @staticmethod
    def _sample_by_interval(history: List[Dict], interval_minutes: int) -> List[Dict]:
        """Thin a descending-timestamp series to one row per interval PER CONTROLLER.

        The cursor is kept per (bot_name, controller_id), not once for the whole result.
        A single global cursor makes `interval` a rate limit on the merged series, so an
        unnarrowed query over a fleet drops whole controllers instead of thinning each of
        them -- a 12-controller fleet answered with 11 of 12 at `1h`. Input order is
        preserved, so the result stays descending by timestamp across controllers.
        """
        if not history or interval_minutes <= 5:
            return history

        sampled = []
        last_sampled_time: Dict[Tuple[Optional[str], Optional[str]], datetime] = {}

        for item in history:
            scope = (item.get("bot_name"), item.get("controller_id"))
            item_time = datetime.fromisoformat(item["timestamp"].replace('Z', '+00:00'))
            previous = last_sampled_time.get(scope)
            if previous is None or (previous - item_time).total_seconds() / 60 >= interval_minutes:
                sampled.append(item)
                last_sampled_time[scope] = item_time

        return sampled

    async def save_controller_performance(
        self,
        bot_name: str,
        controller_id: str,
        status: str,
        performance: Dict,
        custom_info: Dict,
        snapshot_timestamp: Optional[datetime] = None
    ) -> ControllerPerformanceSnapshot:
        """Save a controller performance snapshot."""
        data = {
            "bot_name": bot_name,
            "controller_id": controller_id,
            "status": status,
            "performance": json.dumps(performance) if performance else None,
            "custom_info": json.dumps(custom_info) if custom_info else None,
        }
        if snapshot_timestamp:
            data["timestamp"] = snapshot_timestamp

        snapshot = ControllerPerformanceSnapshot(**data)
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot

    async def save_controller_performances(self, snapshots: List[Dict]) -> List[ControllerPerformanceSnapshot]:
        """Save multiple controller performance snapshots with a single add_all/flush.

        Each item in `snapshots` is a dict with keys: bot_name, controller_id, status,
        performance, custom_info and optionally snapshot_timestamp.
        """
        if not snapshots:
            return []

        rows = []
        for item in snapshots:
            data = {
                "bot_name": item["bot_name"],
                "controller_id": item["controller_id"],
                "status": item["status"],
                "performance": json.dumps(item["performance"]) if item.get("performance") else None,
                "custom_info": json.dumps(item["custom_info"]) if item.get("custom_info") else None,
            }
            if item.get("snapshot_timestamp"):
                data["timestamp"] = item["snapshot_timestamp"]
            rows.append(ControllerPerformanceSnapshot(**data))

        self.session.add_all(rows)
        await self.session.flush()
        return rows

    async def get_latest_performance(
        self,
        bot_name: Optional[str] = None
    ) -> List[Dict]:
        """Get the most recent performance snapshot for each bot/controller."""
        from sqlalchemy import func

        subquery = (
            select(
                ControllerPerformanceSnapshot.bot_name,
                ControllerPerformanceSnapshot.controller_id,
                func.max(ControllerPerformanceSnapshot.timestamp).label("max_timestamp")
            )
            .group_by(
                ControllerPerformanceSnapshot.bot_name,
                ControllerPerformanceSnapshot.controller_id
            )
        )
        if bot_name:
            subquery = subquery.filter(ControllerPerformanceSnapshot.bot_name == bot_name)
        subquery = subquery.subquery()

        query = (
            select(ControllerPerformanceSnapshot)
            .join(
                subquery,
                (ControllerPerformanceSnapshot.bot_name == subquery.c.bot_name) &
                (ControllerPerformanceSnapshot.controller_id == subquery.c.controller_id) &
                (ControllerPerformanceSnapshot.timestamp == subquery.c.max_timestamp)
            )
        )

        result = await self.session.execute(query)
        snapshots = result.scalars().all()
        return [self._to_dict(s) for s in snapshots]

    async def get_performance_history(
        self,
        bot_name: Optional[str] = None,
        controller_id: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        interval: str = "5m"
    ) -> Tuple[List[Dict], Optional[str], bool]:
        """Get historical performance with cursor pagination and interval sampling."""
        interval_minutes = self._interval_to_minutes(interval)
        query = (
            select(ControllerPerformanceSnapshot)
            .order_by(desc(ControllerPerformanceSnapshot.timestamp))
        )

        if bot_name:
            query = query.filter(ControllerPerformanceSnapshot.bot_name == bot_name)
        if controller_id:
            query = query.filter(ControllerPerformanceSnapshot.controller_id == controller_id)
        if start_time:
            query = query.filter(ControllerPerformanceSnapshot.timestamp >= start_time)
        if end_time:
            query = query.filter(ControllerPerformanceSnapshot.timestamp <= end_time)
        if cursor:
            try:
                cursor_time = datetime.fromisoformat(cursor.replace('Z', '+00:00'))
                query = query.filter(ControllerPerformanceSnapshot.timestamp < cursor_time)
            except (ValueError, TypeError):
                pass

        sampling_multiplier = max(1, interval_minutes // 5)
        fetch_limit = (limit * sampling_multiplier + 1) if limit else (100 * sampling_multiplier + 1)
        query = query.limit(fetch_limit)

        result = await self.session.execute(query)
        snapshots = result.scalars().all()

        history = [self._to_dict(s) for s in snapshots]

        sampled = self._sample_by_interval(history, interval_minutes)

        has_more = len(sampled) > limit if limit else False
        if has_more:
            sampled = sampled[:limit]

        next_cursor = None
        if has_more and sampled:
            next_cursor = sampled[-1]["timestamp"]

        return sampled, next_cursor, has_more

    @staticmethod
    def _to_dict(snapshot: ControllerPerformanceSnapshot) -> Dict:
        return {
            "timestamp": snapshot.timestamp.isoformat(),
            "bot_name": snapshot.bot_name,
            "controller_id": snapshot.controller_id,
            "status": snapshot.status,
            "performance": json.loads(snapshot.performance) if snapshot.performance else {},
            "custom_info": json.loads(snapshot.custom_info) if snapshot.custom_info else {},
        }
