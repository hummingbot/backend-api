from dotenv import load_dotenv
from fastapi import FastAPI
from routers import manage_docker, communicate_bots

load_dotenv()
app = FastAPI()

app.include_router(manage_docker.router)
app.include_router(communicate_bots.router)
