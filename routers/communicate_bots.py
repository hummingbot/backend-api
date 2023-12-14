from fastapi import APIRouter

router = APIRouter(tags=["Communicate Bots"])

# Example route
@router.post("/send-message-to-bot")
async def send_message_to_bot(message: str):
    # Logic to communicate with a bot
    return {"status": "message sent"}
