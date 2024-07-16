import json
from typing import Dict, List

import yaml
from fastapi import APIRouter, File, HTTPException, UploadFile
from starlette import status

from models import Script, ScriptConfig
from utils.file_system import FileSystemUtil

router = APIRouter(tags=["Files Management"])

file_system = FileSystemUtil()


@router.get("/list-scripts", response_model=List[str])
async def list_scripts():
    return file_system.list_files('scripts')


@router.get("/list-scripts-configs", response_model=List[str])
async def list_scripts_configs():
    return file_system.list_files('conf/scripts')


@router.get("/script-config/{script_name}", response_model=dict)
async def get_script_config(script_name: str):
    """
    Retrieves the configuration parameters for a given script.
    :param script_name: The name of the script.
    :return: JSON containing the configuration parameters.
    """
    config_class = file_system.load_script_config_class(script_name)
    if config_class is None:
        raise HTTPException(status_code=404, detail="Script configuration class not found")

    # Extracting fields and default values
    config_fields = {field.name: field.default for field in config_class.__fields__.values()}
    return json.loads(json.dumps(config_fields, default=str))  # Handling non-serializable types like Decimal


@router.get("/list-controllers", response_model=dict)
async def list_controllers():
    directional_trading_controllers = [file for file in file_system.list_files('controllers/directional_trading') if
                                       file != "__init__.py"]
    market_making_controllers = [file for file in file_system.list_files('controllers/market_making') if
                                 file != "__init__.py"]
    return {"directional_trading": directional_trading_controllers, "market_making": market_making_controllers}


@router.get("/list-controllers-configs", response_model=List[str])
async def list_controllers_configs():
    return file_system.list_files('conf/controllers')


@router.get("/controller-config/{controller_name}", response_model=dict)
async def get_controller_config(controller_name: str):
    config = file_system.read_yaml_file(f"bots/conf/controllers/{controller_name}.yml")
    return config


@router.get("/all-controller-configs", response_model=List[dict])
async def get_all_controller_configs():
    configs = []
    for controller in file_system.list_files('conf/controllers'):
        config = file_system.read_yaml_file(f"bots/conf/controllers/{controller}")
        configs.append(config)
    return configs


@router.get("/all-controller-configs/bot/{bot_name}", response_model=List[dict])
async def get_all_controller_configs_for_bot(bot_name: str):
    configs = []
    bots_config_path = f"instances/{bot_name}/conf/controllers"
    if not file_system.path_exists(bots_config_path):
        raise HTTPException(status_code=400, detail="Bot not found.")
    for controller in file_system.list_files(bots_config_path):
        config = file_system.read_yaml_file(f"bots/{bots_config_path}/{controller}")
        configs.append(config)
    return configs


@router.post("/update-controller-config/bot/{bot_name}/{controller_id}")
async def update_controller_config(bot_name: str, controller_id: str, config: Dict):
    bots_config_path = f"instances/{bot_name}/conf/controllers"
    if not file_system.path_exists(bots_config_path):
        raise HTTPException(status_code=400, detail="Bot not found.")
    current_config = file_system.read_yaml_file(f"bots/{bots_config_path}/{controller_id}.yml")
    current_config.update(config)
    file_system.dump_dict_to_yaml(f"bots/{bots_config_path}/{controller_id}.yml", current_config)
    return {"message": "Controller configuration updated successfully."}


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
        yaml_content = yaml.dump(config.content)

        file_system.add_file('conf/scripts', config.name + '.yml', yaml_content, override=True)
        return {"message": "Script configuration uploaded successfully."}
    except Exception as e:  # Consider more specific exception handling
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/upload-script-config")
async def upload_script_config(config_file: UploadFile = File(...), override: bool = False):
    try:
        contents = await config_file.read()
        file_system.add_file('conf/scripts', config_file.filename, contents.decode(), override)
        return {"message": "Script configuration uploaded successfully."}
    except FileExistsError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/add-controller-config", status_code=status.HTTP_201_CREATED)
async def add_controller_config(config: ScriptConfig):
    try:
        yaml_content = yaml.dump(config.content)

        file_system.add_file('conf/controllers', config.name + '.yml', yaml_content, override=True)
        return {"message": "Controller configuration uploaded successfully."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/upload-controller-config")
async def upload_controller_config(config_file: UploadFile = File(...), override: bool = False):
    try:
        contents = await config_file.read()
        file_system.add_file('conf/controllers', config_file.filename, contents.decode(), override)
        return {"message": "Controller configuration uploaded successfully."}
    except FileExistsError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/delete-controller-config", status_code=status.HTTP_200_OK)
async def delete_controller_config(config_name: str):
    try:
        file_system.delete_file('conf/controllers', config_name)
        return {"message": f"Controller configuration {config_name} deleted successfully."}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
