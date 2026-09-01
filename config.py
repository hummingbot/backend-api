import logging
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BrokerSettings(BaseSettings):
    """MQTT Broker configuration for bot communication."""

    host: str = Field(default="localhost", description="MQTT broker host")
    port: int = Field(default=1883, description="MQTT broker port")
    username: str = Field(default="admin", description="MQTT broker username")
    password: str = Field(default="password", description="MQTT broker password")
    performance_dump_interval: int = Field(default=5, description="Controller performance dump interval in minutes")

    model_config = SettingsConfigDict(env_prefix="BROKER_", extra="ignore")


class DatabaseSettings(BaseSettings):
    """Database configuration."""

    url: str = Field(
        default="postgresql+asyncpg://hbot:hummingbot-api@localhost:5432/hummingbot_api",
        description="Database connection URL"
    )

    model_config = SettingsConfigDict(env_prefix="DATABASE_", extra="ignore")


class MarketDataSettings(BaseSettings):
    """Market data feed manager configuration."""

    cleanup_interval: int = Field(
        default=300,
        description="How often to run feed cleanup in seconds"
    )
    feed_timeout: int = Field(
        default=600,
        description="How long to keep unused feeds alive in seconds"
    )
    candles_ready_timeout: int = Field(
        default=30,
        description="How long to wait for a candle feed to become ready in seconds"
    )
    ws_heartbeat_interval: int = Field(
        default=30,
        description="WebSocket heartbeat interval in seconds"
    )
    ws_min_update_interval: float = Field(
        default=0.25,
        description="Minimum allowed WebSocket subscription update interval in seconds"
    )
    ws_max_update_interval: float = Field(
        default=60.0,
        description="Maximum allowed WebSocket subscription update interval in seconds"
    )
    ws_executor_min_update_interval: float = Field(
        default=0.5,
        description="Minimum allowed /ws/executors subscription update interval in seconds. "
                    "The floor is stricter than the market-data one because executor push loops "
                    "hit the database (executors, performance reports, positions with per-position "
                    "rate lookups) instead of reading in-memory candles and order books"
    )
    ws_executor_max_update_interval: float = Field(
        default=60.0,
        description="Maximum allowed /ws/executors subscription update interval in seconds"
    )
    ws_executor_default_update_interval: float = Field(
        default=2.0,
        description="Update interval applied to a /ws/executors subscription that does not "
                    "request one, in seconds"
    )
    ticker_update_interval: int = Field(
        default=30,
        description="How often to refresh tickers from connected exchanges in seconds"
    )
    ticker_max_age: int = Field(
        default=60,
        description="Max age of cached tickers before an on-demand request refetches them, in seconds"
    )
    ticker_subscription_ttl: int = Field(
        default=600,
        description="How long a ticker-only connector stays in the background refresh cycle "
                    "after its last request, in seconds"
    )
    hyperliquid_hip3_interval: int = Field(
        default=120,
        description="How often to refresh Hyperliquid HIP-3 (builder-deployed perp dex) tickers, "
                    "in seconds. These need one request per dex (~10 total), so they refresh on a "
                    "slower cadence than the main perp dex. Set to 0 to exclude HIP-3 markets."
    )

    model_config = SettingsConfigDict(env_prefix="MARKET_DATA_", extra="ignore")


# Insecure default credential values (SEC-018), mapped to the environment variables that override them.
# They are kept only for local development convenience and MUST be overridden in production deployments.
_INSECURE_SECURITY_DEFAULTS = {
    "USERNAME": "admin",
    "PASSWORD": "admin",
    "CONFIG_PASSWORD": "a",
}


class SecuritySettings(BaseSettings):
    """Security and authentication configuration.

    All fields are read from environment variables without a prefix (or from .env):
    - USERNAME: API basic auth username (default "admin" — local development only, never use in production)
    - PASSWORD: API basic auth password (default "admin" — local development only, never use in production)
    - CONFIG_PASSWORD: password used to encrypt ALL connector credentials (default "a" — local development only,
      never use in production)
    """

    username: str = Field(default="admin", description="API basic auth username (override via USERNAME in production)")
    password: str = Field(default="admin", description="API basic auth password (override via PASSWORD in production)")
    config_password: str = Field(
        default="a",
        description="Bot configuration encryption password (override via CONFIG_PASSWORD in production)"
    )

    model_config = SettingsConfigDict(
        env_prefix="",
        extra="ignore"  # Ignore extra environment variables
    )

    def insecure_defaults_in_use(self) -> List[str]:
        """Return the env var names of security settings still set to their insecure default values."""
        current_values = {"USERNAME": self.username, "PASSWORD": self.password, "CONFIG_PASSWORD": self.config_password}
        return [name for name, default in _INSECURE_SECURITY_DEFAULTS.items() if current_values[name] == default]


def warn_if_insecure_security_defaults(security: SecuritySettings) -> List[str]:
    """Emit a high-severity log if any security setting still uses its insecure default value (SEC-018).

    Returns the list of env var names that are still at their defaults (empty list when fully configured).
    """
    insecure = security.insecure_defaults_in_use()
    if insecure:
        logging.critical(
            "SECURITY WARNING: insecure default credentials in use for: %s. "
            "Anyone who can reach this API can authenticate with the default basic auth credentials, and all "
            "connector credentials are encrypted with a trivially guessable password. "
            "Set the USERNAME, PASSWORD and CONFIG_PASSWORD environment variables (e.g. in .env) before deploying "
            "to production. Do NOT run a production deployment with these defaults.",
            ", ".join(insecure),
        )
    return insecure


class AWSSettings(BaseSettings):
    """AWS configuration for S3 archiving."""

    api_key: str = Field(default="", description="AWS API key")
    secret_key: str = Field(default="", description="AWS secret key")
    s3_default_bucket_name: str = Field(default="", description="Default S3 bucket for archiving")

    model_config = SettingsConfigDict(env_prefix="AWS_", extra="ignore")


class GatewaySettings(BaseSettings):
    """Gateway service configuration."""

    url: str = Field(
        default="https://localhost:15888",
        description="Gateway service URL. The Gateway always runs secured (mTLS), so this must use "
                    "the 'https' scheme (SEC-048); use 'https://gateway:15888' when running in Docker."
    )

    model_config = SettingsConfigDict(env_prefix="GATEWAY_", extra="ignore")


class CORSSettings(BaseSettings):
    """CORS configuration for the API (SEC-019).

    A wildcard origin ("*") must never be combined with allow_credentials=True: browsers reject that
    combination per the CORS spec, and Starlette works around it by reflecting any Origin, which lets
    arbitrary third-party pages call the API from an authenticated operator's browser. Origins are
    therefore restricted by default and configurable via environment variables:
    - CORS_ALLOW_ORIGINS: JSON list of explicit trusted origins, e.g. '["https://dashboard.example.com"]'
    - CORS_ALLOW_ORIGIN_REGEX: regex for trusted origins (defaults to localhost-only for local development;
      set to an empty string to disable regex matching entirely)
    """

    allow_origins: List[str] = Field(
        default=[],
        description='Explicit list of trusted CORS origins, e.g. CORS_ALLOW_ORIGINS=\'["https://dashboard.example.com"]\''
    )
    allow_origin_regex: str = Field(
        default=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
        description="Regex matching trusted CORS origins; defaults to localhost-only. Empty string disables regex matching."
    )
    allow_credentials: bool = Field(default=True, description="Allow credentialed (cookies/auth) cross-origin requests")
    allow_methods: List[str] = Field(default=["*"], description="HTTP methods allowed for cross-origin requests")
    allow_headers: List[str] = Field(default=["*"], description="HTTP headers allowed for cross-origin requests")

    model_config = SettingsConfigDict(env_prefix="CORS_", extra="ignore")


class AppSettings(BaseSettings):
    """Main application settings."""

    # Static paths
    controllers_path: str = "bots/conf/controllers"
    controllers_module: str = "bots.controllers"
    password_verification_path: str = "credentials/master_account/.password_verification"

    # Environment-configurable settings
    logfire_environment: str = Field(
        default="dev",
        description="Logfire environment name"
    )

    # Account state update interval
    account_update_interval: int = Field(
        default=5,
        description="How often to update account states in minutes"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )


class BacktestingSettings(BaseSettings):
    """Backtest execution limits and result retention.

    A finished backtest is ~98% bulk arrays (processed_data, pnl_timeseries) and only a
    few KB of metrics, so full payloads are archived to disk and only metrics stay
    resident. Retention is therefore a count of results, not a memory budget.

    A run executes in its own worker process and saturates a core for its whole duration,
    so the two execution limits are about the box, not about memory: how many cores runs
    may claim at once, and how long one is allowed to claim one before being abandoned.
    """

    max_concurrent: int = Field(
        default=1,
        description=(
            "How many backtests may run at once; further submissions queue. Runs are isolated "
            "in separate processes, so this can be raised up to the cores you are willing to give them"
        )
    )
    timeout_seconds: float = Field(
        default=1800.0,
        description="Wall-clock budget for one backtest; the worker is killed and the task fails past it"
    )
    max_results: int = Field(
        default=100,
        description="How many finished backtests to retain before the oldest are reaped"
    )
    results_path: str = Field(
        default="bots/data/backtests",
        description="Directory holding archived backtest payloads (inside the bots volume, so it survives redeploys)"
    )
    candles_cache_path: str = Field(
        default="bots/data/backtests/candles",
        description="Directory holding downloaded candle history shared by backtest workers"
    )
    candles_cache_entries: int = Field(
        default=32,
        description=(
            "How many downloaded candle ranges to keep; the least recently used are dropped past it. "
            "0 disables the cache and makes every run download its own history again"
        )
    )
    candles_cache_ttl_seconds: float = Field(
        default=3600.0,
        description=(
            "How long a downloaded candle range may be reused. A window ending near now is fetched "
            "with its last candle still forming, so an entry is refetched once it is older than this"
        )
    )

    model_config = SettingsConfigDict(env_prefix="BACKTESTING_", extra="ignore")


class PerformanceSettings(BaseSettings):
    """Performance snapshot cadence and retention."""

    executor_snapshot_interval: int = Field(
        default=60,
        description="How often a live executor's performance is snapshotted, in seconds. "
                    "Finer than the controller dump because executors are short-lived: at "
                    "a 5-minute grain a three-minute position executor gets one point."
    )
    retention_days: int = Field(
        default=0,
        description="Delete performance snapshots (executor AND controller) older than "
                    "this many days. 0 keeps everything forever, which is what every "
                    "existing deployment does today -- an upgrade must not start deleting "
                    "an operator's history."
    )

    model_config = SettingsConfigDict(env_prefix="PERFORMANCE_", extra="ignore")


class Settings(BaseSettings):
    """Combined application settings."""

    broker: BrokerSettings = Field(default_factory=BrokerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    market_data: MarketDataSettings = Field(default_factory=MarketDataSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    aws: AWSSettings = Field(default_factory=AWSSettings)
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    cors: CORSSettings = Field(default_factory=CORSSettings)
    app: AppSettings = Field(default_factory=AppSettings)
    backtesting: BacktestingSettings = Field(default_factory=BacktestingSettings)
    performance: PerformanceSettings = Field(default_factory=PerformanceSettings)

    # Direct banned_tokens field to handle env parsing
    banned_tokens: List[str] = Field(
        default=["NAV", "ARS", "ETHW", "ETHF"],
        description="List of banned trading tokens"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore"
    )


settings = Settings()
