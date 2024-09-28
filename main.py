from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi_walletauth import jwt_authorization_router as authorization_routes


from routers import manage_accounts, manage_backtesting, manage_broker_messages, manage_docker, manage_files, manage_market_data

load_dotenv()
app = FastAPI()

app.include_router(authorization_routes)
app.include_router(manage_docker.router)
app.include_router(manage_broker_messages.router)
app.include_router(manage_files.router)
app.include_router(manage_market_data.router)
app.include_router(manage_backtesting.router)
app.include_router(manage_accounts.router)
