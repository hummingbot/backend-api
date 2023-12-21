from typing import List

from fastapi import APIRouter, HTTPException, UploadFile, File
from starlette import status

from models import ScriptConfig, Script
from utils.file_system_utils import FileSystemUtil

router = APIRouter(tags=["Files Management"])


file_system = FileSystemUtil(base_path="bots")


@router.get("/list-scripts", response_model=List[str])
async def list_scripts():
    return file_system.list_files('scripts')


@router.get("/list-scripts-configs", response_model=List[str])
async def list_scripts_configs():
    return file_system.list_files('scripts_configs')


@router.get("/list-credentials", response_model=List[str])
async def list_credentials():
    return file_system.list_folders('credentials')


@router.post("/add-script", status_code=status.HTTP_201_CREATED)
async def add_script(script: Script, override: bool = False):
    try:
        file_system.add_file('scripts', script.name + '.py', script.content, override)
        return {"message": "Script added successfully."}
    except FileExistsError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/upload-script")
async def upload_script(config_file: UploadFile = File(...), override: bool = False):
    try:
        contents = await config_file.read()
        file_system.add_file('scripts', config_file.filename, contents.decode(), override)
        return {"message": "Script uploaded successfully."}
    except FileExistsError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/add-script-config", status_code=status.HTTP_201_CREATED)
async def add_script_config(config: ScriptConfig):
    try:
        # Convert the dictionary back to a YAML string
        import yaml
        yaml_content = yaml.dump(config.content)

        file_system.add_file('scripts_configs', config.name + '.yml', yaml_content, override=True)
        return {"message": "Script configuration uploaded successfully."}
    except Exception as e:  # Consider more specific exception handling
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/upload-script-config")
async def upload_script_config(config_file: UploadFile = File(...), override: bool = False):
    try:
        contents = await config_file.read()
        file_system.add_file('scripts_configs', config_file.filename, contents.decode(), override)
        return {"message": "Script configuration uploaded successfully."}
    except FileExistsError as e:
        raise HTTPException(status_code=400, detail=str(e))
