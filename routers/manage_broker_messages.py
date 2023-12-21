import asyncio
import threading
import time
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from fastapi import APIRouter, BackgroundTasks

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



