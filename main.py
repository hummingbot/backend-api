from dotenv import load_dotenv
from fastapi import FastAPI
import os


import routers.instances

load_dotenv()
app = FastAPI()

os.environ['FASTAPI_WALLETAUTH_APP'] = 'robotter-ai'

app.include_router(routers.instances.router)
