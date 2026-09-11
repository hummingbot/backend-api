import logging
import os
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from deps import get_bot_archiver, get_bots_orchestrator, get_docker_service
from models import StartBotAction, StopBotAction, V2ControllerDeployment, V2ScriptDeployment
from services.bots_orchestrator import BotsOrchestrator
from services.docker_service import DockerService
from utils.bot_archiver import BotArchiver
from utils.file_system import fs_util

# Create module-specific logger
logger = logging.getLogger(__name__)

router = APIRouter(tags=["Bot Orchestration"], prefix="/bot-orchestration")


@router.get("/status")
def get_active_bots_status(bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)):
    """
    Get the status of all active bots.

    Args:
        bots_manager: Bot orchestrator service dependency

    Returns:
        Dictionary with status and data containing all active bot statuses
    """
    return {"status": "success", "data": bots_manager.get_all_bots_status()}


@router.get("/mqtt")
def get_mqtt_status(bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)):
    """
    Get MQTT connection status and discovered bots.

    Args:
        bots_manager: Bot orchestrator service dependency

    Returns:
        Dictionary with MQTT connection status, discovered bots, and broker information
    """
    mqtt_connected = bots_manager.mqtt_manager.is_connected
    discovered_bots = bots_manager.mqtt_manager.get_discovered_bots()
    active_bots = list(bots_manager.active_bots.keys())

    # Check client state
    client_state = "connected" if bots_manager.mqtt_manager.is_connected else "disconnected"

    return {
        "status": "success",
        "data": {
            "mqtt_connected": mqtt_connected,
            "discovered_bots": discovered_bots,
            "active_bots": active_bots,
            "broker_host": bots_manager.broker_host,
            "broker_port": bots_manager.broker_port,
            "broker_username": bots_manager.broker_username,
            "client_state": client_state
        }
    }


@router.get("/controller-performance-latest")
async def get_latest_controller_performance(
    bot_name: str = None,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Get the most recent performance snapshot for each bot/controller.
    Optionally filter by bot_name.
    """
    try:
        snapshots = await bots_manager.get_latest_controller_performance(bot_name=bot_name)
        return {"status": "success", "data": snapshots}
    except Exception as e:
        logger.error(f"Failed to get latest controller performance: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/controller-performance-history")
async def get_controller_performance_history(
    bot_name: str = None,
    controller_id: str = None,
    limit: int = Query(default=100, le=1000),
    cursor: str = None,
    start_time: str = None,
    end_time: str = None,
    interval: str = Query(default="5m", pattern="^(5m|15m|30m|1h|4h|12h|1d)$"),
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Get historical controller performance snapshots with pagination and interval sampling.
    """
    try:
        parsed_start = datetime.fromisoformat(start_time) if start_time else None
        parsed_end = datetime.fromisoformat(end_time) if end_time else None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid datetime format: {e}")

    try:
        history, next_cursor, has_more = await bots_manager.get_controller_performance_history(
            bot_name=bot_name,
            controller_id=controller_id,
            limit=limit,
            cursor=cursor,
            start_time=parsed_start,
            end_time=parsed_end,
            interval=interval
        )
        return {
            "status": "success",
            "data": history,
            "pagination": {
                "next_cursor": next_cursor,
                "has_more": has_more,
                "limit": limit,
                "interval": interval,
            }
        }
    except Exception as e:
        logger.error(f"Failed to get controller performance history: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{bot_name}/status")
def get_bot_status(bot_name: str, bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)):
    """
    Get the status of a specific bot.

    Args:
        bot_name: Name of the bot to get status for
        bots_manager: Bot orchestrator service dependency

    Returns:
        Dictionary with bot status information

    Raises:
        HTTPException: 404 if bot not found
    """
    response = bots_manager.get_bot_status(bot_name)
    if not response:
        raise HTTPException(status_code=404, detail="Bot not found")
    return {
        "status": "success",
        "data": response
    }


@router.get("/{bot_name}/history")
async def get_bot_history(
    bot_name: str,
    days: int = 0,
    verbose: bool = False,
    precision: int = None,
    timeout: float = 30.0,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Get trading history for a bot with optional parameters.

    Args:
        bot_name: Name of the bot to get history for
        days: Number of days of history to retrieve (0 for all)
        verbose: Whether to include verbose output
        precision: Decimal precision for numerical values
        timeout: Timeout in seconds for the operation
        bots_manager: Bot orchestrator service dependency

    Returns:
        Dictionary with bot trading history
    """
    response = await bots_manager.get_bot_history(
        bot_name,
        days=days,
        verbose=verbose,
        precision=precision,
        timeout=timeout
    )
    return {"status": "success", "response": response}


@router.post("/start-bot")
async def start_bot(
    action: StartBotAction,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Start a bot with the specified configuration.

    Args:
        action: StartBotAction containing bot configuration parameters
        bots_manager: Bot orchestrator service dependency

    Returns:
        Dictionary with status and response from bot start operation
    """
    response = await bots_manager.start_bot(
        action.bot_name, log_level=action.log_level, script=action.script,
        conf=action.conf, async_backend=action.async_backend
    )

    # Bot run tracking simplified - only track deployment and stop times

    return {"status": "success", "response": response}


@router.post("/stop-bot")
async def stop_bot(
    action: StopBotAction,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Stop a bot with the specified configuration.

    Args:
        action: StopBotAction containing bot stop parameters
        bots_manager: Bot orchestrator service dependency

    Returns:
        Dictionary with status and response from bot stop operation
    """
    # Capture final status BEFORE stopping (performance data is cleared on stop)
    final_status = None
    try:
        final_status = bots_manager.get_bot_status(action.bot_name)
        logger.info(f"Captured final status for {action.bot_name} before stopping")
    except Exception as e:
        logger.warning(f"Failed to capture final status for {action.bot_name}: {e}")

    response = await bots_manager.stop_bot(
        action.bot_name, skip_order_cancellation=action.skip_order_cancellation,
        async_backend=action.async_backend
    )

    # Update bot run status to STOPPED if stop was successful
    if response.get("success"):
        try:
            await bots_manager.mark_bot_run_stopped(action.bot_name, final_status=final_status)
        except Exception as e:
            logger.error(f"Failed to update bot run status: {e}")
            # Don't fail the stop operation if bot run update fails

    return {"status": "success", "response": response}


@router.get("/bot-runs")
async def get_bot_runs(
    bot_name: str = None,
    account_name: str = None,
    strategy_type: str = None,
    strategy_name: str = None,
    run_status: str = None,
    deployment_status: str = None,
    limit: int = 100,
    offset: int = 0,
    include_final_status: bool = False,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Get bot runs with optional filtering.

    Args:
        bot_name: Filter by bot name
        account_name: Filter by account name
        strategy_type: Filter by strategy type (script or controller)
        strategy_name: Filter by strategy name
        run_status: Filter by run status (CREATED, RUNNING, STOPPED, ERROR)
        deployment_status: Filter by deployment status (DEPLOYED, FAILED, ARCHIVED)
        limit: Maximum number of results to return
        offset: Number of results to skip
        include_final_status: Include the final status snapshot for each run. Off by
            default because the blob can be ~89 KB per record (~99% of the payload);
            use GET /bot-runs/{bot_run_id} to fetch it for a single run.
        bots_manager: Bot orchestrator service dependency

    Returns:
        List of bot runs with their details
    """
    try:
        runs_data = await bots_manager.get_bot_runs(
            bot_name=bot_name,
            account_name=account_name,
            strategy_type=strategy_type,
            strategy_name=strategy_name,
            run_status=run_status,
            deployment_status=deployment_status,
            limit=limit,
            offset=offset,
            include_final_status=include_final_status
        )

        return {
            "status": "success",
            "data": runs_data,
            "total": len(runs_data),
            "limit": limit,
            "offset": offset
        }
    except Exception as e:
        logger.error(f"Failed to get bot runs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/bot-runs/stats")
async def get_bot_run_stats(
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Get statistics about bot runs.

    Args:
        bots_manager: Bot orchestrator service dependency

    Returns:
        Bot run statistics
    """
    try:
        stats = await bots_manager.get_bot_run_stats()
        return {"status": "success", "data": stats}
    except Exception as e:
        logger.error(f"Failed to get bot run stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/bot-runs/{bot_run_id}")
async def get_bot_run_by_id(
    bot_run_id: int,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Get a specific bot run by ID.

    Args:
        bot_run_id: ID of the bot run
        bots_manager: Bot orchestrator service dependency

    Returns:
        Bot run details

    Raises:
        HTTPException: 404 if bot run not found
    """
    try:
        run_dict = await bots_manager.get_bot_run_by_id(bot_run_id)

        if not run_dict:
            raise HTTPException(status_code=404, detail=f"Bot run {bot_run_id} not found")

        return {"status": "success", "data": run_dict}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get bot run {bot_run_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/bot-runs/{bot_run_id}")
async def delete_bot_run(
    bot_run_id: int,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Delete a bot run record by ID.

    Args:
        bot_run_id: ID of the bot run to delete
        bots_manager: Bot orchestrator service dependency

    Returns:
        Confirmation of deletion

    Raises:
        HTTPException: 404 if bot run not found
    """
    try:
        result = await bots_manager.delete_bot_run(bot_run_id)

        if not result:
            raise HTTPException(status_code=404, detail=f"Bot run {bot_run_id} not found")

        return {
            "status": "success",
            "message": f"Bot run {bot_run_id} deleted successfully",
            "bot_name": result["bot_name"],
            "archived_folder_deleted": result["archived_folder_deleted"]
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete bot run {bot_run_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/stop-and-archive-bot/{bot_name}")
async def stop_and_archive_bot(
    bot_name: str,
    background_tasks: BackgroundTasks,
    skip_order_cancellation: bool = True,
    archive_locally: bool = True,
    s3_bucket: str = None,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator),
    docker_manager: DockerService = Depends(get_docker_service),
    bot_archiver: BotArchiver = Depends(get_bot_archiver)
):
    """
    Gracefully stop a bot and archive its data in the background.
    This initiates a background task that will:
    1. Stop the bot trading process via MQTT
    2. Wait 15 seconds for graceful shutdown
    3. Monitor and stop the Docker container
    4. Archive the bot data (locally or to S3)
    5. Remove the container

    Returns immediately with a success message while the process continues in the background.
    """
    try:
        # The bot name is also the container name and the MQTT identity, so it is
        # used verbatim everywhere below (no "hummingbot-" prefix is added anywhere).
        active_bots = list(bots_manager.active_bots.keys())

        if bot_name not in active_bots:
            return {
                "status": "error",
                "message": (
                    f"Bot '{bot_name}' not found in active bots. "
                    f"Active bots: {active_bots}. Cannot perform graceful shutdown."
                ),
                "details": {
                    "bot_name": bot_name,
                    "active_bots": active_bots,
                    "reason": "Bot must be actively managed via MQTT for graceful shutdown"
                }
            }

        # Add the background task
        background_tasks.add_task(
            bots_manager.stop_and_archive_bot,
            bot_name=bot_name,
            skip_order_cancellation=skip_order_cancellation,
            archive_locally=archive_locally,
            s3_bucket=s3_bucket,
            docker_manager=docker_manager,
            bot_archiver=bot_archiver
        )

        return {
            "status": "success",
            "message": f"Stop and archive process started for bot {bot_name}",
            "details": {
                "bot_name": bot_name,
                "process": (
                    "The bot will be gracefully stopped, archived, and removed in the background. "
                    "This process typically takes 20-30 seconds."
                )
            }
        }

    except Exception as e:
        logging.error(f"Error initiating stop_and_archive_bot for {bot_name}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/deploy-v2-controllers")
async def deploy_v2_controllers(
    deployment: V2ControllerDeployment,
    docker_manager: DockerService = Depends(get_docker_service),
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Deploy a V2 strategy with controllers by generating the script config and creating the instance.
    This endpoint simplifies the deployment process for V2 controller strategies.

    Args:
        deployment: V2ControllerDeployment configuration
        docker_manager: Docker service dependency

    Returns:
        Dictionary with deployment response and generated configuration details

    Raises:
        HTTPException: 500 if deployment fails
    """
    try:
        # Generate unique script config filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        script_config_filename = f"{deployment.instance_name}-{timestamp}.yml"
        # Use the same name with timestamp for the instance to ensure uniqueness
        unique_instance_name = f"{deployment.instance_name}-{timestamp}"

        # Ensure controller config names have .yml extension
        controllers_with_extension = []
        for controller in deployment.controllers_config:
            if not controller.endswith('.yml'):
                controllers_with_extension.append(f"{controller}.yml")
            else:
                controllers_with_extension.append(controller)

        # Create the script config content
        # Note: candles_config and markets removed - they're optional and empty,
        # and older hummingbot versions don't expect them in the config
        script_config_content = {
            "script_file_name": "v2_with_controllers.py",
            "controllers_config": controllers_with_extension,
        }

        # Add optional drawdown parameters if provided
        if deployment.max_global_drawdown_quote is not None:
            script_config_content["max_global_drawdown_quote"] = deployment.max_global_drawdown_quote
        if deployment.max_controller_drawdown_quote is not None:
            script_config_content["max_controller_drawdown_quote"] = deployment.max_controller_drawdown_quote

        # Save the script config to the scripts directory
        scripts_dir = os.path.join("conf", "scripts")

        script_config_path = os.path.join(scripts_dir, script_config_filename)
        fs_util.dump_dict_to_yaml(script_config_path, script_config_content)

        logging.info(f"Generated script config: {script_config_filename} with content: {script_config_content}")

        # Set generated config on the deployment and deploy
        deployment.instance_name = unique_instance_name
        deployment.script_config = script_config_filename
        response = docker_manager.create_hummingbot_instance(deployment)

        if response.get("success"):
            response["script_config_generated"] = script_config_filename
            response["controllers_deployed"] = deployment.controllers_config
            response["unique_instance_name"] = unique_instance_name

            # Track bot run if deployment was successful
            await bots_manager.create_bot_run(
                bot_name=unique_instance_name,
                instance_name=unique_instance_name,
                strategy_type="controller",
                strategy_name="v2_with_controllers",
                account_name=deployment.credentials_profile,
                config_name=script_config_filename,
                image_version=deployment.image,
                deployment_config=deployment.dict()
            )

        return response

    except Exception as e:
        logging.error(f"Error deploying V2 controllers: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/deploy-v2-script")
async def deploy_v2_script(
    deployment: V2ScriptDeployment,
    docker_manager: DockerService = Depends(get_docker_service),
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Deploy a V2 script bot with optional script configuration.
    This endpoint creates and starts a Hummingbot instance running the specified script.

    Args:
        deployment: V2ScriptDeployment configuration containing instance name, credentials,
                   optional script name and configuration
        docker_manager: Docker service dependency
        db_manager: Database manager dependency

    Returns:
        Dictionary with deployment response including instance details

    Raises:
        HTTPException: 500 if deployment fails
    """
    try:
        # Generate unique instance name with timestamp
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        unique_instance_name = f"{deployment.instance_name}-{timestamp}"

        # Update deployment with unique name
        deployment.instance_name = unique_instance_name

        # Create the hummingbot instance
        response = docker_manager.create_hummingbot_instance(deployment)

        if response.get("success"):
            response["unique_instance_name"] = unique_instance_name

            # Track bot run if deployment was successful
            await bots_manager.create_bot_run(
                bot_name=unique_instance_name,
                instance_name=unique_instance_name,
                strategy_type="script",
                strategy_name=deployment.script or "default",
                account_name=deployment.credentials_profile,
                config_name=deployment.script_config,
                image_version=deployment.image,
                deployment_config=deployment.dict()
            )

        return response

    except Exception as e:
        logging.error(f"Error deploying V2 script: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
