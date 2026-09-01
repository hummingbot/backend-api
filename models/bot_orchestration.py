import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

# Safe single path component names: prevents path traversal via '/', '\' or '..'.
# Mirrors services.accounts_service.SAFE_NAME_PATTERN (replicated locally to avoid a
# heavy/circular import of accounts_service into the model layer).
SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_safe_name(name: str, label: str) -> str:
    """Validate that a name is safe to use as a single path component (no separators or traversal sequences)."""
    if not name or not SAFE_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"Invalid {label}: '{name}'. Only letters, numbers, underscores and hyphens are allowed."
        )
    return name


def validate_safe_config_name(name: str, label: str) -> str:
    """Validate a config file name, ignoring an optional .yml extension before checking the base name."""
    base_name = name[:-4] if name.endswith(".yml") else name
    _validate_safe_name(base_name, label)
    return name


class BotAction(BaseModel):
    """Base class for bot actions"""
    bot_name: str = Field(description="Name of the bot instance to act upon")


class StartBotAction(BotAction):
    """Action to start a bot"""
    log_level: Optional[str] = Field(default=None, description="Logging level (DEBUG, INFO, WARNING, ERROR)")
    script: Optional[str] = Field(default=None, description="Script name to run (without .py extension)")
    conf: Optional[str] = Field(default=None, description="Configuration file name (without .yml extension)")
    async_backend: bool = Field(default=False, description="Whether to run in async backend mode")


class StopBotAction(BotAction):
    """Action to stop a bot"""
    skip_order_cancellation: bool = Field(default=False, description="Whether to skip cancelling open orders when stopping")
    async_backend: bool = Field(default=False, description="Whether to run in async backend mode")


class ImportStrategyAction(BotAction):
    """Action to import a strategy for a bot"""
    strategy: str = Field(description="Name of the strategy to import")


class ConfigureBotAction(BotAction):
    """Action to configure bot parameters"""
    params: dict = Field(description="Configuration parameters to update")


class ShortcutAction(BotAction):
    """Action to execute bot shortcuts"""
    params: list = Field(description="List of shortcut parameters")


class BotStatus(BaseModel):
    """Status information for a bot"""
    bot_name: str = Field(description="Bot name")
    status: str = Field(description="Bot status (running, stopped, etc.)")
    uptime: Optional[float] = Field(None, description="Bot uptime in seconds")
    performance: Optional[Dict[str, Any]] = Field(None, description="Performance metrics")


class BotHistoryRequest(BaseModel):
    """Request for bot trading history"""
    bot_name: str = Field(description="Bot name")
    days: int = Field(default=0, description="Number of days of history (0 for all)")
    verbose: bool = Field(default=False, description="Include verbose information")
    precision: Optional[int] = Field(None, description="Decimal precision for numbers")
    timeout: float = Field(default=30.0, description="Request timeout in seconds")


class BotHistoryResponse(BaseModel):
    """Response for bot trading history"""
    bot_name: str = Field(description="Bot name")
    history: Dict[str, Any] = Field(description="Trading history data")
    status: str = Field(description="Response status")


class MQTTStatus(BaseModel):
    """MQTT connection status"""
    mqtt_connected: bool = Field(description="Whether MQTT is connected")
    discovered_bots: List[str] = Field(description="List of discovered bots")
    active_bots: List[str] = Field(description="List of active bots")
    broker_host: str = Field(description="MQTT broker host")
    broker_port: int = Field(description="MQTT broker port")
    broker_username: Optional[str] = Field(None, description="MQTT broker username")
    client_state: str = Field(description="MQTT client state")


class AllBotsStatusResponse(BaseModel):
    """Response for all bots status"""
    bots: List[BotStatus] = Field(description="List of bot statuses")


class StopAndArchiveRequest(BaseModel):
    """Request for stopping and archiving a bot"""
    skip_order_cancellation: bool = Field(default=True, description="Skip order cancellation")
    async_backend: bool = Field(default=True, description="Use async backend")
    archive_locally: bool = Field(default=True, description="Archive locally")
    s3_bucket: Optional[str] = Field(None, description="S3 bucket for archiving")
    timeout: float = Field(default=30.0, description="Operation timeout")


class StopAndArchiveResponse(BaseModel):
    """Response for stop and archive operation"""
    status: str = Field(description="Operation status")
    message: str = Field(description="Status message")
    details: Dict[str, Any] = Field(description="Operation details")


# Bot deployment models
class V2ScriptDeployment(BaseModel):
    """Configuration for deploying a bot with a script"""
    instance_name: str = Field(description="Unique name for the bot instance")
    credentials_profile: str = Field(description="Name of the credentials profile to use")
    image: str = Field(default="hummingbot/hummingbot:latest", description="Docker image for the Hummingbot instance")
    script: Optional[str] = Field(default=None, description="Script name to run (without .py extension)")
    script_config: Optional[str] = Field(default=None, description="Script configuration file name (without .yml extension)")
    headless: bool = Field(default=False, description="Run in headless mode (no UI)")

    @field_validator("instance_name")
    @classmethod
    def _validate_instance_name(cls, v: str) -> str:
        return _validate_safe_name(v, "instance_name")

    @field_validator("credentials_profile")
    @classmethod
    def _validate_credentials_profile(cls, v: str) -> str:
        return _validate_safe_name(v, "credentials_profile")

    @field_validator("script_config")
    @classmethod
    def _validate_script_config(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return validate_safe_config_name(v, "script_config")


class V2ControllerDeployment(BaseModel):
    """Configuration for deploying a bot with controllers"""
    instance_name: str = Field(description="Unique name for the bot instance")
    credentials_profile: str = Field(description="Name of the credentials profile to use")
    controllers_config: List[str] = Field(
        description="List of controller configuration files to use (without .yml extension)"
    )
    max_global_drawdown_quote: Optional[float] = Field(
        default=None, description="Maximum allowed global drawdown in quote usually USDT"
    )
    max_controller_drawdown_quote: Optional[float] = Field(
        default=None, description="Maximum allowed per-controller drawdown in quote usually USDT"
    )
    image: str = Field(default="hummingbot/hummingbot:latest", description="Docker image for the Hummingbot instance")
    script_config: Optional[str] = Field(default=None, description="Generated script configuration file name")
    headless: bool = Field(default=False, description="Run in headless mode (no UI)")

    @field_validator("instance_name")
    @classmethod
    def _validate_instance_name(cls, v: str) -> str:
        return _validate_safe_name(v, "instance_name")

    @field_validator("credentials_profile")
    @classmethod
    def _validate_credentials_profile(cls, v: str) -> str:
        return _validate_safe_name(v, "credentials_profile")

    @field_validator("controllers_config")
    @classmethod
    def _validate_controllers_config(cls, v: List[str]) -> List[str]:
        return [validate_safe_config_name(controller, "controllers_config") for controller in v]

    @field_validator("script_config")
    @classmethod
    def _validate_script_config(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return validate_safe_config_name(v, "script_config")
