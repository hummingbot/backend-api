from decimal import Decimal
from typing import Any, Dict, Optional

from pydantic import BaseModel


class HummingbotInstanceConfig(BaseModel):
    instance_name: str
    credentials_profile: str
    image: str = "hummingbot/hummingbot:latest"
    script: Optional[str] = None
    script_config: Optional[str] = None


class ImageName(BaseModel):
    image_name: str


class Script(BaseModel):
    name: str
    content: str


class ScriptConfig(BaseModel):
    name: str
    content: Dict[str, Any]  # YAML content represented as a dictionary


class BotAction(BaseModel):
    bot_name: str


class StartBotAction(BotAction):
    log_level: str = None
    script: str = None
    conf: str = None
    async_backend: bool = False


class StopBotAction(BotAction):
    skip_order_cancellation: bool = False
    async_backend: bool = False


class ImportStrategyAction(BotAction):
    strategy: str


class ConfigureBotAction(BotAction):
    params: dict


class ShortcutAction(BotAction):
    params: list

class Instance(BaseModel):
    id: str
    wallet_address: str
    running_status: bool
    deployed_strategy: Optional[str]

class InstanceStats(BaseModel):
    pnl: Decimal
    # Add other relevant statistics here

class Strategy(BaseModel):
    name: str
    parameters: dict
    min_values: dict
    max_values: dict
    default_values: dict

class BacktestRequest(BaseModel):
    strategy_name: str
    parameters: dict
    start_time: int
    end_time: int

class BacktestResult(BaseModel):
    pnl: Decimal
    # Add other relevant backtest results here

class StartStrategyRequest(BaseModel):
    strategy_name: str
    parameters: dict

class InstanceResponse(BaseModel):
    instance_id: str
    wallet_address: str