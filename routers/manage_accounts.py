from typing import Dict, List

from fastapi import APIRouter, HTTPException
from hummingbot.client.settings import AllConnectorSettings
from starlette import status

from services.accounts_service import AccountsService
from utils.file_system import FileSystemUtil

router = APIRouter(tags=["Manage Credentials"])
file_system = FileSystemUtil(base_path="bots/credentials")
accounts_service = AccountsService()


@router.on_event("startup")
async def startup_event():
    accounts_service.start_update_account_state_loop()


@router.on_event("shutdown")
async def shutdown_event():
    accounts_service.stop_update_account_state_loop()


@router.get("/accounts-state", response_model=Dict[str, Dict[str, List[Dict]]])
async def get_all_accounts_state():
    return accounts_service.get_accounts_state()


@router.get("/account-state-history", response_model=List[Dict])
async def get_account_state_history():
    """
    Get the historical state of all accounts.
    """
    try:
        history = accounts_service.load_account_state_history()
        return history
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/available-connectors", response_model=List[str])
async def available_connectors():
    return list(AllConnectorSettings.get_connector_settings().keys())


@router.get("/connector-config-map/{connector_name}", response_model=List[str])
async def get_connector_config_map(connector_name: str):
    return accounts_service.get_connector_config_map(connector_name)


@router.get("/all-connectors-config-map", response_model=Dict[str, List[str]])
async def get_all_connectors_config_map():
    all_config_maps = {}
    for connector in list(AllConnectorSettings.get_connector_settings().keys()):
        all_config_maps[connector] = accounts_service.get_connector_config_map(connector)
    return all_config_maps


@router.get("/list-accounts", response_model=List[str])
async def list_accounts():
    return accounts_service.list_accounts()


@router.get("/list-credentials/{account_name}", response_model=List[str])
async def list_credentials(account_name: str):
    try:
        return accounts_service.list_credentials(account_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/add-account", status_code=status.HTTP_201_CREATED)
async def add_account(account_name: str):
    try:
        accounts_service.add_account(account_name)
        return {"message": "Credential added successfully."}
    except FileExistsError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/delete-account")
async def delete_account(account_name: str):
    try:
        if account_name == "master_account":
            raise HTTPException(status_code=400, detail="Cannot delete master account.")
        accounts_service.delete_account(account_name)
        return {"message": "Credential deleted successfully."}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/delete-credential/{account_name}/{connector_name}")
async def delete_credential(account_name: str, connector_name: str):
    try:
        accounts_service.delete_credentials(account_name, connector_name)
        return {"message": "Credential deleted successfully."}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/add-connector-keys/{account_name}/{connector_name}", status_code=status.HTTP_201_CREATED)
async def add_connector_keys(account_name: str, connector_name: str, keys: Dict):
    try:
        await accounts_service.add_connector_keys(account_name, connector_name, keys)
        return {"message": "Connector keys added successfully."}
    except Exception as e:
        accounts_service.delete_credentials(account_name, connector_name)
        raise HTTPException(status_code=400, detail=str(e))
