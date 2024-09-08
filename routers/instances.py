from decimal import Decimal
import uuid
from fastapi import APIRouter, HTTPException
from typing import List

from config import BROKER_HOST, BROKER_PASSWORD, BROKER_PORT, BROKER_USERNAME
from models import HummingbotInstanceConfig, Instance, InstanceStats, Strategy, BacktestRequest, BacktestResult, StartStrategyRequest, InstanceResponse
from services.docker_service import DockerManager
from services.bots_orchestrator import BotsManager

router = APIRouter(tags=["Instance Management"])

docker_manager = DockerManager()
bots_manager = BotsManager(broker_host=BROKER_HOST, broker_port=BROKER_PORT, 
                           broker_username=BROKER_USERNAME, broker_password=BROKER_PASSWORD)

@router.post("/instances", response_model=InstanceResponse)
async def create_instance():
    # Create a new Hummingbot instance
    instance_config = HummingbotInstanceConfig(
        instance_name=f"instance_{uuid.uuid4().hex[:8]}",
        credentials_profile="master_account",
        image="hummingbot/hummingbot:latest"
    )
    result = docker_manager.create_hummingbot_instance(instance_config)
    if result["success"]:
        # Generate a wallet address for the instance (this is a placeholder, implement actual wallet generation)
        wallet_address = f"0x{uuid.uuid4().hex}"
        return InstanceResponse(instance_id=instance_config.instance_name, wallet_address=wallet_address)
    else:
        raise HTTPException(status_code=500, detail=result["message"])

@router.get("/instances", response_model=List[Instance])
async def get_instances():
    active_containers = docker_manager.get_active_containers()
    instances = []
    for container_name in active_containers:
        status = bots_manager.get_bot_status(container_name)
        instances.append(Instance(
            id=container_name,
            wallet_address="0x1234567890123456789012345678901234567890",  # Placeholder
            running_status=status["status"] == "running",
            deployed_strategy=status["performance"].get("strategy", None) if status["status"] == "running" else None
        ))
    return instances

@router.get("/instance/{instance_id}/stats", response_model=InstanceStats)
async def get_instance_stats(instance_id: str):
    status = bots_manager.get_bot_status(instance_id)
    if status["status"] == "error":
        raise HTTPException(status_code=404, detail="Instance not found")
    # Extract PNL from the performance data (this is a placeholder, implement actual PNL calculation)
    pnl = Decimal("0.0")
    for controller in status["performance"].values():
        if "performance" in controller and "pnl" in controller["performance"]:
            pnl += Decimal(str(controller["performance"]["pnl"]))
    return InstanceStats(pnl=pnl)

@router.get("/strategies", response_model=List[Strategy])
async def get_strategies():
    # This is a placeholder. You need to implement a way to get all available strategies and their configurations.
    return [
        Strategy(
            name="simple_market_making",
            parameters={"bid_spread": "float", "ask_spread": "float"},
            min_values={"bid_spread": 0.0, "ask_spread": 0.0},
            max_values={"bid_spread": 1.0, "ask_spread": 1.0},
            default_values={"bid_spread": 0.01, "ask_spread": 0.01}
        )
    ]

@router.post("/strategies/backtest", response_model=BacktestResult)
async def backtest_strategy(backtest_request: BacktestRequest):
    # This is a placeholder. You need to implement the actual backtesting logic.
    return BacktestResult(pnl=Decimal("100.0"))

@router.put("/instance/{instance_id}/start")
async def start_instance(instance_id: str, start_request: StartStrategyRequest):
    response = bots_manager.start_bot(instance_id, script=start_request.strategy_name, conf=start_request.parameters)
    if not response:
        raise HTTPException(status_code=500, detail="Failed to start the instance")
    return {"status": "success", "message": "Instance started successfully"}

@router.post("/strategies/{instance_id}/stop")
async def stop_instance(instance_id: str):
    response = bots_manager.stop_bot(instance_id)
    if not response:
        raise HTTPException(status_code=500, detail="Failed to stop the instance")
    return {"status": "success", "message": "Instance stopped successfully"}