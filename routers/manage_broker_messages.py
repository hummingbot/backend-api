from fastapi import APIRouter

from utils.bots_manager import BotsManager

router = APIRouter(tags=["Manage Broker Messages"])
bots_manager = BotsManager(broker_host='localhost', broker_port=1883, broker_username='admin', broker_password='password')
bots_manager.update_active_bots()


@router.post("/send-message-to-bot")
async def send_message_to_bot(message: str):
    # Logic to communicate with a bot
    return {"status": "message sent"}
