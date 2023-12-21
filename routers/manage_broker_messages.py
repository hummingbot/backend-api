from apscheduler.schedulers.asyncio import AsyncIOScheduler

from fastapi import APIRouter

from models import StartBotAction, StopBotAction, ImportStrategyAction
from utils.bots_manager import BotsManager

# Initialize the scheduler
scheduler = AsyncIOScheduler()
router = APIRouter(tags=["Manage Broker Messages"])
bots_manager = BotsManager(broker_host='localhost', broker_port=1883, broker_username='admin', broker_password='password')


def update_active_bots():
    bots_manager.update_active_bots()
    print("active bots:")
    for bot, data in bots_manager.active_bots.items():
        print(data['bot_name'])


@router.on_event("startup")
async def startup_event():
    # Add the job to the scheduler
    scheduler.add_job(update_active_bots, 'interval', seconds=10)
    scheduler.start()


@router.on_event("shutdown")
async def shutdown_event():
    # Shutdown the scheduler on application exit
    scheduler.shutdown()


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


@router.get("/get-bot-status/{bot_name}")
def get_bot_status(bot_name: str):
    response = bots_manager.get_bot_status(bot_name)
    return {"status": "success", "response": response}


@router.get("/get-bot-history/{bot_name}")
def get_bot_history(bot_name: str):
    response = bots_manager.get_bot_history(bot_name)
    return {"status": "success", "response": response}
