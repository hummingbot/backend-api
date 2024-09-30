from dotenv import load_dotenv
from fastapi import FastAPI
import os


import routers.instances

load_dotenv()
app = FastAPI()

os.environ['FASTAPI_WALLETAUTH_APP'] = 'hummingbot_instance_manager'

app.include_router(routers.instances.router)
