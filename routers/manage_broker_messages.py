import asyncio
import threading
import time

from fastapi import APIRouter, BackgroundTasks

from utils.bots_manager import BotsManager

router = APIRouter(tags=["Manage Broker Messages"])
bots_manager = BotsManager(broker_host='localhost', broker_port=1883, broker_username='admin', broker_password='password')
bots_manager.update_active_bots()


def periodic_task():
    while True:
        bots_manager.update_active_bots()
        print("active bots:")
        for bot, data in bots_manager.active_bots.items():
            print(data['bot_name'])
        time.sleep(10)


@router.on_event("startup")
def startup_event():
    task_thread = threading.Thread(target=periodic_task)
    task_thread.daemon = True  # Ensures that the thread will exit when the main process does
    task_thread.start()

