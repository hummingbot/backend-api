import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import BotRun


class BotRunRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_bot_run(
        self,
        bot_name: str,
        instance_name: str,
        strategy_type: str,  # 'script' or 'controller'
        strategy_name: str,
        account_name: str,
        config_name: Optional[str] = None,
        image_version: Optional[str] = None,
        deployment_config: Optional[Dict[str, Any]] = None
    ) -> BotRun:
        """Create a new bot run record."""
        bot_run = BotRun(
            bot_name=bot_name,
            instance_name=instance_name,
            strategy_type=strategy_type,
            strategy_name=strategy_name,
            config_name=config_name,
            account_name=account_name,
            image_version=image_version,
            deployment_config=json.dumps(deployment_config) if deployment_config else None,
            deployment_status="DEPLOYED",
            run_status="CREATED"
        )

        self.session.add(bot_run)
        await self.session.flush()
        await self.session.refresh(bot_run)
        return bot_run

    async def update_bot_run_stopped(
        self,
        bot_name: str,
        final_status: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None
    ) -> Optional[BotRun]:
        """Mark a bot run as stopped and save final status."""
        stmt = select(BotRun).where(
            and_(
                BotRun.bot_name == bot_name,
                or_(BotRun.run_status == "RUNNING", BotRun.run_status == "CREATED")
            )
        ).order_by(desc(BotRun.deployed_at))

        result = await self.session.execute(stmt)
        bot_run = result.scalar_one_or_none()

        if bot_run:
            bot_run.run_status = "STOPPED" if not error_message else "ERROR"
            # Aware UTC, not utcnow(): stopped_at is TIMESTAMP(timezone=True), and a naive
            # datetime is stored as if it were already in the session's local timezone, so
            # utcnow() landed the row at the server's UTC offset behind the real stop time.
            # stop-and-archive masked it -- update_bot_run_archived overwrote the value
            # with a correct one -- but a bot stopped and never archived kept the skew,
            # and run duration and performance-window attribution are read off this field.
            bot_run.stopped_at = datetime.now(timezone.utc)
            bot_run.final_status = json.dumps(final_status) if final_status else None
            bot_run.error_message = error_message
            await self.session.flush()
            await self.session.refresh(bot_run)

        return bot_run

    async def update_bot_run_archived(self, bot_name: str) -> Optional[BotRun]:
        """Mark a bot run as archived."""
        stmt = select(BotRun).where(
            BotRun.bot_name == bot_name
        ).order_by(desc(BotRun.deployed_at))

        result = await self.session.execute(stmt)
        bot_run = result.scalar_one_or_none()

        if bot_run:
            bot_run.deployment_status = "ARCHIVED"
            bot_run.stopped_at = datetime.now(timezone.utc)
            await self.session.flush()
            await self.session.refresh(bot_run)

        return bot_run

    async def get_bot_runs(
        self,
        bot_name: Optional[str] = None,
        account_name: Optional[str] = None,
        strategy_type: Optional[str] = None,
        strategy_name: Optional[str] = None,
        run_status: Optional[str] = None,
        deployment_status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[BotRun]:
        """Get bot runs with optional filters."""
        stmt = select(BotRun)

        conditions = []
        if bot_name:
            conditions.append(BotRun.bot_name == bot_name)
        if account_name:
            conditions.append(BotRun.account_name == account_name)
        if strategy_type:
            conditions.append(BotRun.strategy_type == strategy_type)
        if strategy_name:
            conditions.append(BotRun.strategy_name == strategy_name)
        if run_status:
            conditions.append(BotRun.run_status == run_status)
        if deployment_status:
            conditions.append(BotRun.deployment_status == deployment_status)

        if conditions:
            stmt = stmt.where(and_(*conditions))

        stmt = stmt.order_by(desc(BotRun.deployed_at)).limit(limit).offset(offset)

        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_bot_run_by_id(self, bot_run_id: int) -> Optional[BotRun]:
        """Get a specific bot run by ID."""
        stmt = select(BotRun).where(BotRun.id == bot_run_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_latest_bot_run(self, bot_name: str) -> Optional[BotRun]:
        """Get the latest bot run for a specific bot."""
        stmt = select(BotRun).where(
            BotRun.bot_name == bot_name
        ).order_by(desc(BotRun.deployed_at))

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_bot_runs(self) -> List[BotRun]:
        """Get all currently active (running) bot runs."""
        stmt = select(BotRun).where(
            and_(
                BotRun.run_status == "RUNNING",
                BotRun.deployment_status == "DEPLOYED"
            )
        ).order_by(desc(BotRun.deployed_at))

        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_bot_run_stats(self) -> Dict[str, Any]:
        """Get statistics about bot runs."""
        # Total runs
        total_stmt = select(func.count(BotRun.id))
        total_result = await self.session.execute(total_stmt)
        total_runs = total_result.scalar()

        # Active runs
        active_stmt = select(func.count(BotRun.id)).where(
            and_(
                BotRun.run_status == "RUNNING",
                BotRun.deployment_status == "DEPLOYED"
            )
        )
        active_result = await self.session.execute(active_stmt)
        active_runs = active_result.scalar()

        # Runs by strategy type
        strategy_stmt = select(
            BotRun.strategy_type,
            func.count(BotRun.id).label('count')
        ).group_by(BotRun.strategy_type)
        strategy_result = await self.session.execute(strategy_stmt)
        strategy_counts = {row.strategy_type: row.count for row in strategy_result}

        # Runs by status
        status_stmt = select(
            BotRun.run_status,
            func.count(BotRun.id).label('count')
        ).group_by(BotRun.run_status)
        status_result = await self.session.execute(status_stmt)
        status_counts = {row.run_status: row.count for row in status_result}

        return {
            "total_runs": total_runs,
            "active_runs": active_runs,
            "strategy_type_counts": strategy_counts,
            "status_counts": status_counts
        }

    async def delete_bot_run(self, bot_run_id: int) -> Optional[BotRun]:
        """Delete a bot run record by ID. Returns the deleted record or None."""
        stmt = select(BotRun).where(BotRun.id == bot_run_id)
        result = await self.session.execute(stmt)
        bot_run = result.scalar_one_or_none()

        if bot_run:
            await self.session.delete(bot_run)
            await self.session.flush()

        return bot_run

    async def delete_bot_runs_by_bot_name(self, bot_name: str) -> int:
        """Delete all bot run records for a given bot_name. Returns count deleted."""
        stmt = select(BotRun).where(BotRun.bot_name == bot_name)
        result = await self.session.execute(stmt)
        bot_runs = result.scalars().all()

        count = len(bot_runs)
        for bot_run in bot_runs:
            await self.session.delete(bot_run)

        if count > 0:
            await self.session.flush()

        return count
