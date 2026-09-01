"""
Repository for executor database operations.
"""
import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, case, desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ExecutorOrder, ExecutorRecord, PositionHoldRecord
from database.repositories.executor_performance_repository import ExecutorPerformanceRepository


class ExecutorRepository:
    """Repository for ExecutorRecord and ExecutorOrder database operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    # ========================================
    # ExecutorRecord Operations
    # ========================================

    async def create_executor(
            self,
            executor_id: str,
            executor_type: str,
            account_name: str,
            connector_name: str,
            trading_pair: str,
            config: Optional[str] = None,
            status: str = "RUNNING",
            controller_id: str = "main",
            created_at: Optional[datetime] = None
    ) -> ExecutorRecord:
        """Create a new executor record.

        `created_at` defaults to the server clock; pass it only when the row is being
        written after the fact (see upsert_executor_completion) so the record still
        orders by when the executor actually started.
        """
        executor = ExecutorRecord(
            executor_id=executor_id,
            executor_type=executor_type,
            account_name=account_name,
            connector_name=connector_name,
            trading_pair=trading_pair,
            controller_id=controller_id,
            config=config,
            status=status
        )
        if created_at is not None:
            executor.created_at = created_at

        self.session.add(executor)
        await self.session.flush()
        await self.session.refresh(executor)
        return executor

    async def update_executor(
            self,
            executor_id: str,
            status: Optional[str] = None,
            close_type: Optional[str] = None,
            net_pnl_quote: Optional[Decimal] = None,
            net_pnl_pct: Optional[Decimal] = None,
            cum_fees_quote: Optional[Decimal] = None,
            filled_amount_quote: Optional[Decimal] = None,
            final_state: Optional[str] = None,
            error_log: Optional[str] = None
    ) -> Optional[ExecutorRecord]:
        """Update an executor record."""
        stmt = select(ExecutorRecord).where(ExecutorRecord.executor_id == executor_id)
        result = await self.session.execute(stmt)
        executor = result.scalar_one_or_none()

        if executor:
            if status is not None:
                executor.status = status
            if close_type is not None:
                executor.close_type = close_type
                executor.closed_at = datetime.now(timezone.utc)
            if net_pnl_quote is not None:
                executor.net_pnl_quote = net_pnl_quote
            if net_pnl_pct is not None:
                executor.net_pnl_pct = net_pnl_pct
            if cum_fees_quote is not None:
                executor.cum_fees_quote = cum_fees_quote
            if filled_amount_quote is not None:
                executor.filled_amount_quote = filled_amount_quote
            if final_state is not None:
                executor.final_state = final_state
            if error_log is not None:
                executor.error_log = error_log

            await self.session.flush()
            await self.session.refresh(executor)

        return executor

    async def upsert_executor_completion(
            self,
            executor_id: str,
            executor_type: str,
            account_name: str,
            connector_name: str,
            trading_pair: str,
            controller_id: str = "main",
            config: Optional[str] = None,
            created_at: Optional[datetime] = None,
            status: Optional[str] = None,
            close_type: Optional[str] = None,
            net_pnl_quote: Optional[Decimal] = None,
            net_pnl_pct: Optional[Decimal] = None,
            cum_fees_quote: Optional[Decimal] = None,
            filled_amount_quote: Optional[Decimal] = None,
            final_state: Optional[str] = None,
            error_log: Optional[str] = None
    ) -> Tuple[Optional[ExecutorRecord], bool]:
        """Write an executor's final state, creating its row if it is not there yet.

        `update_executor` is select-then-update, so it silently does nothing when the
        creation INSERT has not landed: an executor that closes milliseconds after
        start can have its completion written before (or instead of) its creation row,
        and the final state would just be dropped, leaving a phantom RUNNING executor.
        This is the same insert-or-update shape `upsert_position_hold` already uses.

        Returns (record, created) where `created` is True if the row had to be
        repaired — the caller is expected to log that, it is never normal.
        """
        completion = dict(
            status=status,
            close_type=close_type,
            net_pnl_quote=net_pnl_quote,
            net_pnl_pct=net_pnl_pct,
            cum_fees_quote=cum_fees_quote,
            filled_amount_quote=filled_amount_quote,
            final_state=final_state,
            error_log=error_log,
        )

        executor = await self.update_executor(executor_id=executor_id, **completion)
        if executor is not None:
            return executor, False

        # No row: insert one from the metadata the caller carries. A creation INSERT
        # racing us in another session can still win between our SELECT and this
        # INSERT, so do it in a SAVEPOINT and fall back to the update — the outer
        # transaction stays usable either way.
        created = True
        try:
            async with self.session.begin_nested():
                await self.create_executor(
                    executor_id=executor_id,
                    executor_type=executor_type,
                    account_name=account_name,
                    connector_name=connector_name,
                    trading_pair=trading_pair,
                    config=config,
                    status=status or "RUNNING",
                    controller_id=controller_id,
                    created_at=created_at,
                )
        except IntegrityError:
            created = False

        executor = await self.update_executor(executor_id=executor_id, **completion)
        return executor, created

    async def get_executor_by_id(self, executor_id: str) -> Optional[ExecutorRecord]:
        """Get an executor by ID."""
        stmt = select(ExecutorRecord).where(ExecutorRecord.executor_id == executor_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_executors(
            self,
            account_name: Optional[str] = None,
            connector_name: Optional[str] = None,
            trading_pair: Optional[str] = None,
            executor_type: Optional[str] = None,
            status: Optional[str] = None,
            controller_id: Optional[str] = None,
            limit: Optional[int] = 100,
            offset: int = 0
    ) -> List[ExecutorRecord]:
        """Get executors with optional filters."""
        stmt = select(ExecutorRecord)

        conditions = []
        if account_name:
            conditions.append(ExecutorRecord.account_name == account_name)
        if connector_name:
            conditions.append(ExecutorRecord.connector_name == connector_name)
        if trading_pair:
            conditions.append(ExecutorRecord.trading_pair == trading_pair)
        if executor_type:
            conditions.append(ExecutorRecord.executor_type == executor_type)
        if status:
            conditions.append(ExecutorRecord.status == status)
        if controller_id:
            conditions.append(ExecutorRecord.controller_id == controller_id)

        if conditions:
            stmt = stmt.where(and_(*conditions))

        stmt = stmt.order_by(desc(ExecutorRecord.created_at)).offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_executors_by_close_types(
            self,
            close_types: List[str],
            executor_type: Optional[str] = None,
            limit: Optional[int] = 500,
    ) -> List[ExecutorRecord]:
        """Get executors whose close_type is one of the given values, newest first.

        Filter by executor_type in SQL where possible: applying `limit` to a broad
        candidate set and filtering in Python can silently drop the very rows the
        caller is looking for once the newest N candidates are all irrelevant.
        """
        stmt = select(ExecutorRecord).where(ExecutorRecord.close_type.in_(close_types))
        if executor_type:
            stmt = stmt.where(ExecutorRecord.executor_type == executor_type)
        stmt = stmt.order_by(desc(ExecutorRecord.created_at))
        if limit is not None:
            stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_active_executors(
            self,
            account_name: Optional[str] = None,
            connector_name: Optional[str] = None
    ) -> List[ExecutorRecord]:
        """Get all active (running) executors."""
        stmt = select(ExecutorRecord).where(ExecutorRecord.status == "RUNNING")

        if account_name:
            stmt = stmt.where(ExecutorRecord.account_name == account_name)
        if connector_name:
            stmt = stmt.where(ExecutorRecord.connector_name == connector_name)

        stmt = stmt.order_by(desc(ExecutorRecord.created_at))

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_position_hold_executors(
            self,
            account_name: Optional[str] = None,
            connector_name: Optional[str] = None,
            trading_pair: Optional[str] = None,
            controller_id: Optional[str] = None
    ) -> List[ExecutorRecord]:
        """Get executors that closed with POSITION_HOLD (keep_position=True)."""
        stmt = select(ExecutorRecord).where(ExecutorRecord.close_type == "POSITION_HOLD")

        conditions = []
        if account_name:
            conditions.append(ExecutorRecord.account_name == account_name)
        if connector_name:
            conditions.append(ExecutorRecord.connector_name == connector_name)
        if trading_pair:
            conditions.append(ExecutorRecord.trading_pair == trading_pair)
        if controller_id:
            conditions.append(ExecutorRecord.controller_id == controller_id)

        if conditions:
            stmt = stmt.where(and_(*conditions))

        stmt = stmt.order_by(desc(ExecutorRecord.created_at))

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # ========================================
    # PositionHoldRecord Operations
    # ========================================

    async def upsert_position_hold(
            self,
            account_name: str,
            connector_name: str,
            trading_pair: str,
            controller_id: str,
            buy_amount_base: Decimal,
            buy_amount_quote: Decimal,
            sell_amount_base: Decimal,
            sell_amount_quote: Decimal,
            realized_pnl_quote: Decimal,
            cum_fees_quote: Decimal = Decimal("0"),
            executor_ids: List[str] = None
    ) -> PositionHoldRecord:
        """Create or update a position hold record."""
        import json as _json
        if executor_ids is None:
            executor_ids = []

        stmt = select(PositionHoldRecord).where(and_(
            PositionHoldRecord.account_name == account_name,
            PositionHoldRecord.connector_name == connector_name,
            PositionHoldRecord.trading_pair == trading_pair,
            PositionHoldRecord.controller_id == controller_id,
            PositionHoldRecord.status == "ACTIVE",
        ))
        result = await self.session.execute(stmt)
        record = result.scalar_one_or_none()

        if record:
            record.buy_amount_base = buy_amount_base
            record.buy_amount_quote = buy_amount_quote
            record.sell_amount_base = sell_amount_base
            record.sell_amount_quote = sell_amount_quote
            record.realized_pnl_quote = realized_pnl_quote
            record.cum_fees_quote = cum_fees_quote
            record.executor_ids = _json.dumps(executor_ids)
        else:
            record = PositionHoldRecord(
                account_name=account_name,
                connector_name=connector_name,
                trading_pair=trading_pair,
                controller_id=controller_id,
                buy_amount_base=buy_amount_base,
                buy_amount_quote=buy_amount_quote,
                sell_amount_base=sell_amount_base,
                sell_amount_quote=sell_amount_quote,
                realized_pnl_quote=realized_pnl_quote,
                cum_fees_quote=cum_fees_quote,
                executor_ids=_json.dumps(executor_ids),
                status="ACTIVE",
            )
            self.session.add(record)

        await self.session.flush()
        return record

    async def get_active_position_holds(
            self,
            account_name: Optional[str] = None,
            connector_name: Optional[str] = None,
            trading_pair: Optional[str] = None,
            controller_id: Optional[str] = None,
    ) -> List[PositionHoldRecord]:
        """Get all ACTIVE position hold records."""
        stmt = select(PositionHoldRecord).where(PositionHoldRecord.status == "ACTIVE")

        conditions = []
        if account_name:
            conditions.append(PositionHoldRecord.account_name == account_name)
        if connector_name:
            conditions.append(PositionHoldRecord.connector_name == connector_name)
        if trading_pair:
            conditions.append(PositionHoldRecord.trading_pair == trading_pair)
        if controller_id:
            conditions.append(PositionHoldRecord.controller_id == controller_id)

        if conditions:
            stmt = stmt.where(and_(*conditions))

        stmt = stmt.order_by(desc(PositionHoldRecord.last_updated))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def clear_position_hold(
            self,
            account_name: str,
            connector_name: str,
            trading_pair: str,
            controller_id: str = "main"
    ) -> bool:
        """Mark a position hold as CLEARED."""
        from sqlalchemy import update as sa_update

        stmt = (
            sa_update(PositionHoldRecord)
            .where(and_(
                PositionHoldRecord.account_name == account_name,
                PositionHoldRecord.connector_name == connector_name,
                PositionHoldRecord.trading_pair == trading_pair,
                PositionHoldRecord.controller_id == controller_id,
                PositionHoldRecord.status == "ACTIVE",
            ))
            .values(status="CLEARED", cleared_at=datetime.now(timezone.utc))
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0

    async def get_executor_stats(self) -> Dict[str, Any]:
        """Get statistics about executors."""
        # Total executors
        total_stmt = select(func.count(ExecutorRecord.id))
        total_result = await self.session.execute(total_stmt)
        total_executors = total_result.scalar() or 0

        # Active executors
        active_stmt = select(func.count(ExecutorRecord.id)).where(
            ExecutorRecord.status == "RUNNING"
        )
        active_result = await self.session.execute(active_stmt)
        active_executors = active_result.scalar() or 0

        # Total PnL
        pnl_stmt = select(func.sum(ExecutorRecord.net_pnl_quote))
        pnl_result = await self.session.execute(pnl_stmt)
        total_pnl = pnl_result.scalar() or Decimal("0")

        # Total volume — the volume generated, not the capital deployed.
        volume_stmt = select(func.sum(ExecutorRecord.filled_amount_quote))
        volume_result = await self.session.execute(volume_stmt)
        total_volume = volume_result.scalar() or Decimal("0")

        # Executors by type
        type_stmt = select(
            ExecutorRecord.executor_type,
            func.count(ExecutorRecord.id).label('count')
        ).group_by(ExecutorRecord.executor_type)
        type_result = await self.session.execute(type_stmt)
        type_counts = {row.executor_type: row.count for row in type_result}

        # Executors by status
        status_stmt = select(
            ExecutorRecord.status,
            func.count(ExecutorRecord.id).label('count')
        ).group_by(ExecutorRecord.status)
        status_result = await self.session.execute(status_stmt)
        status_counts = {row.status: row.count for row in status_result}

        # Executors by connector
        connector_stmt = select(
            ExecutorRecord.connector_name,
            func.count(ExecutorRecord.id).label('count')
        ).group_by(ExecutorRecord.connector_name)
        connector_result = await self.session.execute(connector_stmt)
        connector_counts = {row.connector_name: row.count for row in connector_result}

        return {
            "total_executors": total_executors,
            "active_executors": active_executors,
            "total_pnl_quote": float(total_pnl),
            "total_volume_quote": float(total_volume),
            "type_counts": type_counts,
            "status_counts": status_counts,
            "connector_counts": connector_counts
        }

    async def get_performance_report(
            self,
            controller_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get a performance report, optionally filtered by controller_id.

        Returns aggregate metrics: total executors, PnL, fees, volume,
        win rate, PnL dispersion (for Sharpe), and breakdown by type.

        Every metric is an aggregate: the number of rows this returns does not
        grow with the size of the executors table. It is polled by the
        `/ws/executors` performance push loop, once per interval per subscriber.
        """
        base_filter = []
        if controller_id:
            base_filter.append(ExecutorRecord.controller_id == controller_id)

        # --- Status counts ---
        status_stmt = select(
            ExecutorRecord.status,
            func.count(ExecutorRecord.id).label("cnt"),
        ).group_by(ExecutorRecord.status)
        if base_filter:
            status_stmt = status_stmt.where(and_(*base_filter))
        status_rows = await self.session.execute(status_stmt)
        status_counts = {r.status: r.cnt for r in status_rows}

        total_executors = sum(status_counts.values())

        # --- Aggregate PnL / fees / volume (completed only, excluding POSITION_HOLD to avoid double-counting) ---
        completed_filter = base_filter + [
            ExecutorRecord.status != "RUNNING",
            ExecutorRecord.close_type != "POSITION_HOLD",
        ]
        agg_stmt = select(
            func.coalesce(func.sum(ExecutorRecord.net_pnl_quote), Decimal(0)).label("pnl"),
            func.coalesce(func.sum(ExecutorRecord.cum_fees_quote), Decimal(0)).label("fees"),
            func.coalesce(func.sum(ExecutorRecord.filled_amount_quote), Decimal(0)).label("vol"),
            func.coalesce(func.avg(ExecutorRecord.net_pnl_pct), Decimal(0)).label("pnl_pct_avg"),
            func.coalesce(
                func.sum(ExecutorRecord.net_pnl_quote * ExecutorRecord.net_pnl_quote),
                Decimal(0),
            ).label("pnl_sq"),
            func.count(ExecutorRecord.id).label("completed_count"),
            func.sum(case(
                (ExecutorRecord.net_pnl_quote > 0, 1),
                else_=0,
            )).label("wins"),
        ).where(and_(*completed_filter))
        agg_row = (await self.session.execute(agg_stmt)).one()

        completed_count = agg_row.completed_count or 0
        wins = agg_row.wins or 0
        win_rate = (wins / completed_count) if completed_count > 0 else 0.0

        # --- PnL dispersion for Sharpe, from the aggregates rather than the rows ---
        # Sample standard deviation out of the count, the sum and the sum of squares the
        # query above already carries. Fetching one net_pnl_quote per completed executor
        # to do this in Python cost a full table scan on every poll of this report.
        # NULL PnL counts as zero, as the old per-row list did: SUM skips it, COUNT does not.
        # The moments stay Decimal until the square root: a large mean with a small spread
        # loses its whole variance to cancellation if the subtraction is done in float.
        pnl_std = None
        if completed_count >= 2:
            variance = (
                agg_row.pnl_sq - agg_row.pnl * agg_row.pnl / completed_count
            ) / (completed_count - 1)
            pnl_std = math.sqrt(max(float(variance), 0.0))  # max() clamps noise at zero variance

        # --- Breakdown by executor type (also excluding POSITION_HOLD to match aggregate totals) ---
        type_stmt = select(
            ExecutorRecord.executor_type,
            func.count(ExecutorRecord.id).label("total"),
            func.sum(case(
                (ExecutorRecord.status != "RUNNING", 1),
                else_=0,
            )).label("completed"),
            func.sum(case(
                (ExecutorRecord.status == "RUNNING", 1),
                else_=0,
            )).label("running"),
            func.coalesce(func.sum(ExecutorRecord.net_pnl_quote), Decimal(0)).label("pnl"),
            func.coalesce(func.sum(ExecutorRecord.filled_amount_quote), Decimal(0)).label("vol"),
            func.coalesce(func.sum(ExecutorRecord.cum_fees_quote), Decimal(0)).label("fees"),
        ).where(
            and_(*completed_filter)
        ).group_by(ExecutorRecord.executor_type)
        type_rows = await self.session.execute(type_stmt)
        by_type = [
            {
                "executor_type": r.executor_type,
                "total": r.total,
                "completed": r.completed or 0,
                "running": r.running or 0,
                "pnl_quote": float(r.pnl),
                "volume_quote": float(r.vol),
                "fees_quote": float(r.fees),
            }
            for r in type_rows
        ]

        return {
            "total_executors": total_executors,
            "status_counts": status_counts,
            "pnl_total_quote": float(agg_row.pnl),
            "pnl_pct_avg": float(agg_row.pnl_pct_avg),
            "fees_total_quote": float(agg_row.fees),
            "volume_total_quote": float(agg_row.vol),
            "win_rate": win_rate,
            "completed_count": completed_count,
            "pnl_std": pnl_std,
            "by_type": by_type,
        }

    # ========================================
    # ExecutorOrder Operations
    # ========================================

    async def create_executor_order(
            self,
            executor_id: str,
            client_order_id: str,
            order_type: str,
            trade_type: str,
            amount: Decimal,
            price: Optional[Decimal] = None,
            exchange_order_id: Optional[str] = None,
            status: str = "SUBMITTED"
    ) -> ExecutorOrder:
        """Create a new executor order record."""
        order = ExecutorOrder(
            executor_id=executor_id,
            client_order_id=client_order_id,
            order_type=order_type,
            trade_type=trade_type,
            amount=amount,
            price=price,
            exchange_order_id=exchange_order_id,
            status=status
        )

        self.session.add(order)
        await self.session.flush()
        await self.session.refresh(order)
        return order

    async def update_executor_order(
            self,
            client_order_id: str,
            status: Optional[str] = None,
            filled_amount: Optional[Decimal] = None,
            average_fill_price: Optional[Decimal] = None,
            exchange_order_id: Optional[str] = None
    ) -> Optional[ExecutorOrder]:
        """Update an executor order record."""
        stmt = select(ExecutorOrder).where(ExecutorOrder.client_order_id == client_order_id)
        result = await self.session.execute(stmt)
        order = result.scalar_one_or_none()

        if order:
            if status is not None:
                order.status = status
            if filled_amount is not None:
                order.filled_amount = filled_amount
            if average_fill_price is not None:
                order.average_fill_price = average_fill_price
            if exchange_order_id is not None:
                order.exchange_order_id = exchange_order_id

            await self.session.flush()
            await self.session.refresh(order)

        return order

    async def get_executor_orders(
            self,
            executor_id: str,
            status: Optional[str] = None
    ) -> List[ExecutorOrder]:
        """Get orders for an executor."""
        stmt = select(ExecutorOrder).where(ExecutorOrder.executor_id == executor_id)

        if status:
            stmt = stmt.where(ExecutorOrder.status == status)

        stmt = stmt.order_by(desc(ExecutorOrder.created_at))

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_order_by_client_id(self, client_order_id: str) -> Optional[ExecutorOrder]:
        """Get an order by client order ID."""
        stmt = select(ExecutorOrder).where(ExecutorOrder.client_order_id == client_order_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def cleanup_orphaned_executors(
            self,
            active_executor_ids: List[str],
            close_type: str = "SYSTEM_CLEANUP"
    ) -> int:
        """
        Clean up orphaned executors - those marked as RUNNING but not in active memory.

        Each terminated row takes the metrics of its most recent performance snapshot.
        A running executor's row carries the zeros it was INSERTed with -- the live
        figures only exist in ExecutorService memory until it completes -- so terminating
        it without them booked every executor that was live at a restart at 0 PnL, 0 fees
        and 0 volume, permanently, and /executors/performance summed those zeros forever.
        The snapshot series is what makes the real figures recoverable here.

        An executor with no snapshot (created and orphaned inside one snapshot interval)
        keeps the old behaviour: there is nothing better to write. The adopted figures are
        the last observed ones rather than the true final ones, which is why the row is
        still marked SYSTEM_CLEANUP -- a reader can tell an approximated close from a
        clean one.

        Args:
            active_executor_ids: List of executor IDs currently active in memory
            close_type: Close type to set for cleaned up executors
        Returns:
            Number of executors cleaned up
        """
        # Find executors that are RUNNING but not in the active list
        conditions = [ExecutorRecord.status == "RUNNING"]

        if active_executor_ids:
            conditions.append(~ExecutorRecord.executor_id.in_(active_executor_ids))

        orphaned = (await self.session.execute(
            select(ExecutorRecord).where(and_(*conditions))
        )).scalars().all()

        if not orphaned:
            return 0

        latest_snapshots = await ExecutorPerformanceRepository(self.session).get_latest_for(
            [record.executor_id for record in orphaned]
        )

        closed_at = datetime.now(timezone.utc)
        for record in orphaned:
            record.status = "TERMINATED"
            record.close_type = close_type
            record.closed_at = closed_at

            snapshot = latest_snapshots.get(record.executor_id)
            if snapshot:
                record.net_pnl_quote = Decimal(str(snapshot["net_pnl_quote"]))
                record.net_pnl_pct = Decimal(str(snapshot["net_pnl_pct"]))
                record.cum_fees_quote = Decimal(str(snapshot["cum_fees_quote"]))
                record.filled_amount_quote = Decimal(str(snapshot["filled_amount_quote"]))

        await self.session.flush()

        return len(orphaned)
