import logging
import secrets
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import urlparse

import logfire
from dotenv import load_dotenv

# Apply the patch before importing hummingbot components
from hummingbot.client.config import config_helpers

# Load environment variables early
load_dotenv()

VERSION = "1.0.1"

# Monkey patch save_to_yml to prevent writes to library directory


def patched_save_to_yml(yml_path, cm):
    """Patched version of save_to_yml that prevents writes to library directory"""
    import logging
    logger = logging.getLogger(__name__)
    logger.debug(f"Skipping config write to {yml_path} (patched for API mode)")
    # Do nothing - this prevents the original function from trying to write to the library directory


config_helpers.save_to_yml = patched_save_to_yml

from fastapi import Depends, FastAPI, HTTPException, Request, status  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.security import HTTPBasic, HTTPBasicCredentials  # noqa: E402
from hummingbot.client.config.client_config_map import GatewayConfigMap  # noqa: E402
from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger  # noqa: E402
from hummingbot.core.gateway.gateway_http_client import GatewayHttpClient  # noqa: E402

from config import settings, warn_if_insecure_security_defaults  # noqa: E402
from database import AsyncDatabaseManager  # noqa: E402
from routers import (  # noqa: E402
    accounts,
    archived_bots,
    backtesting,
    bot_orchestration,
    connectors,
    controllers,
    docker,
    executors,
    gateway,
    gateway_amm,
    gateway_clmm,
    gateway_swap,
    market_data,
    portfolio,
    scripts,
    storage,
    system,
    trading,
    websocket,
)
from services.accounts_service import AccountsService  # noqa: E402
from services.backtesting_service import BacktestingService  # noqa: E402
from services.bots_orchestrator import BotsOrchestrator  # noqa: E402
from services.docker_service import DockerService  # noqa: E402
from services.executor_service import ExecutorService  # noqa: E402
from services.executor_ws_manager import ExecutorWebSocketManager  # noqa: E402
from services.gateway_clmm_service import GatewayCLMMService  # noqa: E402
from services.gateway_service import GatewayService  # noqa: E402
from services.gateway_swap_service import GatewaySwapService  # noqa: E402
from services.market_data_service import MarketDataService  # noqa: E402
from services.trading_history_service import TradingHistoryService  # noqa: E402
from services.trading_service import TradingService  # noqa: E402
from services.unified_connector_service import UnifiedConnectorService  # noqa: E402
from services.websocket_manager import WebSocketManager  # noqa: E402
from utils.bot_archiver import BotArchiver  # noqa: E402
from utils.core_compatibility import require_core_surface  # noqa: E402
from utils.security import BackendAPISecurity  # noqa: E402

# Set up logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Enable info logging for MQTT manager
logging.getLogger('services.mqtt_manager').setLevel(logging.INFO)

# Get settings from Pydantic Settings
username = settings.security.username
password = settings.security.password

# Security setup
security = HTTPBasic()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager for the FastAPI application.
    Handles startup and shutdown events.
    """
    # The installed core is unpinned (see environment.yml), so check it carries the
    # fields this API reads before anything reads them — the read path substitutes
    # zeros for a missing attribute and logs at DEBUG, which turns a wrong install
    # into silently zeroed accounting rather than a failure.
    require_core_surface()

    # SEC-018: warn loudly if USERNAME/PASSWORD/CONFIG_PASSWORD are still the insecure defaults
    warn_if_insecure_security_defaults(settings.security)

    # Ensure password verification file exists
    if BackendAPISecurity.new_password_required():
        # Create secrets manager with CONFIG_PASSWORD
        secrets_manager = ETHKeyFileSecretManger(password=settings.security.config_password)
        BackendAPISecurity.store_password_verification(secrets_manager)
        logging.info("Created password verification file for master_account")

    # =========================================================================
    # 1. Infrastructure Setup
    # =========================================================================

    # Initialize the secrets manager and log the master account in BEFORE any Gateway
    # client exists: hummingbot's GatewayHttpClient decrypts its mTLS client key with
    # Security.secrets_manager.password, which stays None until a login. Logging in
    # here (instead of lazily on first connector creation) means the status monitor's
    # very first ping can never hit an uninitialized Security.
    secrets_manager = ETHKeyFileSecretManger(password=settings.security.config_password)
    if not BackendAPISecurity.login_account(account_name="master_account", secrets_manager=secrets_manager):
        raise RuntimeError(
            "CONFIG_PASSWORD does not match the stored password verification file at "
            f"bots/{settings.app.password_verification_path}"
        )

    # Initialize GatewayHttpClient singleton
    from utils.gateway_certs import ensure_gateway_certs
    parsed_gateway_url = urlparse(settings.gateway.url)
    gateway_use_ssl = parsed_gateway_url.scheme == "https"
    if gateway_use_ssl:
        # SEC-048: generate the shared mTLS cert set up front (idempotent; an existing CA is
        # reused untouched) and mirror the client certs into root_path()/certs, the only place
        # hummingbot's in-process GatewayHttpClient looks. Doing this at startup rather than
        # lazily on Gateway start means the client can always build its SSL context — a missing
        # ca_cert.pem used to raise FileNotFoundError on every single request.
        try:
            ensure_gateway_certs(settings.security.config_password)
        except Exception as e:
            # Non-fatal: the API must boot even if the cert dir isn't writable.
            logging.warning(f"Could not prepare Gateway mTLS certs: {e}")
    gateway_config = GatewayConfigMap(
        gateway_api_host=parsed_gateway_url.hostname or "localhost",
        gateway_api_port=str(parsed_gateway_url.port or 15888),
        gateway_use_ssl=gateway_use_ssl
    )
    GatewayHttpClient.get_instance(gateway_config)
    # Start the Gateway status monitor so Gateway's network connectors (e.g.
    # 'solana-mainnet-beta', and any newly added chain like 'ethereum-unichain') are
    # discovered from /config/chains and registered in AllConnectorSettings. Without
    # it, a Gateway network only lands in AllConnectorSettings lazily, when a connector
    # for it is first constructed; the monitor makes new chains enumerable without
    # first deploying a bot on them, and picks them up when Gateway comes online later.
    # The monitor polls every 2s and logs loudly on every failure, so it is only started
    # once there is a Gateway to poll — see the gateway_service block below, which has the
    # Docker client needed to check. GatewayService.start() starts it when the Gateway
    # comes up later.
    logging.info(f"Initialized GatewayHttpClient with URL: {settings.gateway.url}")

    # Initialize database
    db_manager = AsyncDatabaseManager(settings.database.url)
    await db_manager.create_tables()
    logging.info("Database initialized")

    # Read the global quote token (the currency everything is valued in) from conf_client.yml.
    # Prices come from our own ticker pool (MarketDataService), not the legacy RateOracle.
    from utils.file_system import FileSystemUtil
    fs_util = FileSystemUtil()

    quote_token = "USDT"
    try:
        conf_client_path = "credentials/master_account/conf_client.yml"
        config_data = fs_util.read_yaml_file(conf_client_path)
        quote_token = config_data.get("global_token", {}).get("global_token_name", "USDT")
        logging.info(f"Configured global quote token: {quote_token}")
    except FileNotFoundError:
        logging.warning("conf_client.yml not found, defaulting global quote token to USDT")
    except Exception as e:
        logging.warning(f"Error reading conf_client.yml: {e}, defaulting global quote token to USDT")

    # =========================================================================
    # 2. UnifiedConnectorService - Single source of truth for all connectors
    # =========================================================================

    connector_service = UnifiedConnectorService(
        secrets_manager=secrets_manager,
        db_manager=db_manager
    )
    logging.info("UnifiedConnectorService initialized")

    # =========================================================================
    # 3. Services that depend on connector_service
    # =========================================================================

    # MarketDataService - candles, order books, tickers, cross-rate pricing
    market_data_service = MarketDataService(
        connector_service=connector_service,
        quote_token=quote_token,
        cleanup_interval=settings.market_data.cleanup_interval,
        feed_timeout=settings.market_data.feed_timeout,
        ticker_update_interval=settings.market_data.ticker_update_interval,
        ticker_max_age=settings.market_data.ticker_max_age,
        ticker_subscription_ttl=settings.market_data.ticker_subscription_ttl,
    )
    # Connector trade-volume telemetry resolves rates through the ticker pool instead of the
    # legacy RateOracle singleton.
    connector_service.set_rate_provider(market_data_service)
    logging.info("MarketDataService initialized")

    # TradingService - order placement, positions, trading interfaces
    trading_service = TradingService(
        connector_service=connector_service,
        market_data_service=market_data_service
    )
    logging.info("TradingService initialized")

    # AccountsService - account management, balances, portfolio (simplified)
    accounts_service = AccountsService(
        db_manager=db_manager,
        connector_service=connector_service,
        market_data_service=market_data_service,
        trading_service=trading_service,
        account_update_interval=settings.app.account_update_interval,
        gateway_url=settings.gateway.url
    )
    logging.info("AccountsService initialized")

    # TradingHistoryService - read-only persistence queries for orders/trades/funding
    trading_history_service = TradingHistoryService(db_manager=db_manager)
    logging.info("TradingHistoryService initialized")

    # Gateway CLMM/swap persistence - the /gateway/clmm/* and /gateway/swap* routes
    # read and write their history through these instead of owning sessions themselves
    gateway_clmm_service = GatewayCLMMService(db_manager=db_manager)
    gateway_swap_service = GatewaySwapService(db_manager=db_manager)
    logging.info("GatewayCLMMService and GatewaySwapService initialized")

    # =========================================================================
    # 4. ExecutorService - depends on TradingService (NO circular dependency)
    # =========================================================================

    executor_service = ExecutorService(
        trading_service=trading_service,
        db_manager=db_manager,
        default_account="master_account",
        update_interval=1.0,
        max_retries=10
    )
    logging.info("ExecutorService initialized")

    # =========================================================================
    # 5. Other Services
    # =========================================================================

    bots_orchestrator = BotsOrchestrator(
        broker_host=settings.broker.host,
        broker_port=settings.broker.port,
        broker_username=settings.broker.username,
        broker_password=settings.broker.password,
        db_manager=db_manager,
        performance_dump_interval=settings.broker.performance_dump_interval
    )

    backtesting_service = BacktestingService()
    docker_service = DockerService()
    gateway_service = GatewayService()
    # If a secured Gateway is already running but this API lost the shared mTLS certs (e.g. the
    # API container was recreated without the persisted bots/ mount), regenerate the cert set and
    # restart the Gateway so it loads a matching server cert. Non-fatal: the API must still boot
    # even when Docker is unavailable or the Gateway is simply not running.
    if gateway_use_ssl:
        try:
            reconcile = gateway_service.reconcile_certs()
            if reconcile.get("action") != "none":
                logging.info(f"Gateway cert reconciliation: {reconcile.get('message')}")
        except Exception as e:
            logging.warning(f"Gateway cert reconciliation skipped: {e}")

    # Start the Gateway status monitor only when there is a Gateway to poll. Gating on the
    # container itself (rather than on cert presence, which is now always true) keeps the 2s
    # poll loop from spamming errors for the many deployments that never run a Gateway.
    # Plain-HTTP setups may point at a Gateway this API doesn't manage, so keep polling there.
    try:
        gateway_is_running = gateway_service.is_running()
    except Exception as e:
        logging.warning(f"Could not determine Gateway container state: {e}")
        gateway_is_running = False
    if not gateway_use_ssl or gateway_is_running:
        GatewayHttpClient.get_instance().start_monitor()
    else:
        logging.info("Gateway container not running; status monitor deferred until it is started")

    bot_archiver = BotArchiver(
        settings.aws.api_key,
        settings.aws.secret_key,
        settings.aws.s3_default_bucket_name
    )

    # =========================================================================
    # 6. Start services
    # =========================================================================

    # Initialize all trading connectors FIRST (before any service that might use them)
    # This ensures OrdersRecorder is properly attached before any concurrent access
    logging.info("Initializing all trading connectors...")
    await connector_service.initialize_all_trading_connectors()

    # Reconcile persisted active orders against the exchange (e.g. after an API
    # restart/crash that lost in-memory references). Confirmed-closed orders are
    # marked terminal; still-open orders are re-tracked so they stay cancelable.
    # Runs after connectors reload their persisted in-flight orders.
    await connector_service.reconcile_active_orders()

    bots_orchestrator.start()
    market_data_service.start()
    await market_data_service.warmup_tickers()
    executor_service.start()
    await executor_service.cleanup_orphaned_executors()
    await executor_service.recover_positions_from_db()
    accounts_service.start()

    # =========================================================================
    # 7. Store services in app state
    # =========================================================================

    app.state.db_manager = db_manager
    app.state.connector_service = connector_service
    app.state.market_data_service = market_data_service
    app.state.trading_service = trading_service
    app.state.accounts_service = accounts_service
    app.state.trading_history_service = trading_history_service
    app.state.gateway_clmm_service = gateway_clmm_service
    app.state.gateway_swap_service = gateway_swap_service
    app.state.executor_service = executor_service
    websocket_manager = WebSocketManager(market_data_service)
    app.state.websocket_manager = websocket_manager

    app.state.backtesting_service = backtesting_service
    app.state.bots_orchestrator = bots_orchestrator
    app.state.docker_service = docker_service
    app.state.gateway_service = gateway_service
    app.state.bot_archiver = bot_archiver

    # WebSocket manager for executor streaming
    executor_ws_manager = ExecutorWebSocketManager(executor_service, market_data_service, bots_orchestrator)
    app.state.executor_ws_manager = executor_ws_manager

    logging.info("All services started successfully")

    yield

    # =========================================================================
    # Shutdown services
    # =========================================================================

    logging.info("Shutting down services...")

    websocket_manager.shutdown()
    await executor_ws_manager.shutdown()
    await bots_orchestrator.stop()
    await accounts_service.stop()
    await executor_service.stop()
    market_data_service.stop()
    await connector_service.stop_all()
    GatewayHttpClient.get_instance().stop_monitor()
    docker_service.cleanup()
    await db_manager.close()

    logging.info("All services stopped")

# Initialize FastAPI with metadata and lifespan
app = FastAPI(
    title="Hummingbot API",
    description="API for managing Hummingbot trading instances",
    version=VERSION,
    lifespan=lifespan,
    redirect_slashes=False,
)

# Add CORS middleware (SEC-019). Origins are restricted by default: a wildcard origin must not be
# combined with allow_credentials=True. Trusted origins are configured via CORS_ALLOW_ORIGINS /
# CORS_ALLOW_ORIGIN_REGEX (see config.CORSSettings); the default only allows localhost origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors.allow_origins,
    allow_origin_regex=settings.cors.allow_origin_regex or None,
    allow_credentials=settings.cors.allow_credentials,
    allow_methods=settings.cors.allow_methods,
    allow_headers=settings.cors.allow_headers,
)

# Compress responses for clients that send Accept-Encoding: gzip. These payloads are highly
# repetitive JSON and compress ~14x; small responses are left alone via minimum_size.
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Custom handler for validation errors to log detailed error messages.
    """
    # Build a readable error message from validation errors
    error_messages = []
    for error in exc.errors():
        loc = " -> ".join(str(part) for part in error.get("loc", []))
        msg = error.get("msg", "Validation error")
        error_messages.append(f"{loc}: {msg}")

    # Log the validation error with details
    logging.warning(
        f"Validation error on {request.method} {request.url.path}: {'; '.join(error_messages)}"
    )

    # Return standard FastAPI validation error response
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.errors()},
    )

logfire.configure(send_to_logfire="if-token-present", environment=settings.app.logfire_environment,
                  service_name="hummingbot-api")
logfire.instrument_fastapi(app)


def auth_user(
        credentials: Annotated[HTTPBasicCredentials, Depends(security)],
):
    """Authenticate user using HTTP Basic Auth"""
    current_username_bytes = credentials.username.encode("utf8")
    correct_username_bytes = f"{username}".encode("utf8")
    is_correct_username = secrets.compare_digest(
        current_username_bytes, correct_username_bytes
    )
    current_password_bytes = credentials.password.encode("utf8")
    correct_password_bytes = f"{password}".encode("utf8")
    is_correct_password = secrets.compare_digest(
        current_password_bytes, correct_password_bytes
    )
    if not (is_correct_username and is_correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username


# Include all routers with authentication
app.include_router(docker.router, dependencies=[Depends(auth_user)])
app.include_router(gateway.router, dependencies=[Depends(auth_user)])
app.include_router(accounts.router, dependencies=[Depends(auth_user)])
app.include_router(connectors.router, dependencies=[Depends(auth_user)])
app.include_router(portfolio.router, dependencies=[Depends(auth_user)])
app.include_router(trading.router, dependencies=[Depends(auth_user)])
app.include_router(gateway_swap.router, dependencies=[Depends(auth_user)])
app.include_router(gateway_clmm.router, dependencies=[Depends(auth_user)])
app.include_router(gateway_amm.router, dependencies=[Depends(auth_user)])
app.include_router(bot_orchestration.router, dependencies=[Depends(auth_user)])
app.include_router(controllers.router, dependencies=[Depends(auth_user)])
app.include_router(scripts.router, dependencies=[Depends(auth_user)])
app.include_router(market_data.router, dependencies=[Depends(auth_user)])
app.include_router(backtesting.router, dependencies=[Depends(auth_user)])
app.include_router(archived_bots.router, dependencies=[Depends(auth_user)])
app.include_router(storage.router, dependencies=[Depends(auth_user)])
app.include_router(system.router, dependencies=[Depends(auth_user)])

app.include_router(executors.router, dependencies=[Depends(auth_user)])

# WebSocket router (handles its own auth)
app.include_router(websocket.router)


@app.get("/")
async def root():
    """API root endpoint returning basic information."""
    return {
        "name": "Hummingbot API",
        "version": VERSION,
        "status": "running",
    }
