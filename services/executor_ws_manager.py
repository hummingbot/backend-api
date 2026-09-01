"""
WebSocket manager for executor/controller data streaming.

Provides real-time push updates for executor status, performance,
positions, summary, and logs via WebSocket subscriptions.
"""
import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Awaitable, Callable, Dict, Optional

from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect

from config import settings
from services.bots_orchestrator import BotsOrchestrator
from services.executor_service import ExecutorService
from services.market_data_service import MarketDataService
from utils.trading_pair import InvalidTradingPair, split_trading_pair

logger = logging.getLogger(__name__)

SUBSCRIPTION_TYPES = {
    "executors",
    "executor_detail",
    "executor_summary",
    "performance",
    "positions",
    "executor_logs",
    "bot_status",
    "all_bots_status",
}


@dataclass
class ExecutorSubscription:
    """Tracks a single WebSocket subscription."""
    sub_id: str
    sub_type: str
    update_interval: float
    task: Optional[asyncio.Task] = None

    # For executors subscription
    filters: Dict[str, Any] = field(default_factory=dict)

    # For executor_detail / executor_logs
    executor_id: Optional[str] = None

    # For bot_status
    bot_name: Optional[str] = None

    # For performance / positions
    controller_id: Optional[str] = None

    # For executor_logs
    log_level: Optional[str] = None
    log_limit: int = 100

    # Change detection
    last_sent_hash: Optional[str] = None
    # For logs: track count to send only new entries
    last_log_count: int = 0


# A fetcher turns a subscription into the payload to hash and push; an extra
# builder derives additional top-level frame keys from that payload.
FetchFn = Callable[["ExecutorSubscription"], Awaitable[Any]]
ExtraFn = Callable[[Any], Dict[str, Any]]


@dataclass(frozen=True)
class PushSpec:
    """How one hash-and-push subscription type is fetched and framed."""
    fetch: FetchFn
    msg_type: str
    extra: Optional[ExtraFn] = None


def _compute_hash(data: Any) -> str:
    """MD5 hash of JSON-serialized data for change detection."""
    raw = json.dumps(data, sort_keys=True, default=str)
    return hashlib.md5(raw.encode()).hexdigest()


def _clamp_interval(interval: Optional[float]) -> float:
    """Clamp update interval to the configured executor WebSocket range."""
    md = settings.market_data
    if interval is None:
        return md.ws_executor_default_update_interval
    return max(
        md.ws_executor_min_update_interval,
        min(md.ws_executor_max_update_interval, interval),
    )


class ExecutorWebSocketManager:
    """
    Manages WebSocket subscriptions for executor data.

    Each subscription spawns an asyncio task that polls the relevant
    ExecutorService method, computes a hash for change detection,
    and pushes updates only when data changes.
    """

    def __init__(
        self,
        executor_service: ExecutorService,
        market_data_service: MarketDataService,
        bots_orchestrator: Optional[BotsOrchestrator] = None,
    ):
        self._executor_service = executor_service
        self._market_data_service = market_data_service
        self._bots_orchestrator = bots_orchestrator
        # conn_id -> {sub_id -> ExecutorSubscription}
        self._subscriptions: Dict[str, Dict[str, ExecutorSubscription]] = {}

    async def handle_subscribe(
        self, conn_id: str, websocket: WebSocket, msg: Dict[str, Any]
    ) -> None:
        """Handle a subscribe message from the client."""
        sub_type = msg.get("type")
        if sub_type not in SUBSCRIPTION_TYPES:
            await self._send_error(
                websocket,
                f"Unknown subscription type: {sub_type}. "
                f"Valid types: {sorted(SUBSCRIPTION_TYPES)}",
            )
            return

        interval = _clamp_interval(msg.get("update_interval"))

        # Build subscription
        sub = ExecutorSubscription(
            sub_id="",  # set below
            sub_type=sub_type,
            update_interval=interval,
        )

        if sub_type == "executors":
            filters = msg.get("filters", {})
            sub.filters = filters
            # Hash the filters for a stable sub ID
            fh = _compute_hash(filters)[:8]
            sub.sub_id = f"executors_{fh}"

        elif sub_type == "executor_detail":
            executor_id = msg.get("executor_id")
            if not executor_id:
                await self._send_error(websocket, "executor_detail requires 'executor_id'")
                return
            sub.executor_id = executor_id
            sub.sub_id = f"executor_detail_{executor_id}"

        elif sub_type == "executor_summary":
            sub.sub_id = "executor_summary"

        elif sub_type == "performance":
            sub.controller_id = msg.get("controller_id")
            cid = sub.controller_id or "all"
            sub.sub_id = f"performance_{cid}"

        elif sub_type == "positions":
            sub.controller_id = msg.get("controller_id")
            cid = sub.controller_id or "all"
            sub.sub_id = f"positions_{cid}"

        elif sub_type == "executor_logs":
            executor_id = msg.get("executor_id")
            if not executor_id:
                await self._send_error(websocket, "executor_logs requires 'executor_id'")
                return
            sub.executor_id = executor_id
            sub.log_level = msg.get("level")
            sub.log_limit = msg.get("limit", 100)
            sub.sub_id = f"executor_logs_{executor_id}"

        elif sub_type == "bot_status":
            bot_name = msg.get("bot_name")
            if not bot_name:
                await self._send_error(websocket, "bot_status requires 'bot_name'")
                return
            if not self._bots_orchestrator:
                await self._send_error(websocket, "Bot orchestrator not available")
                return
            sub.bot_name = bot_name
            sub.sub_id = f"bot_status_{bot_name}"

        elif sub_type == "all_bots_status":
            if not self._bots_orchestrator:
                await self._send_error(websocket, "Bot orchestrator not available")
                return
            sub.sub_id = "all_bots_status"

        # Cancel existing subscription with same ID for this connection
        conn_subs = self._subscriptions.setdefault(conn_id, {})
        if sub.sub_id in conn_subs:
            old = conn_subs[sub.sub_id]
            if old.task and not old.task.done():
                old.task.cancel()

        # Spawn push loop
        push_fn = self._get_push_fn(sub_type)
        sub.task = asyncio.create_task(
            push_fn(conn_id, websocket, sub),
            name=f"ws-executor-{conn_id}-{sub.sub_id}",
        )
        conn_subs[sub.sub_id] = sub

        await websocket.send_json({
            "type": "subscribed",
            "subscription_id": sub.sub_id,
            "subscription_type": sub_type,
            "update_interval": interval,
        })
        logger.info(f"[WS-Exec] {conn_id} subscribed to {sub.sub_id}")

    async def handle_unsubscribe(
        self, conn_id: str, websocket: WebSocket, sub_id: str
    ) -> None:
        """Handle an unsubscribe message from the client."""
        conn_subs = self._subscriptions.get(conn_id, {})
        sub = conn_subs.pop(sub_id, None)
        if sub:
            if sub.task and not sub.task.done():
                sub.task.cancel()
            await websocket.send_json({
                "type": "unsubscribed",
                "subscription_id": sub_id,
            })
            logger.info(f"[WS-Exec] {conn_id} unsubscribed from {sub_id}")
        else:
            await self._send_error(websocket, f"No subscription found: {sub_id}")

    def remove_connection(self, conn_id: str) -> None:
        """Clean up all subscriptions for a disconnected client."""
        conn_subs = self._subscriptions.pop(conn_id, {})
        for sub in conn_subs.values():
            if sub.task and not sub.task.done():
                sub.task.cancel()
        if conn_subs:
            logger.info(
                f"[WS-Exec] Cleaned up {len(conn_subs)} subscriptions for {conn_id}"
            )

    async def shutdown(self) -> None:
        """Cancel all subscription tasks across all connections."""
        for conn_id in list(self._subscriptions.keys()):
            self.remove_connection(conn_id)
        logger.info("[WS-Exec] Shutdown complete")

    # ------------------------------------------------------------------
    # Push loop dispatch
    # ------------------------------------------------------------------

    def _push_specs(self) -> Dict[str, PushSpec]:
        """Map every hash-and-push subscription type to how it is fetched and framed."""
        return {
            "executors": PushSpec(
                fetch=self._fetch_executors,
                msg_type="executors",
                extra=lambda data: {"total_count": len(data)},
            ),
            "executor_detail": PushSpec(
                fetch=self._fetch_executor_detail,
                msg_type="executor_detail",
            ),
            "executor_summary": PushSpec(
                fetch=self._fetch_summary,
                msg_type="executor_summary",
            ),
            "performance": PushSpec(
                fetch=self._fetch_performance,
                msg_type="performance",
            ),
            "positions": PushSpec(
                fetch=self._fetch_positions,
                msg_type="positions",
            ),
            "bot_status": PushSpec(
                fetch=self._fetch_bot_status,
                msg_type="bot_status",
            ),
            "all_bots_status": PushSpec(
                fetch=self._fetch_all_bots_status,
                msg_type="all_bots_status",
                extra=lambda data: {"bot_count": len(data)},
            ),
        }

    def _get_push_fn(self, sub_type: str):
        """Resolve a subscription type to the coroutine function that drives its loop."""
        if sub_type == "executor_logs":
            # Logs key on last_log_count, not on a payload hash — its own loop.
            return self._logs_push_loop
        spec = self._push_specs()[sub_type]
        return partial(
            self._push_loop,
            fetch=spec.fetch,
            msg_type=spec.msg_type,
            extra=spec.extra,
        )

    # ------------------------------------------------------------------
    # Push loops
    # ------------------------------------------------------------------

    async def _push_loop(
        self,
        conn_id: str,
        websocket: WebSocket,
        sub: ExecutorSubscription,
        fetch: FetchFn,
        msg_type: str,
        extra: Optional[ExtraFn] = None,
    ) -> None:
        """Poll `fetch` on the subscription interval and push only when the data changes."""
        try:
            while True:
                try:
                    data = await fetch(sub)
                    h = _compute_hash(data)
                    if h != sub.last_sent_hash:
                        sub.last_sent_hash = h
                        message = {
                            "type": msg_type,
                            "subscription_id": sub.sub_id,
                            "data": data,
                        }
                        if extra is not None:
                            message.update(extra(data))
                        message["timestamp"] = time.time()
                        if not await self._send_or_stop(conn_id, websocket, sub, msg_type, message):
                            break
                except Exception as e:
                    logger.error(f"[WS-Exec] {msg_type} push error: {e}", exc_info=True)
                await asyncio.sleep(sub.update_interval)
        except asyncio.CancelledError:
            pass

    async def _logs_push_loop(
        self, conn_id: str, websocket: WebSocket, sub: ExecutorSubscription
    ) -> None:
        """Poll get_executor_logs() and push only new entries."""
        try:
            while True:
                try:
                    all_logs = self._executor_service.get_executor_logs(
                        sub.executor_id,
                        level=sub.log_level,
                        limit=sub.log_limit,
                    )
                    current_count = len(all_logs)
                    if current_count > sub.last_log_count:
                        new_logs = all_logs[sub.last_log_count:]
                        sub.last_log_count = current_count
                        message = {
                            "type": "executor_logs",
                            "subscription_id": sub.sub_id,
                            "data": new_logs,
                            "total_count": current_count,
                            "timestamp": time.time(),
                        }
                        if not await self._send_or_stop(
                            conn_id, websocket, sub, "executor_logs", message
                        ):
                            break
                except Exception as e:
                    logger.error(f"[WS-Exec] executor_logs push error: {e}", exc_info=True)
                await asyncio.sleep(sub.update_interval)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Fetchers
    #
    # One per hash-and-push subscription type. They normalise the mixed
    # sync/async service calls and own any per-type payload shaping, so the
    # generic loop above only ever sees "await fetch(sub) -> data".
    # ------------------------------------------------------------------

    async def _fetch_executors(self, sub: ExecutorSubscription) -> Any:
        filters = sub.filters
        return await self._executor_service.get_executors(
            account_name=filters.get("account_name"),
            connector_name=filters.get("connector_name"),
            trading_pair=filters.get("trading_pair"),
            executor_type=filters.get("executor_type"),
            status=filters.get("status"),
            controller_id=filters.get("controller_id"),
        )

    async def _fetch_executor_detail(self, sub: ExecutorSubscription) -> Any:
        return await self._executor_service.get_executor(sub.executor_id)

    async def _fetch_summary(self, sub: ExecutorSubscription) -> Any:
        return self._executor_service.get_summary()

    async def _fetch_performance(self, sub: ExecutorSubscription) -> Any:
        return await self._executor_service.get_performance_report(
            controller_id=sub.controller_id,
            market_data_service=self._market_data_service,
        )

    async def _fetch_positions(self, sub: ExecutorSubscription) -> Dict[str, Any]:
        """Positions held, enriched with unrealized PnL from the current rates."""
        positions = self._executor_service.get_positions_held(
            controller_id=sub.controller_id,
        )
        position_dicts = []
        total_realized = 0.0
        total_unrealized = None

        for p in positions:
            unrealized_pnl = None
            # See routers/executors.py: a hyphenated base symbol failed the
            # old length check and dropped the PnL without saying so.
            try:
                base, quote = split_trading_pair(p.trading_pair)
            except InvalidTradingPair:
                base = quote = None
            if base and quote:
                rate = self._market_data_service.get_rate(base, quote)
                if rate is not None:
                    unrealized_pnl = float(p.get_unrealized_pnl(rate))
                    if total_unrealized is None:
                        total_unrealized = 0.0
                    total_unrealized += unrealized_pnl

            total_realized += float(p.realized_pnl_quote)
            position_dicts.append({
                "trading_pair": p.trading_pair,
                "connector_name": p.connector_name,
                "account_name": p.account_name,
                "controller_id": p.controller_id,
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
                "unrealized_pnl_quote": unrealized_pnl,
                "executor_count": len(p.executor_ids),
                "executor_ids": p.executor_ids,
                "last_updated": p.last_updated.isoformat() if p.last_updated else None,
            })

        return {
            "total_positions": len(positions),
            "total_realized_pnl": total_realized,
            "total_unrealized_pnl": total_unrealized,
            "positions": position_dicts,
        }

    async def _fetch_bot_status(self, sub: ExecutorSubscription) -> Dict[str, Any]:
        """Single bot status, with the logs stripped out."""
        raw_status = self._bots_orchestrator.get_bot_status(sub.bot_name)
        return {
            "bot_name": sub.bot_name,
            "status": raw_status.get("status"),
            "performance": raw_status.get("performance", {}),
            "recently_active": raw_status.get("recently_active", False),
        }

    async def _fetch_all_bots_status(self, sub: ExecutorSubscription) -> Dict[str, Any]:
        """Every bot's status, with the logs stripped out of each."""
        raw = self._bots_orchestrator.get_all_bots_status()
        return {
            bot_name: {
                "status": bot_data.get("status"),
                "source": bot_data.get("source"),
                "performance": bot_data.get("performance", {}),
                "recently_active": bot_data.get("recently_active", False),
            }
            for bot_name, bot_data in raw.items()
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _send_or_stop(
        conn_id: str,
        websocket: WebSocket,
        sub: ExecutorSubscription,
        msg_type: str,
        message: Dict[str, Any],
    ) -> bool:
        """Push one frame; return False when the client is gone and the loop must stop.

        Mirrors services/websocket_manager.py, which breaks out of its push loops
        on a disconnect instead of logging an error every interval. Only the send
        is guarded: a RuntimeError raised by a fetch is a service fault, not a
        dropped client, and must stay a logged-and-retried error.
        """
        try:
            await websocket.send_json(message)
            return True
        except (WebSocketDisconnect, RuntimeError):
            logger.info(
                f"[WS-Exec] {conn_id} disconnected, stopping {msg_type} push [{sub.sub_id}]"
            )
            return False

    @staticmethod
    async def _send_error(websocket: WebSocket, message: str) -> None:
        await websocket.send_json({"type": "error", "message": message})
