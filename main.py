import os
import secrets
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from routers import (
    manage_accounts,
    manage_backtesting,
    manage_broker_messages,
    manage_databases,
    manage_docker,
    manage_files,
    manage_market_data,
    manage_performance,
)

load_dotenv()
security = HTTPBasic()

username = os.getenv("USERNAME", "admin")
password = os.getenv("PASSWORD", "admin")
debug_mode = os.getenv("DEBUG_MODE", False)

app = FastAPI()


def auth_user(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
):
    current_username_bytes = credentials.username.encode("utf8")
    correct_username_bytes = f"{username}".encode("utf8")
    is_correct_username = secrets.compare_digest(
        current_username_bytes, correct_username_bytes
    )
    current_password_bytes = credentials.password.encode("utf8")
    correct_password_bytes = f"{password}".encode("utf8")
    is_correct_password = secrets.compare_digest(
        current_password_bytes, correct_password_bytes
    )
    if not (is_correct_username and is_correct_password) and not debug_mode:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )


app.include_router(manage_docker.router, dependencies=[Depends(auth_user)])
app.include_router(manage_broker_messages.router, dependencies=[Depends(auth_user)])
app.include_router(manage_files.router, dependencies=[Depends(auth_user)])
app.include_router(manage_market_data.router, dependencies=[Depends(auth_user)])
app.include_router(manage_backtesting.router, dependencies=[Depends(auth_user)])
app.include_router(manage_databases.router, dependencies=[Depends(auth_user)])
app.include_router(manage_performance.router, dependencies=[Depends(auth_user)])
app.include_router(manage_accounts.router, dependencies=[Depends(auth_user)])
