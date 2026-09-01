"""
WebSocket router for real-time market data and executor data streaming.
"""
import asyncio
import base64
import logging
import secrets
import time
import uuid
from typing import Optional, Tuple

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from config import settings
from services.websocket_manager import WebSocketManager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["WebSocket"])

HEARTBEAT_INTERVAL = 30  # seconds

# Subprotocol a browser client offers to carry its credentials, since the JS WebSocket API
# cannot set an Authorization header: new WebSocket(url, ["hummingbot-auth", b64url(user:pass)])
AUTH_SUBPROTOCOL = "hummingbot-auth"


def _decode_basic_credentials(encoded: str) -> Optional[Tuple[str, str]]:
    """
    Decode a ``base64(username:password)`` blob into its two halves.

    Accepts base64url as well as standard base64, with or without padding: base64url is the
    only variant whose alphabet is a valid ``Sec-WebSocket-Protocol`` token, so browser
    clients have to use it, while ``Authorization: Basic`` uses the standard alphabet.

    Returns None if the blob is not decodable or carries no ``:`` separator.
    """
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        decoded = base64.b64decode(padded.replace("-", "+").replace("_", "/")).decode("utf-8")
    except Exception:
        return None
    if ":" not in decoded:
        return None
    username, password = decoded.split(":", 1)
    return username, password


def _authenticate_websocket(websocket: WebSocket) -> Tuple[bool, Optional[str]]:
    """
    Authenticate a WebSocket handshake from its headers, before it is accepted.

    Credentials are never read from the query string: uvicorn logs the full path *with* its
    query string for every handshake, so a ``?username=``/``?password=``/``?token=`` channel
    would write the global admin credentials into access logs, traces and browser history.

    Two credential channels are supported, both header-based:
      - ``Authorization: Basic base64(username:password)`` — same channel as the HTTP routes,
        for any client that can set request headers.
      - ``Sec-WebSocket-Protocol: hummingbot-auth, base64url(username:password)`` — for
        browsers, whose ``new WebSocket(url, protocols)`` API cannot set headers but can
        offer subprotocols.

    Returns ``(authenticated, subprotocol)``. When credentials arrived over the subprotocol
    channel the selected subprotocol must be echoed back in ``websocket.accept()``, otherwise
    the browser fails the connection itself.
    """
    credentials: Optional[Tuple[str, str]] = None
    subprotocol: Optional[str] = None

    auth_header = websocket.headers.get("authorization", "")
    if auth_header.startswith("Basic "):
        credentials = _decode_basic_credentials(auth_header[6:])
    else:
        offered = [
            protocol.strip()
            for protocol in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if protocol.strip()
        ]
        if len(offered) >= 2 and offered[0] == AUTH_SUBPROTOCOL:
            credentials = _decode_basic_credentials(offered[1])
            subprotocol = AUTH_SUBPROTOCOL

    if credentials is None:
        return False, None

    ws_user, ws_pass = credentials
    correct_user = secrets.compare_digest(
        ws_user.encode(), settings.security.username.encode()
    )
    correct_pass = secrets.compare_digest(
        ws_pass.encode(), settings.security.password.encode()
    )
    return bool(correct_user and correct_pass), subprotocol


async def _reject_unauthenticated(websocket: WebSocket) -> None:
    """
    Refuse the handshake itself instead of accepting it and closing with 4001.

    An unauthenticated peer never reaches an open WebSocket: it gets an HTTP 401 handshake
    response where the server supports the ASGI websocket denial-response extension, and a
    1008 policy-violation close (which the server turns into an HTTP 403) where it does not.
    """
    if websocket.client_state == WebSocketState.CONNECTING:
        await websocket.receive()

    if "websocket.http.response" in (websocket.scope.get("extensions") or {}):
        await websocket.send({
            "type": "websocket.http.response.start",
            "status": 401,
            "headers": [
                (b"www-authenticate", b'Basic realm="hummingbot-api"'),
                (b"content-type", b"text/plain; charset=utf-8"),
            ],
        })
        await websocket.send({
            "type": "websocket.http.response.body",
            "body": b"Authentication failed",
        })
    else:
        await websocket.close(code=1008, reason="Authentication failed")


async def _heartbeat_loop(websocket: WebSocket) -> None:
    """Send periodic heartbeat pings."""
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await websocket.send_json({
                "type": "heartbeat",
                "timestamp": time.time(),
            })
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


@router.websocket("/ws/market-data")
async def market_data_websocket(websocket: WebSocket) -> None:
    """
    WebSocket endpoint for streaming market data.

    Authentication (headers only, never the query string):
        - Authorization: Basic base64(username:password)
        - Sec-WebSocket-Protocol: hummingbot-auth, base64url(username:password)
          for browsers; the server echoes back the "hummingbot-auth" subprotocol.
    Unauthenticated handshakes are refused with HTTP 401, not accepted.

    Subscribe/unsubscribe protocol:
        -> {"action": "subscribe", "type": "candles", "connector": "binance",
            "trading_pair": "BTC-USDT", "interval": "1m", "update_interval": 1.0}
        <- {"type": "subscribed", "subscription_id": "candles_binance_BTC-USDT_1m"}
        <- {"type": "candles", "subscription_id": "...", "data": [...], ...}
        -> {"action": "unsubscribe", "subscription_id": "candles_binance_BTC-USDT_1m"}
        <- {"type": "unsubscribed", "subscription_id": "..."}

    Subscription types:
        - candles: streaming candle data for a trading pair
        - order_book: order book snapshots with configurable depth
        - trades: real-time trade events
    """
    authenticated, subprotocol = _authenticate_websocket(websocket)
    if not authenticated:
        await _reject_unauthenticated(websocket)
        return

    await websocket.accept(subprotocol=subprotocol)

    manager: WebSocketManager = websocket.app.state.websocket_manager
    conn_id = manager.generate_connection_id()

    await websocket.send_json({
        "type": "connected",
        "connection_id": conn_id,
        "timestamp": time.time(),
    })
    logger.info(f"[WS-MD] Client connected: {conn_id}")

    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(websocket), name=f"ws-md-hb-{conn_id}"
    )

    try:
        while True:
            msg = await websocket.receive_json()
            action = msg.get("action")

            if action == "subscribe":
                await manager.handle_subscribe(conn_id, websocket, msg)
            elif action == "unsubscribe":
                sub_id = msg.get("subscription_id")
                if sub_id:
                    await manager.handle_unsubscribe(conn_id, websocket, sub_id)
                else:
                    await websocket.send_json({
                        "type": "error",
                        "message": "unsubscribe requires 'subscription_id'",
                    })
            elif action == "ping":
                await websocket.send_json({
                    "type": "pong",
                    "timestamp": time.time(),
                })
            else:
                await websocket.send_json({
                    "type": "error",
                    "message": f"Unknown action: {action}. "
                               f"Valid actions: subscribe, unsubscribe, ping",
                })
    except WebSocketDisconnect:
        logger.info(f"[WS-MD] Client disconnected: {conn_id}")
    except Exception as e:
        logger.error(f"[WS-MD] Error for {conn_id}: {e}", exc_info=True)
    finally:
        heartbeat_task.cancel()
        manager.remove_connection(conn_id)


@router.websocket("/ws/executors")
async def executors_websocket(websocket: WebSocket) -> None:
    """
    WebSocket endpoint for streaming executor data.

    Authentication (headers only, never the query string):
        - Authorization: Basic base64(username:password)
        - Sec-WebSocket-Protocol: hummingbot-auth, base64url(username:password)
          for browsers; the server echoes back the "hummingbot-auth" subprotocol.
    Unauthenticated handshakes are refused with HTTP 401, not accepted.

    Subscribe/unsubscribe protocol:
        -> {"action": "subscribe", "type": "executor_summary", "update_interval": 2.0}
        <- {"type": "subscribed", "subscription_id": "executor_summary", ...}
        <- {"type": "executor_summary", "subscription_id": "executor_summary", "data": {...}, ...}
        -> {"action": "unsubscribe", "subscription_id": "executor_summary"}
        <- {"type": "unsubscribed", "subscription_id": "executor_summary"}

    Subscription types:
        - executors: filtered list of executors
        - executor_detail: single executor detail
        - executor_summary: aggregate summary of active executors
        - performance: performance report (optionally per controller)
        - positions: held positions with unrealized PnL
        - executor_logs: streaming log entries for an executor
        - bot_status: single bot status with performance & custom_info (requires bot_name)
        - all_bots_status: all active bots status with performance & custom_info
    """
    authenticated, subprotocol = _authenticate_websocket(websocket)
    if not authenticated:
        await _reject_unauthenticated(websocket)
        return

    await websocket.accept(subprotocol=subprotocol)

    # Get manager from app state
    manager = websocket.app.state.executor_ws_manager
    conn_id = str(uuid.uuid4())[:12]

    await websocket.send_json({
        "type": "connected",
        "connection_id": conn_id,
        "timestamp": time.time(),
    })
    logger.info(f"[WS-Exec] Client connected: {conn_id}")

    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(websocket), name=f"ws-exec-hb-{conn_id}"
    )

    try:
        while True:
            raw = await websocket.receive_json()
            action = raw.get("action")

            if action == "subscribe":
                await manager.handle_subscribe(conn_id, websocket, raw)
            elif action == "unsubscribe":
                sub_id = raw.get("subscription_id")
                if sub_id:
                    await manager.handle_unsubscribe(conn_id, websocket, sub_id)
                else:
                    await websocket.send_json({
                        "type": "error",
                        "message": "unsubscribe requires 'subscription_id'",
                    })
            elif action == "ping":
                await websocket.send_json({
                    "type": "pong",
                    "timestamp": time.time(),
                })
            else:
                await websocket.send_json({
                    "type": "error",
                    "message": f"Unknown action: {action}. "
                               f"Valid actions: subscribe, unsubscribe, ping",
                })
    except WebSocketDisconnect:
        logger.info(f"[WS-Exec] Client disconnected: {conn_id}")
    except Exception as e:
        logger.error(f"[WS-Exec] Error for {conn_id}: {e}", exc_info=True)
    finally:
        heartbeat_task.cancel()
        manager.remove_connection(conn_id)
