import os

from fastapi import APIRouter, HTTPException
import logging
from models import HummingbotInstanceConfig
from services import DockerManager
from utils.bot_archiver import BotArchiver

router = APIRouter(tags=["Docker Management"])
docker_manager = DockerManager()
bot_archiver = BotArchiver(os.environ.get("AWS_API_KEY"), os.environ.get("AWS_SECRET_KEY"),
                           os.environ.get("S3_DEFAULT_BUCKET_NAME"))


@router.get("/is-docker-running")
async def is_docker_running():
    return {"is_docker_running": docker_manager.is_docker_running()}


@router.get("/active-containers")
async def active_containers():
    return {"active_containers": docker_manager.get_active_containers()}


@router.get("/exited-containers")
async def exited_containers():
    return {"exited_containers": docker_manager.get_exited_containers()}


@router.post("/clean-exited-containers")
async def clean_exited_containers():
    return {"clean_exited_containers": docker_manager.clean_exited_containers()}


@router.post("/remove-container/{container_name}")
async def remove_container(container_name: str, archive_locally: bool = True, s3_bucket: str = None):
    # Form the instance directory path correctly
    instance_dir = os.path.join('bots', 'instances', container_name)

    try:
        # Archive the data
        if archive_locally:
            bot_archiver.archive_locally(container_name, instance_dir)
        else:
            bot_archiver.archive_and_upload(container_name, instance_dir, bucket_name=s3_bucket)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Remove the container
    response = docker_manager.remove_container(container_name)
    return response

@router.post("/stop-container/{container_name}")
async def stop_container(container_name: str):
    return docker_manager.stop_container(container_name)


@router.post("/start-container/{container_name}")
async def start_container(container_name: str):
    return docker_manager.start_container(container_name)


@router.post("/create-hummingbot-instance")
async def create_hummingbot_instance(config: HummingbotInstanceConfig):
    logging.info(f"Creating hummingbot instance with config: {config}")
    response = docker_manager.create_hummingbot_instance(config)
    return response
