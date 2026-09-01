import asyncio
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import docker

from database import AsyncDatabaseManager, BotRunRepository, ControllerPerformanceRepository
from services.docker_service import DockerService
from utils.bot_archiver import BotArchiver
from utils.mqtt_manager import MQTTManager

logger = logging.getLogger(__name__)


class BotsOrchestrator:
    """Orchestrates Hummingbot instances using Docker and MQTT communication."""

    def __init__(self, broker_host, broker_port, broker_username, broker_password,
                 db_manager: AsyncDatabaseManager, performance_dump_interval: int = 5):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.broker_username = broker_username
        self.broker_password = broker_password

        # Initialize Docker client
        self.docker_client = docker.from_env()

        # Initialize MQTT manager
        self.mqtt_manager = MQTTManager(host=broker_host, port=broker_port, username=broker_username, password=broker_password)

        # Active bots tracking
        self.active_bots = {}
        self._update_bots_task: Optional[asyncio.Task] = None

        # Track bots that are currently being stopped and archived
        self.stopping_bots = set()

        # Controller performance dump (similar to AccountsService.dump_account_state)
        self.performance_dump_interval = performance_dump_interval * 60  # Convert minutes to seconds
        self._performance_dump_task: Optional[asyncio.Task] = None
        # Shared manager injected from main.py; tables are created once at startup,
        # so no per-service bootstrap is needed here.
        self.db_manager = db_manager

        # MQTT manager will be started asynchronously later

    @staticmethod
    def hummingbot_containers_fiter(container):
        """Filter for Hummingbot containers based on image name pattern."""
        try:
            # Get the image name (first tag if available, otherwise the image ID)
            image_name = container.image.tags[0] if container.image.tags else str(container.image)
            pattern = r'.+/hummingbot:'
            return bool(re.match(pattern, image_name))
        except Exception:
            return False

    async def get_active_containers(self):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_get_active_containers)

    def _sync_get_active_containers(self):
        return [
            container.name
            for container in self.docker_client.containers.list()
            if container.status == "running" and self.hummingbot_containers_fiter(container)
        ]

    def start(self):
        """Start the loop that monitors active bots."""
        # Start MQTT manager and update loop in async context
        self._update_bots_task = asyncio.create_task(self._start_async())

        # Start controller performance dump loop
        self._performance_dump_task = asyncio.create_task(self._performance_dump_loop())
        logger.info(f"Controller performance dump started ({self.performance_dump_interval}s interval)")

    async def _start_async(self):
        """Start MQTT manager and update loop asynchronously."""
        logger.info("Starting MQTT manager...")
        await self.mqtt_manager.start()

        # Then start the update loop
        await self.update_active_bots()

    async def stop(self):
        """Stop the active bots monitoring loop."""
        if self._update_bots_task:
            self._update_bots_task.cancel()
            try:
                await self._update_bots_task
            except asyncio.CancelledError:
                pass
        self._update_bots_task = None

        if self._performance_dump_task:
            self._performance_dump_task.cancel()
            try:
                await self._performance_dump_task
            except asyncio.CancelledError:
                pass
        self._performance_dump_task = None

        # Stop MQTT manager
        await self.mqtt_manager.stop()

    async def update_active_bots(self, sleep_time=1.0):
        """Monitor and update active bots list using both Docker and MQTT discovery."""
        while True:
            try:
                # Get bots from Docker containers
                docker_bots = await self.get_active_containers()

                # Get bots from MQTT messages (auto-discovered)
                mqtt_bots = self.mqtt_manager.get_discovered_bots(timeout_seconds=30)  # 30 second timeout

                # Combine both sources
                all_active_bots = set([bot for bot in docker_bots + mqtt_bots if not self.is_bot_stopping(bot)])

                # Remove bots that are no longer active
                for bot_name in list(self.active_bots):
                    if bot_name not in all_active_bots:
                        self.mqtt_manager.clear_bot_data(bot_name)
                        del self.active_bots[bot_name]

                # Add new bots
                for bot_name in all_active_bots:
                    if bot_name not in self.active_bots:
                        self.active_bots[bot_name] = {
                            "bot_name": bot_name,
                            "status": "connected",
                            "source": "docker" if bot_name in docker_bots else "mqtt",
                        }
                        # Subscribe to this specific bot's topics
                        await self.mqtt_manager.subscribe_to_bot(bot_name)

            except Exception as e:
                logger.error(f"Error in update_active_bots: {e}", exc_info=True)

            await asyncio.sleep(sleep_time)

    # Interact with a specific bot
    async def start_bot(self, bot_name, **kwargs):
        """
        Start a bot with optional script.
        Maintains backward compatibility with kwargs.
        """
        if bot_name not in self.active_bots:
            logger.warning(f"Bot {bot_name} not found in active bots")
            return {"success": False, "message": f"Bot {bot_name} not found"}

        # Create StartCommandMessage.Request format
        data = {
            "log_level": kwargs.get("log_level"),
            "script": kwargs.get("script"),
            "conf": kwargs.get("conf"),
            "is_quickstart": kwargs.get("is_quickstart", False),
            "async_backend": kwargs.get("async_backend", True),
        }

        success = await self.mqtt_manager.publish_command(bot_name, "start", data)
        return {"success": success}

    async def stop_bot(self, bot_name, **kwargs):
        """
        Stop a bot.
        Maintains backward compatibility with kwargs.
        """
        if bot_name not in self.active_bots:
            logger.warning(f"Bot {bot_name} not found in active bots")
            return {"success": False, "message": f"Bot {bot_name} not found"}

        # Create StopCommandMessage.Request format
        data = {
            "skip_order_cancellation": kwargs.get("skip_order_cancellation", False),
            "async_backend": kwargs.get("async_backend", True),
        }

        success = await self.mqtt_manager.publish_command(bot_name, "stop", data)

        # Clear performance data after stop command to immediately reflect stopped status
        if success:
            self.mqtt_manager.clear_bot_controller_reports(bot_name)

        return {"success": success}

    async def import_strategy_for_bot(self, bot_name, strategy, **kwargs):
        """
        Import a strategy configuration for a bot.
        Maintains backward compatibility.
        """
        if bot_name not in self.active_bots:
            logger.warning(f"Bot {bot_name} not found in active bots")
            return {"success": False, "message": f"Bot {bot_name} not found"}

        # Create ImportCommandMessage.Request format
        data = {"strategy": strategy}
        success = await self.mqtt_manager.publish_command(bot_name, "import_strategy", data)
        return {"success": success}

    async def configure_bot(self, bot_name, params, **kwargs):
        """
        Configure bot parameters.
        Maintains backward compatibility.
        """
        if bot_name not in self.active_bots:
            logger.warning(f"Bot {bot_name} not found in active bots")
            return {"success": False, "message": f"Bot {bot_name} not found"}

        # Create ConfigCommandMessage.Request format
        data = {"params": params}
        success = await self.mqtt_manager.publish_command(bot_name, "config", data)
        return {"success": success}

    async def get_bot_history(self, bot_name, **kwargs):
        """
        Request bot trading history and wait for the response.
        Maintains backward compatibility.
        """
        if bot_name not in self.active_bots:
            logger.warning(f"Bot {bot_name} not found in active bots")
            return {"success": False, "message": f"Bot {bot_name} not found"}

        # Create HistoryCommandMessage.Request format
        data = {
            "days": kwargs.get("days", 0),
            "verbose": kwargs.get("verbose", False),
            "precision": kwargs.get("precision"),
            "async_backend": kwargs.get("async_backend", False),
        }

        # Use the new RPC method to wait for response
        timeout = kwargs.get("timeout", 30.0)  # Default 30 second timeout
        response = await self.mqtt_manager.publish_command_and_wait(bot_name, "history", data, timeout=timeout)

        if response is None:
            return {
                "success": False,
                "message": f"No response received from {bot_name} within {timeout} seconds",
                "timeout": True,
            }

        return {"success": True, "data": response}

    @staticmethod
    def determine_controller_performance(controller_reports):
        """Process controller reports and extract performance and custom_info.

        Args:
            controller_reports: Dict with controller_id as key and report dict as value.
                New format: Each report contains 'performance' and 'custom_info' keys.
                Old format: Report contains performance metrics directly (backward compatible).

        Returns:
            Dict with cleaned controller data including status, performance, and custom_info.
        """
        cleaned_data = {}
        for controller_id, report in controller_reports.items():
            try:
                # Support both new format (nested) and old format (flat)
                # New format: {"performance": {...}, "custom_info": {...}}
                # Old format: {...performance metrics directly...}
                if "performance" in report:
                    # New format with nested structure
                    performance = report.get("performance", {})
                    custom_info = report.get("custom_info", {})
                else:
                    # Old format - metrics are directly in the report
                    performance = report
                    custom_info = {}

                # Validate performance metrics are numeric (skip known non-numeric fields)
                non_numeric_fields = ("positions_summary", "close_type_counts")
                _ = sum(
                    metric for key, metric in performance.items()
                    if key not in non_numeric_fields and isinstance(metric, (int, float))
                )

                cleaned_data[controller_id] = {
                    "status": "running",
                    "performance": performance,
                    "custom_info": custom_info
                }
            except Exception as e:
                # Handle both formats in error case too
                if "performance" in report:
                    perf = report.get("performance", {})
                    info = report.get("custom_info", {})
                else:
                    perf = report
                    info = {}
                cleaned_data[controller_id] = {
                    "status": "error",
                    "error": f"Error processing controller data: {e}",
                    "performance": perf,
                    "custom_info": info
                }
        return cleaned_data

    def get_all_bots_status(self):
        """Get status information for all active bots."""
        all_bots_status = {}
        for bot in [bot for bot in self.active_bots if not self.is_bot_stopping(bot)]:
            status = self.get_bot_status(bot)
            status["source"] = self.active_bots[bot].get("source", "unknown")
            all_bots_status[bot] = status
        return all_bots_status

    def get_bot_status(self, bot_name):
        """
        Get status information for a specific bot.
        """
        if bot_name not in self.active_bots:
            return {"status": "not_found", "error": f"Bot {bot_name} not found"}

        try:
            # Check if bot is currently being stopped and archived
            if bot_name in self.stopping_bots:
                return {
                    "status": "stopping",
                    "message": "Bot is currently being stopped and archived",
                    "performance": {},
                    "error_logs": [],
                    "general_logs": [],
                    "recently_active": False,
                }

            # Get data from MQTT manager
            controller_reports = self.mqtt_manager.get_bot_controller_reports(bot_name)
            performance = self.determine_controller_performance(controller_reports)
            error_logs = self.mqtt_manager.get_bot_error_logs(bot_name)
            general_logs = self.mqtt_manager.get_bot_logs(bot_name)

            # Check if bot has sent recent messages (within last 30 seconds)
            discovered_bots = self.mqtt_manager.get_discovered_bots(timeout_seconds=30)
            recently_active = bot_name in discovered_bots

            # Determine status based on performance data and recent activity
            if len(performance) > 0 and recently_active:
                status = "running"
            elif len(performance) > 0 and not recently_active:
                status = "idle"  # Has performance data but no recent activity
            else:
                status = "stopped"

            return {
                "status": status,
                "performance": performance,
                "error_logs": error_logs,
                "general_logs": general_logs,
                "recently_active": recently_active,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def set_bot_stopping(self, bot_name: str):
        """Mark a bot as currently being stopped and archived."""
        self.stopping_bots.add(bot_name)
        logger.info(f"Marked bot {bot_name} as stopping")

    def clear_bot_stopping(self, bot_name: str):
        """Clear the stopping status for a bot."""
        self.stopping_bots.discard(bot_name)
        logger.info(f"Cleared stopping status for bot {bot_name}")

    def is_bot_stopping(self, bot_name: str) -> bool:
        """Check if a bot is currently being stopped."""
        return bot_name in self.stopping_bots

    # ============================================
    # Controller Performance Snapshots
    # ============================================

    async def _performance_dump_loop(self):
        """Periodically dump controller performance to the database (default every 5 minutes)."""
        while True:
            try:
                await self.dump_controller_performance()
            except Exception as e:
                logger.error(f"Error dumping controller performance: {e}")
            finally:
                await asyncio.sleep(self.performance_dump_interval)

    async def dump_controller_performance(self):
        """Save current controller performance for all active bots to the database."""
        snapshot_timestamp = datetime.now(timezone.utc)
        saved_count = 0

        try:
            async with self.db_manager.get_session_context() as session:
                repo = ControllerPerformanceRepository(session)

                snapshots = []
                for bot_name in list(self.active_bots):
                    if self.is_bot_stopping(bot_name):
                        continue

                    controller_reports = self.mqtt_manager.get_bot_controller_reports(bot_name)
                    performance_data = self.determine_controller_performance(controller_reports)

                    for controller_id, data in performance_data.items():
                        snapshots.append({
                            "bot_name": bot_name,
                            "controller_id": controller_id,
                            "status": data.get("status", "unknown"),
                            "performance": data.get("performance", {}),
                            "custom_info": data.get("custom_info", {}),
                            "snapshot_timestamp": snapshot_timestamp,
                        })

                saved_rows = await repo.save_controller_performances(snapshots)
                saved_count = len(saved_rows)

            if saved_count > 0:
                logger.info(f"Dumped {saved_count} controller performance snapshots")
        except Exception as e:
            logger.error(f"Error saving controller performance to database: {e}")
            raise

    async def get_controller_performance_history(
        self,
        bot_name: Optional[str] = None,
        controller_id: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        interval: str = "5m"
    ):
        """Get historical controller performance with pagination and interval sampling."""
        try:
            async with self.db_manager.get_session_context() as session:
                repo = ControllerPerformanceRepository(session)
                return await repo.get_performance_history(
                    bot_name=bot_name,
                    controller_id=controller_id,
                    limit=limit,
                    cursor=cursor,
                    start_time=start_time,
                    end_time=end_time,
                    interval=interval
                )
        except Exception as e:
            logger.error(f"Error getting controller performance history: {e}")
            return [], None, False

    async def get_latest_controller_performance(
        self,
        bot_name: Optional[str] = None
    ) -> List[Dict]:
        """Get the most recent performance snapshot for each bot/controller."""
        try:
            async with self.db_manager.get_session_context() as session:
                repo = ControllerPerformanceRepository(session)
                return await repo.get_latest_performance(bot_name=bot_name)
        except Exception as e:
            logger.error(f"Error getting latest controller performance: {e}")
            return []

    # ============================================
    # Bot Run persistence
    # ============================================

    async def mark_bot_run_stopped(self, bot_name: str, final_status: Optional[Dict] = None):
        """Update a bot run status to STOPPED, capturing the final status snapshot."""
        async with self.db_manager.get_session_context() as session:
            bot_run_repo = BotRunRepository(session)
            await bot_run_repo.update_bot_run_stopped(bot_name, final_status=final_status)
            logger.info(f"Updated bot run status to STOPPED for {bot_name}")

    async def get_bot_runs(
        self,
        bot_name: Optional[str] = None,
        account_name: Optional[str] = None,
        strategy_type: Optional[str] = None,
        strategy_name: Optional[str] = None,
        run_status: Optional[str] = None,
        deployment_status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        include_final_status: bool = False,
    ) -> List[Dict]:
        """Get bot runs with optional filtering, serialized as dictionaries.

        ``final_status`` is omitted by default: the blob is ~99% of a run record's
        bytes (up to ~89 KB each) and is only meaningful on the detail endpoint.
        """
        async with self.db_manager.get_session_context() as session:
            bot_run_repo = BotRunRepository(session)
            bot_runs = await bot_run_repo.get_bot_runs(
                bot_name=bot_name,
                account_name=account_name,
                strategy_type=strategy_type,
                strategy_name=strategy_name,
                run_status=run_status,
                deployment_status=deployment_status,
                limit=limit,
                offset=offset,
            )
            return [
                self._serialize_bot_run(run, include_final_status=include_final_status)
                for run in bot_runs
            ]

    async def get_bot_run_stats(self) -> Dict[str, Any]:
        """Get statistics about bot runs."""
        async with self.db_manager.get_session_context() as session:
            bot_run_repo = BotRunRepository(session)
            return await bot_run_repo.get_bot_run_stats()

    async def get_bot_run_by_id(self, bot_run_id: int) -> Optional[Dict]:
        """Get a specific bot run by ID, serialized as a dictionary (None if not found)."""
        async with self.db_manager.get_session_context() as session:
            bot_run_repo = BotRunRepository(session)
            bot_run = await bot_run_repo.get_bot_run_by_id(bot_run_id)
            if not bot_run:
                return None
            return self._serialize_bot_run(bot_run)

    async def delete_bot_run(self, bot_run_id: int) -> Optional[Dict]:
        """Delete a bot run record and its archived folder.

        Returns a dict with ``bot_name`` and ``archived_folder_deleted`` keys,
        or None if the bot run does not exist.
        """
        async with self.db_manager.get_session_context() as session:
            bot_run_repo = BotRunRepository(session)
            bot_run = await bot_run_repo.delete_bot_run(bot_run_id)

            if not bot_run:
                return None

            # Also delete the archived bot folder if it exists
            archived_dir = os.path.join('bots', 'archived', bot_run.instance_name)
            archived_deleted = False
            if os.path.isdir(archived_dir):
                try:
                    import platform
                    import subprocess
                    if platform.system() == 'Darwin':
                        # Strip macOS ACLs (Docker adds "deny delete" ACLs)
                        subprocess.run(['chmod', '-R', '-N', archived_dir], check=False)
                    shutil.rmtree(archived_dir)
                    archived_deleted = True
                    logger.info(f"Deleted archived folder: {archived_dir}")
                except Exception as e:
                    logger.warning(f"Failed to delete archived folder {archived_dir}: {e}")

            return {
                "bot_name": bot_run.bot_name,
                "archived_folder_deleted": archived_deleted,
            }

    async def create_bot_run(self, **kwargs):
        """Create a bot run record. Errors are logged and swallowed so that a
        failed tracking write never fails the caller's deployment."""
        try:
            async with self.db_manager.get_session_context() as session:
                bot_run_repo = BotRunRepository(session)
                await bot_run_repo.create_bot_run(**kwargs)
                logger.info(f"Created bot run record for deployment {kwargs.get('instance_name')}")
        except Exception as e:
            logger.error(f"Failed to create bot run record: {e}")
            # Don't fail the deployment if bot run creation fails

    @staticmethod
    def _serialize_bot_run(run, include_final_status: bool = True) -> Dict:
        """Serialize a BotRun ORM object into a JSON-friendly dictionary.

        Set ``include_final_status=False`` to drop the (large) final status blob,
        e.g. for list responses.
        """
        serialized = {
            "id": run.id,
            "bot_name": run.bot_name,
            "instance_name": run.instance_name,
            "deployed_at": run.deployed_at.isoformat() if run.deployed_at else None,
            "stopped_at": run.stopped_at.isoformat() if run.stopped_at else None,
            "strategy_type": run.strategy_type,
            "strategy_name": run.strategy_name,
            "config_name": run.config_name,
            "account_name": run.account_name,
            "image_version": run.image_version,
            "deployment_status": run.deployment_status,
            "run_status": run.run_status,
            "deployment_config": run.deployment_config,
            "error_message": run.error_message,
        }
        if include_final_status:
            serialized["final_status"] = run.final_status
        return serialized

    # ============================================
    # Stop & Archive orchestration
    # ============================================

    async def stop_and_archive_bot(
        self,
        bot_name: str,
        skip_order_cancellation: bool,
        archive_locally: bool,
        s3_bucket: Optional[str],
        docker_manager: DockerService,
        bot_archiver: BotArchiver,
    ):
        """Stop a bot and archive its data (8-step workflow).

        This is the background-task body for ``stop-and-archive-bot``. It is
        FastAPI-agnostic and can be invoked/tested directly.
        """
        try:
            logger.info(f"Starting background stop-and-archive for {bot_name}")

            # Step 1: Capture bot final status before stopping (while bot is still running)
            logger.info(f"Capturing final status for {bot_name}")
            final_status = None
            try:
                final_status = self.get_bot_status(bot_name)
                logger.info(f"Captured final status for {bot_name}: {final_status}")
            except Exception as e:
                logger.warning(f"Failed to capture final status for {bot_name}: {e}")

            # Step 2: Update bot run with stopped_at timestamp and final status before stopping
            try:
                await self.mark_bot_run_stopped(bot_name, final_status=final_status)
                logger.info(f"Updated bot run with stopped_at timestamp and final status for {bot_name}")
            except Exception as e:
                logger.error(f"Failed to update bot run with stopped status: {e}")
                # Continue with stop process even if database update fails

            # Step 3: Mark the bot as stopping, and stop the bot trading process
            self.set_bot_stopping(bot_name)
            logger.info(f"Stopping bot trading process for {bot_name}")
            stop_response = await self.stop_bot(
                bot_name,
                skip_order_cancellation=skip_order_cancellation,
                async_backend=True  # Always use async for background tasks
            )

            if not stop_response or not stop_response.get("success", False):
                error_msg = stop_response.get('error', 'Unknown error') if stop_response else 'No response from bot orchestrator'
                logger.error(f"Failed to stop bot process: {error_msg}")
                return

            # Step 4: Wait for graceful shutdown (15 seconds as requested)
            logger.info(f"Waiting 15 seconds for bot {bot_name} to gracefully shutdown")
            await asyncio.sleep(15)

            # Step 5: Stop the container with monitoring
            max_retries = 10
            retry_interval = 2
            container_stopped = False

            for i in range(max_retries):
                logger.info(f"Attempting to stop container {bot_name} (attempt {i+1}/{max_retries})")
                docker_manager.stop_container(bot_name)

                # Check if container is already stopped
                container_status = docker_manager.get_container_status(bot_name)
                if container_status.get("state", {}).get("status") == "exited":
                    container_stopped = True
                    logger.info(f"Container {bot_name} is already stopped")
                    break

                await asyncio.sleep(retry_interval)

            if not container_stopped:
                logger.error(f"Failed to stop container {bot_name} after {max_retries} attempts")
                return

            # Step 6: Archive the bot data
            instance_dir = os.path.join('bots', 'instances', bot_name)
            logger.info(f"Archiving bot data from {instance_dir}")

            try:
                if archive_locally:
                    bot_archiver.archive_locally(bot_name, instance_dir)
                else:
                    bot_archiver.archive_and_upload(bot_name, instance_dir, bucket_name=s3_bucket)
                logger.info(f"Successfully archived bot data for {bot_name}")
            except Exception as e:
                logger.error(f"Archive failed: {str(e)}")
                # Continue with removal even if archive fails

            # Step 7: Remove the container
            logging.info(f"Removing container {bot_name}")
            remove_response = docker_manager.remove_container(bot_name, force=False)

            if not remove_response.get("success"):
                # If graceful remove fails, try force remove
                logging.warning("Graceful container removal failed, attempting force removal")
                remove_response = docker_manager.remove_container(bot_name, force=True)

            if remove_response.get("success"):
                logging.info(f"Successfully completed stop-and-archive for bot {bot_name}")

                # Step 8: Update bot run deployment status to ARCHIVED
                try:
                    async with self.db_manager.get_session_context() as session:
                        bot_run_repo = BotRunRepository(session)
                        await bot_run_repo.update_bot_run_archived(bot_name)
                        logger.info(f"Updated bot run deployment status to ARCHIVED for {bot_name}")
                except Exception as e:
                    logger.error(f"Failed to update bot run to archived: {e}")
            else:
                logging.error(f"Failed to remove container {bot_name}")

                # Update bot run with error status (but keep stopped_at timestamp from earlier)
                try:
                    async with self.db_manager.get_session_context() as session:
                        bot_run_repo = BotRunRepository(session)
                        await bot_run_repo.update_bot_run_stopped(
                            bot_name,
                            error_message="Failed to remove container during archive process"
                        )
                        logger.info(f"Updated bot run with error status for {bot_name}")
                except Exception as e:
                    logger.error(f"Failed to update bot run with error: {e}")

        except Exception as e:
            logging.error(f"Error in background stop-and-archive for {bot_name}: {str(e)}")

            # Update bot run with error status
            try:
                async with self.db_manager.get_session_context() as session:
                    bot_run_repo = BotRunRepository(session)
                    await bot_run_repo.update_bot_run_stopped(
                        bot_name,
                        error_message=str(e)
                    )
                    logger.info(f"Updated bot run with error status for {bot_name}")
            except Exception as db_error:
                logger.error(f"Failed to update bot run with error: {db_error}")
        finally:
            # Always clear the stopping status when the background task completes
            self.clear_bot_stopping(bot_name)
            logger.info(f"Cleared stopping status for bot {bot_name}")

            # Remove bot from active_bots and clear all MQTT data
            if bot_name in self.active_bots:
                self.mqtt_manager.clear_bot_data(bot_name)
                del self.active_bots[bot_name]
                logger.info(f"Removed bot {bot_name} from active_bots and cleared MQTT data")
