import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect
from hummingbot.core.event.event_forwarder import SourceInfoEventForwarder
from hummingbot.core.event.events import OrderBookEvent, OrderBookTradeEvent
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig

from config import settings
from services.market_data_service import MarketDataService

logger = logging.getLogger(__name__)


@dataclass
class Subscription:
    subscription_id: str
    sub_type: str  # "candles", "order_book", or "trades"
    connector: str
    trading_pair: str
    update_interval: float
    # Candles-specific
    interval: Optional[str] = None
    max_records: int = 100
    # Order book-specific
    depth: int = 10
    # State tracking
    last_sent_candle_ts: Optional[float] = None
    last_sent_ob_uid: Optional[int] = None
    task: Optional[asyncio.Task] = field(default=None, repr=False)
    # Trades-specific
    event_forwarder: Optional[SourceInfoEventForwarder] = field(default=None, repr=False)
    trade_buffer: List = field(default_factory=list)


class WebSocketManager:
    def __init__(self, market_data_service: MarketDataService):
        self._market_data_service = market_data_service
        self._connections: Dict[str, Dict[str, Subscription]] = {}

    def _clamp_interval(self, interval: float) -> float:
        mn = settings.market_data.ws_min_update_interval
        mx = settings.market_data.ws_max_update_interval
        return max(mn, min(mx, interval))

    async def handle_subscribe(self, conn_id: str, websocket: WebSocket, msg: dict):
        sub_type = msg.get("type")
        connector = msg.get("connector")
        trading_pair = msg.get("trading_pair")
        update_interval = self._clamp_interval(msg.get("update_interval", 1.0))

        if sub_type not in ("candles", "order_book", "trades"):
            await self._send_error(websocket, f"Invalid subscription type: {sub_type}")
            return
        if not connector or not trading_pair:
            await self._send_error(websocket, "connector and trading_pair are required")
            return

        if sub_type == "candles":
            interval = msg.get("interval", "1m")
            max_records = msg.get("max_records", 100)
            sub_id = f"candles_{connector}_{trading_pair}_{interval}"
            sub = Subscription(
                subscription_id=sub_id,
                sub_type="candles",
                connector=connector,
                trading_pair=trading_pair,
                update_interval=update_interval,
                interval=interval,
                max_records=max_records,
            )
        elif sub_type == "order_book":
            depth = msg.get("depth", 10)
            sub_id = f"order_book_{connector}_{trading_pair}"
            sub = Subscription(
                subscription_id=sub_id,
                sub_type="order_book",
                connector=connector,
                trading_pair=trading_pair,
                update_interval=update_interval,
                depth=depth,
            )
        else:  # trades
            sub_id = f"trades_{connector}_{trading_pair}"
            sub = Subscription(
                subscription_id=sub_id,
                sub_type="trades",
                connector=connector,
                trading_pair=trading_pair,
                update_interval=update_interval,
            )

        subs = self._connections.setdefault(conn_id, {})

        # Cancel existing subscription with same id
        if sub_id in subs:
            self._cleanup_subscription(subs.pop(sub_id))

        # Start the feed / ensure it exists. For candles, creating the feed also validates
        # the trading pair on first use (cache hit afterwards); an invalid pair raises ValueError.
        try:
            if sub_type == "candles":
                config = CandlesConfig(
                    connector=connector,
                    trading_pair=trading_pair,
                    interval=sub.interval,
                    max_records=sub.max_records,
                )
                await self._market_data_service.get_candles_feed(config)
            else:
                # Both order_book and trades need the order book initialized
                await self._market_data_service.initialize_order_book(connector, trading_pair)
        except ValueError as e:
            await self._send_error(websocket, str(e))
            return
        except Exception as e:
            await self._send_error(websocket, f"Failed to start feed: {e}")
            return

        # Spawn push loop / listener
        if sub_type == "candles":
            sub.task = asyncio.create_task(self._candles_push_loop(websocket, sub))
        elif sub_type == "order_book":
            sub.task = asyncio.create_task(self._order_book_push_loop(websocket, sub))
        else:  # trades
            self._attach_trade_listener(sub)
            sub.task = asyncio.create_task(self._trades_push_loop(websocket, sub))

        subs[sub_id] = sub

        await self._send_json(websocket, {
            "type": "subscribed",
            "subscription_id": sub_id,
        })
        logger.info(f"[{conn_id}] Subscribed: {sub_id} (interval={update_interval}s)")

    async def handle_unsubscribe(self, conn_id: str, websocket: WebSocket, sub_id: str):
        subs = self._connections.get(conn_id, {})
        sub = subs.pop(sub_id, None)
        if sub:
            self._cleanup_subscription(sub)
            await self._send_json(websocket, {
                "type": "unsubscribed",
                "subscription_id": sub_id,
            })
            logger.info(f"[{conn_id}] Unsubscribed: {sub_id}")
        else:
            await self._send_error(websocket, f"Subscription not found: {sub_id}")

    def remove_connection(self, conn_id: str):
        subs = self._connections.pop(conn_id, {})
        for sub in subs.values():
            self._cleanup_subscription(sub)
        if subs:
            logger.info(f"[{conn_id}] Removed connection, cancelled {len(subs)} subscriptions")

    def _cleanup_subscription(self, sub: Subscription):
        if sub.task and not sub.task.done():
            sub.task.cancel()
        if sub.event_forwarder:
            self._detach_trade_listener(sub)

    def shutdown(self):
        for conn_id in list(self._connections.keys()):
            self.remove_connection(conn_id)
        logger.info("WebSocketManager shut down")

    # ==================== Push Loops ====================

    async def _candles_push_loop(self, websocket: WebSocket, sub: Subscription):
        try:
            config = CandlesConfig(
                connector=sub.connector,
                trading_pair=sub.trading_pair,
                interval=sub.interval,
                max_records=sub.max_records,
            )
            while True:
                await asyncio.sleep(sub.update_interval)
                try:
                    feed = await self._market_data_service.get_candles_feed(config)
                    if not feed.ready:
                        continue
                    df = feed.candles_df
                    if df is None or df.empty:
                        continue
                    latest_ts = float(df["timestamp"].iloc[-1])
                    new_candle = sub.last_sent_candle_ts is None or latest_ts != sub.last_sent_candle_ts
                    if new_candle:
                        # New candle row appeared — send full history
                        sub.last_sent_candle_ts = latest_ts
                        msg_type = "candles"
                        payload = df.tail(sub.max_records).to_dict(orient="records")
                    else:
                        # Live candle update — send only the last candle
                        msg_type = "candle_update"
                        payload = df.iloc[-1].to_dict()
                    sent = await self._send_or_stop(websocket, sub, msg_type, {
                        "type": msg_type,
                        "subscription_id": sub.subscription_id,
                        "data": payload,
                        "timestamp": time.time(),
                    })
                    if not sent:
                        break
                except Exception as e:
                    logger.error(f"Candles push error [{sub.subscription_id}]: {e}")
        except asyncio.CancelledError:
            pass

    async def _order_book_push_loop(self, websocket: WebSocket, sub: Subscription):
        try:
            while True:
                await asyncio.sleep(sub.update_interval)
                try:
                    ob = self._market_data_service.get_order_book(sub.connector, sub.trading_pair)
                    if ob is None:
                        continue
                    # Change detection via last_diff_uid (updates on every incremental diff)
                    uid = getattr(ob, "last_diff_uid", None) or getattr(ob, "snapshot_uid", None)
                    if uid is not None and sub.last_sent_ob_uid is not None and uid == sub.last_sent_ob_uid:
                        continue
                    if uid is not None:
                        sub.last_sent_ob_uid = uid

                    snapshot = ob.snapshot
                    bids = snapshot[0].head(sub.depth)[["price", "amount"]].values.tolist()
                    asks = snapshot[1].head(sub.depth)[["price", "amount"]].values.tolist()

                    sent = await self._send_or_stop(websocket, sub, "order_book", {
                        "type": "order_book",
                        "subscription_id": sub.subscription_id,
                        "data": {"bids": bids, "asks": asks},
                        "timestamp": time.time(),
                    })
                    if not sent:
                        break
                except Exception as e:
                    logger.error(f"Order book push error [{sub.subscription_id}]: {e}")
        except asyncio.CancelledError:
            pass

    # ==================== Trades ====================

    def _attach_trade_listener(self, sub: Subscription):
        ob = self._market_data_service.get_order_book(sub.connector, sub.trading_pair)
        if ob is None:
            logger.warning(f"No order book to attach trade listener for {sub.connector}/{sub.trading_pair}")
            return

        def on_trade(event_tag: int, order_book, event: OrderBookTradeEvent):
            if event.trading_pair == sub.trading_pair:
                sub.trade_buffer.append({
                    "timestamp": event.timestamp,
                    "price": float(event.price),
                    "amount": float(event.amount),
                    "side": event.type.name.lower(),
                })

        sub.event_forwarder = SourceInfoEventForwarder(on_trade)
        ob.add_listener(OrderBookEvent.TradeEvent, sub.event_forwarder)
        logger.info(f"Attached trade listener for {sub.connector}/{sub.trading_pair}")

    def _detach_trade_listener(self, sub: Subscription):
        if not sub.event_forwarder:
            return
        try:
            ob = self._market_data_service.get_order_book(sub.connector, sub.trading_pair)
            if ob:
                ob.remove_listener(OrderBookEvent.TradeEvent, sub.event_forwarder)
        except Exception as e:
            logger.error(f"Error detaching trade listener: {e}")
        sub.event_forwarder = None

    async def _trades_push_loop(self, websocket: WebSocket, sub: Subscription):
        try:
            while True:
                await asyncio.sleep(sub.update_interval)
                if not sub.trade_buffer:
                    continue
                try:
                    # Drain the buffer
                    trades = sub.trade_buffer[:]
                    sub.trade_buffer.clear()
                    sent = await self._send_or_stop(websocket, sub, "trades", {
                        "type": "trades",
                        "subscription_id": sub.subscription_id,
                        "data": trades,
                        "timestamp": time.time(),
                    })
                    if not sent:
                        break
                except Exception as e:
                    logger.error(f"Trades push error [{sub.subscription_id}]: {e}")
        except asyncio.CancelledError:
            pass

    # ==================== Helpers ====================

    @staticmethod
    async def _send_json(websocket: WebSocket, data: dict):
        await websocket.send_json(data)

    @staticmethod
    async def _send_or_stop(websocket: WebSocket, sub: Subscription, msg_type: str, message: dict) -> bool:
        """Push one frame; return False when the client is gone and the loop must stop.

        Only the send is guarded. A RuntimeError raised by a *fetch* is a service
        fault (connector still initialising, transient upstream error), not a
        dropped client, so it must stay with the caller's `except Exception`
        branch, which logs it and retries on the next interval. Mirrors
        services/executor_ws_manager.py.
        """
        try:
            await websocket.send_json(message)
            return True
        except (WebSocketDisconnect, RuntimeError):
            logger.info(f"WebSocket disconnected, stopping {msg_type} push [{sub.subscription_id}]")
            return False

    @staticmethod
    async def _send_error(websocket: WebSocket, message: str):
        await websocket.send_json({"type": "error", "message": message})

    @staticmethod
    def generate_connection_id() -> str:
        return str(uuid.uuid4())[:8]
