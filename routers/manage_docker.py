from fastapi import APIRouter
from docker_manager import DockerManager

router = APIRouter(tags=["Docker Management"])
docker_manager = DockerManager()


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
async def remove_container(container_name: str):
    return docker_manager.remove_container(container_name)


@router.post("/stop-container/{container_name}")
async def stop_container(container_name: str):
    return docker_manager.stop_container(container_name)

@router.post("/start-container/{container_name}")
async def start_container(container_name: str):
    return docker_manager.start_container(container_name)
