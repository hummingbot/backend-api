import os

from fastapi import APIRouter, HTTPException, Depends

from models import DockerImage
from utils.bot_archiver import BotArchiver
from utils.docker_images import get_image_tags
from services.docker_service import DockerService
from deps import get_docker_service, get_bot_archiver

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
    return  docker_service.is_docker_running()


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
    return get_image_tags(available_images["images"], image_name)


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
async def remove_container(container_name: str, archive_locally: bool = True, s3_bucket: str = None, docker_service: DockerService = Depends(get_docker_service), bot_archiver: BotArchiver = Depends(get_bot_archiver)):
    """
    Remove a Hummingbot container and optionally archive its bot data.
    
    NOTE: This endpoint only works with Hummingbot containers (names starting with 'hummingbot-')
    as it archives bot-specific data from the bots/instances directory.
    
    Args:
        container_name: Name of the Hummingbot container to remove
        archive_locally: Whether to archive data locally (default: True)
        s3_bucket: S3 bucket name for cloud archiving (optional)
        docker_service: Docker service dependency
        bot_archiver: Bot archiver service dependency
        
    Returns:
        Response from container removal operation
        
    Raises:
        HTTPException: 400 if container is not a Hummingbot container
        HTTPException: 500 if archiving fails
    """
    # Validate that this is a Hummingbot container
    if not container_name.startswith("hummingbot-"):
        raise HTTPException(
            status_code=400, 
            detail=f"This endpoint only removes Hummingbot containers. Container '{container_name}' is not a Hummingbot container."
        )
    
    # Remove the container
    response = docker_service.remove_container(container_name)
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
