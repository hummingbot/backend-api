from fastapi import Depends, HTTPException, Request

from database import AsyncDatabaseManager
from services.accounts_service import AccountsService
from services.backtesting_service import BacktestingService
from services.bots_orchestrator import BotsOrchestrator
from services.docker_service import DockerService
from services.executor_service import ExecutorService
from services.executor_ws_manager import ExecutorWebSocketManager
from services.gateway_service import GatewayService
from services.market_data_service import MarketDataService
from services.trading_history_service import TradingHistoryService
from services.trading_service import TradingService
from services.unified_connector_service import UnifiedConnectorService
from services.websocket_manager import WebSocketManager
from utils.bot_archiver import BotArchiver


def get_bots_orchestrator(request: Request) -> BotsOrchestrator:
    """Get BotsOrchestrator service from app state."""
    return request.app.state.bots_orchestrator


def get_accounts_service(request: Request) -> AccountsService:
    """Get AccountsService from app state."""
    return request.app.state.accounts_service


GATEWAY_UNAVAILABLE_DETAIL = "Gateway service is not available"


async def require_gateway_online(accounts_service: AccountsService = Depends(get_accounts_service)) -> None:
    """Pre-flight guard for routes that cannot be served at all while Gateway is unreachable.

    Attach per route with ``dependencies=[Depends(require_gateway_online)]``. Deliberately not applied at
    router level: the container-control routes (``/gateway/start``, ``/gateway/restart``, ...) must answer
    precisely when Gateway is down, and the DB-backed reads keep serving stored data regardless.
    """
    if not await accounts_service.gateway_client.ping():
        raise HTTPException(status_code=503, detail=GATEWAY_UNAVAILABLE_DETAIL)


def get_docker_service(request: Request) -> DockerService:
    """Get DockerService from app state."""
    return request.app.state.docker_service


def get_gateway_service(request: Request) -> GatewayService:
    """Get GatewayService from app state."""
    return request.app.state.gateway_service


def get_connector_service(request: Request) -> UnifiedConnectorService:
    """Get UnifiedConnectorService from app state."""
    return request.app.state.connector_service


def get_market_data_service(request: Request) -> MarketDataService:
    """Get MarketDataService from app state."""
    return request.app.state.market_data_service


def get_trading_service(request: Request) -> TradingService:
    """Get TradingService from app state."""
    return request.app.state.trading_service


def get_trading_history_service(request: Request) -> TradingHistoryService:
    """Get TradingHistoryService from app state."""
    return request.app.state.trading_history_service


def get_executor_service(request: Request) -> ExecutorService:
    """Get ExecutorService from app state."""
    return request.app.state.executor_service


def get_bot_archiver(request: Request) -> BotArchiver:
    """Get BotArchiver from app state."""
    return request.app.state.bot_archiver


def get_database_manager(request: Request) -> AsyncDatabaseManager:
    """Get AsyncDatabaseManager from app state."""
    return request.app.state.db_manager


def get_executor_ws_manager(request: Request) -> ExecutorWebSocketManager:
    """Get ExecutorWebSocketManager from app state."""
    return request.app.state.executor_ws_manager


def get_backtesting_service(request: Request) -> BacktestingService:
    """Get BacktestingService from app state."""
    return request.app.state.backtesting_service


def get_websocket_manager(request: Request) -> WebSocketManager:
    """Get WebSocketManager from app state."""
    return request.app.state.websocket_manager
