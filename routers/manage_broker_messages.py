import json
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from fastapi import APIRouter, HTTPException

from models import StartBotAction, StopBotAction, ImportStrategyAction
from utils.bots_orchestrator import BotsManager
broker_host = os.getenv('BROKER_HOST', 'localhost')
broker_port = int(os.getenv('BROKER_PORT', 1883))
broker_username = os.getenv('BROKER_USERNAME', 'admin')
broker_password = os.getenv('BROKER_PASSWORD', 'password')

# Initialize the scheduler
scheduler = AsyncIOScheduler()
router = APIRouter(tags=["Manage Broker Messages"])
bots_manager = BotsManager(broker_host=broker_host, broker_port=broker_port, broker_username=broker_username, broker_password=broker_password)


def update_active_bots():
    bots_manager.update_active_bots()
    # print("active bots:")
    # for bot, data in bots_manager.active_bots.items():
    #     print(data)


@router.on_event("startup")
async def startup_event():
    # Add the job to the scheduler
    scheduler.add_job(update_active_bots, 'interval', seconds=10)
    scheduler.start()


@router.on_event("shutdown")
async def shutdown_event():
    # Shutdown the scheduler on application exit
    scheduler.shutdown()


@router.get("/get-active-bots-status")
def get_active_bots_status():
    """Returns the cached status of all active bots."""
    if not bots_manager.active_bots:
        raise HTTPException(status_code=404, detail="No active bots found")
    return {"status": "success", "data": bots_manager.get_all_bots_status()}



@router.get("/get-bot-status/{bot_name}")
def get_bot_status(bot_name: str):
    response = bots_manager.get_bot_status(bot_name)
    if not response:
        raise HTTPException(status_code=404, detail="Bot not found")

    status_message = response.get("message", "")
    if "No active maker orders" in status_message:
        running_status = "starting"  # Indicate that the bot is trying to start
    elif "Market connectors are not ready" in status_message:
        running_status = "starting"  # Similar case, bot is starting but not ready
    elif "No strategy is currently running" in status_message:
        running_status = "stopped"   # The bot is stopped
    else:
        running_status = "running"   # Default to running if none of the above

    return {
        "status": "success",
        "data": {
            "bot_name": bot_name,
            "running_status": running_status,
            "details": response
        }
    }


@router.get("/get-bot-history/{bot_name}")
def get_bot_history(bot_name: str):
    response = bots_manager.get_bot_history(bot_name)
    return {"status": "success", "response": response}


@router.post("/start-bot")
def start_bot(action: StartBotAction):
    response = bots_manager.start_bot(action.bot_name, log_level=action.log_level, script=action.script, conf=action.conf, async_backend=action.async_backend)
    return {"status": "success", "response": response}


@router.post("/stop-bot")
def stop_bot(action: StopBotAction):
    response = bots_manager.stop_bot(action.bot_name, skip_order_cancellation=action.skip_order_cancellation, async_backend=action.async_backend)
    return {"status": "success", "response": response}


@router.post("/import-strategy")
def import_strategy(action: ImportStrategyAction):
    response = bots_manager.import_strategy_for_bot(action.bot_name, action.strategy)
    return {"status": "success", "response": response}
