from fastapi import APIRouter

router = APIRouter(tags=["Files Management"])


@router.get("/list-scripts")
async def list_scripts():
    pass


@router.get("/list-scripts-configs")
async def list_scripts_configs():
    pass


@router.get("/list-credentials")
async def list_credentials():
    pass


@router.post("/add-script")
async def add_script():
    pass


@router.post("/add-script-config")
async def add_script_config():
    pass
