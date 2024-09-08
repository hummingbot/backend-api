from dotenv import load_dotenv
from fastapi import FastAPI

import routers.instances

load_dotenv()
app = FastAPI()

app.include_router(routers.instances.router)
