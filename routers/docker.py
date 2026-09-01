import os

from fastapi import APIRouter, Depends, HTTPException

from deps import get_bot_archiver, get_docker_service
from models import DockerImage
from services.docker_service import DockerService
from utils.bot_archiver import BotArchiver

router = APIRouter(tags=["Docker"], prefix="/docker")


@router.get("/running")
async def is_docker_running(docker_service: DockerService = Depends(get_docker_service)):
    """
    Check if Docker daemon is running.

    Args:
        docker_service: Docker service dependency

    Returns:
        Dictionary indicating if Docker is running
    """
    return docker_service.is_docker_running()


@router.get("/available-images/")
async def available_images(image_name: str = None, docker_service: DockerService = Depends(get_docker_service)):
    """
    Get available Docker images matching the specified name.

    Args:
        image_name: Name pattern to search for in image tags
        docker_service: Docker service dependency

    Returns:
        Dictionary with list of available image tags
    """
    available_images = docker_service.get_available_images()
    if image_name:
        return [tag for image in available_images["images"] for tag in image.tags if image_name in tag]
    return [tag for tag in available_images["images"]]


@router.get("/active-containers")
async def active_containers(name_filter: str = None, docker_service: DockerService = Depends(get_docker_service)):
    """
    Get all currently active (running) Docker containers.

    Args:
        name_filter: Optional filter to match container names (case-insensitive)
        docker_service: Docker service dependency

    Returns:
        List of active container information
    """
    return docker_service.get_active_containers(name_filter)


@router.get("/exited-containers")
async def exited_containers(name_filter: str = None, docker_service: DockerService = Depends(get_docker_service)):
    """
    Get all exited (stopped) Docker containers.

    Args:
        name_filter: Optional filter to match container names (case-insensitive)
        docker_service: Docker service dependency

    Returns:
        List of exited container information
    """
    return docker_service.get_exited_containers(name_filter)


@router.post("/clean-exited-containers")
async def clean_exited_containers(docker_service: DockerService = Depends(get_docker_service)):
    """
    Remove all exited Docker containers to free up space.

    Args:
        docker_service: Docker service dependency

    Returns:
        Response from cleanup operation
    """
    return docker_service.clean_exited_containers()


@router.post("/remove-container/{container_name}")
async def remove_container(
    container_name: str,
    archive_locally: bool = True,
    s3_bucket: str = None,
    docker_service: DockerService = Depends(get_docker_service),
    bot_archiver: BotArchiver = Depends(get_bot_archiver),
):
    """
    Remove a bot container created by this API and archive its bot data.

    NOTE: This endpoint only works with containers this API manages. A bot container is named
    after its instance verbatim, and owns the bots/instances/<container_name> directory that this
    endpoint archives; an unrelated container on the host has no such directory and is refused.

    Args:
        container_name: Name of the bot container to remove
        archive_locally: Whether to archive data locally (default: True)
        s3_bucket: S3 bucket name for cloud archiving (optional)
        docker_service: Docker service dependency
        bot_archiver: Bot archiver service dependency

    Returns:
        Response from container removal operation

    Raises:
        HTTPException: 400 if the container is not a bot managed by this API
        HTTPException: 500 if archiving fails
    """
    # Validate that this container belongs to a bot this API created, by the only marker that
    # actually exists: its instance directory. Container names carry no prefix.
    try:
        instance_dir = DockerService.resolve_instance_dir(container_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not os.path.isdir(instance_dir):
        raise HTTPException(
            status_code=400,
            detail=f"This endpoint only removes bot containers managed by this API. Container "
                   f"'{container_name}' has no instance directory at '{instance_dir}'."
        )

    # Remove the container
    response = docker_service.remove_container(container_name)
    try:
        # Archive the data
        if archive_locally:
            bot_archiver.archive_locally(container_name, instance_dir)
        else:
            bot_archiver.archive_and_upload(container_name, instance_dir, bucket_name=s3_bucket)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return response


@router.post("/stop-container/{container_name}")
async def stop_container(container_name: str, docker_service: DockerService = Depends(get_docker_service)):
    """
    Stop a running Docker container.

    Args:
        container_name: Name of the container to stop
        docker_service: Docker service dependency

    Returns:
        Response from container stop operation
    """
    return docker_service.stop_container(container_name)


@router.post("/start-container/{container_name}")
async def start_container(container_name: str, docker_service: DockerService = Depends(get_docker_service)):
    """
    Start a stopped Docker container.

    Args:
        container_name: Name of the container to start
        docker_service: Docker service dependency

    Returns:
        Response from container start operation
    """
    return docker_service.start_container(container_name)


@router.post("/pull-image/")
async def pull_image(image: DockerImage, docker_service: DockerService = Depends(get_docker_service)):
    """
    Initiate Docker image pull as background task.
    Returns immediately with task status for monitoring.

    Args:
        image: DockerImage object containing the image name to pull
        docker_service: Docker service dependency

    Returns:
        Status of the pull operation initiation
    """
    result = docker_service.pull_image_async(image.image_name)
    return result


@router.get("/pull-status/")
async def get_pull_status(docker_service: DockerService = Depends(get_docker_service)):
    """
    Get status of all pull operations.

    Args:
        docker_service: Docker service dependency

    Returns:
        Dictionary with all pull operations and their statuses
    """
    return docker_service.get_all_pull_status()
