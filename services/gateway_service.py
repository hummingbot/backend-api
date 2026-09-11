import logging
import os
import platform
import shutil
from typing import Any, Dict, Optional

import docker
from docker.errors import DockerException
from docker.types import LogConfig
from hummingbot.core.gateway.gateway_http_client import GatewayHttpClient

from config import settings
from models.gateway import GatewayConfig, GatewayStatus
from utils.gateway_certs import certs_present, ensure_gateway_certs, gateway_certs_dir

# Create module-specific logger
logger = logging.getLogger(__name__)


class GatewayService:
    """
    Service for managing the Hummingbot Gateway Docker container.
    Ensures only one Gateway instance can exist at a time.
    """

    GATEWAY_CONTAINER_NAME = "gateway"
    # Shared Gateway dir lives UNDER bots/ so it sits inside the single host<->container bind
    # mount (./bots:/hummingbot-api/bots). Anything written outside that mount by a containerized
    # API lands on the container's ephemeral FS and never reaches the Gateway container.
    GATEWAY_SUBPATH = os.path.join("bots", "gateway-files")
    # Path inside the Gateway container where it reads the mTLS cert set.
    GATEWAY_CERTS_BIND = "/home/gateway/certs"

    def __init__(self):
        self.SOURCE_PATH = os.getcwd()
        # Use BOTS_PATH if set (for Docker), otherwise use SOURCE_PATH (for local)
        self.BOTS_PATH = os.environ.get('BOTS_PATH', self.SOURCE_PATH)
        self._client = None

    @property
    def client(self):
        # Defer Docker discovery until a Gateway operation is requested. Aomi
        # and CEX executors can run while Docker/Gateway is unavailable.
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def _gateway_base(self, host: bool = False) -> str:
        """Gateway-files base. host=False is the path the API process reads/writes
        (container-local); host=True is the host path used as a Docker bind-mount source."""
        base = self.BOTS_PATH if host else self.SOURCE_PATH
        return os.path.join(base, self.GATEWAY_SUBPATH)

    def _ensure_gateway_directories(self):
        """Create necessary directories for Gateway if they don't exist.

        Directories are created on the path the API process can actually write (container-local,
        inside the mounted bots/ dir); the matching host paths are used for the bind mounts.
        """
        gateway_base = self._gateway_base(host=False)

        conf_dir = os.path.join(gateway_base, "conf")
        logs_dir = os.path.join(gateway_base, "logs")
        certs_dir = os.path.join(gateway_base, "certs")

        os.makedirs(conf_dir, exist_ok=True)
        os.makedirs(logs_dir, exist_ok=True)
        os.makedirs(certs_dir, exist_ok=True)

        return {
            "base": gateway_base,
            "conf": conf_dir,
            "logs": logs_dir,
            "certs": certs_dir,
        }

    def _get_gateway_container(self) -> Optional[docker.models.containers.Container]:
        """Get the Gateway container if it exists"""
        try:
            return self.client.containers.get(self.GATEWAY_CONTAINER_NAME)
        except docker.errors.NotFound:
            return None
        except DockerException as e:
            logger.error(f"Error getting Gateway container: {e}")
            return None

    def is_running(self) -> bool:
        """True when the Gateway container exists and is running.

        This is the direct signal for "is there a Gateway to talk to". Callers must not
        infer it from cert presence: certs are generated up front so the mTLS client is
        always usable, so their existence says nothing about whether the Gateway is up.
        """
        container = self._get_gateway_container()
        return container is not None and container.status == "running"

    @staticmethod
    def _normalize_mount_source(source: str) -> str:
        """Normalize a Docker-reported bind-mount source to a comparable host path.

        Docker Desktop reports macOS/Windows host paths through a ``/host_mnt`` prefix
        (host ``/Users/x`` surfaces as ``/host_mnt/Users/x``), so a raw string compare
        against a configured host path would never match there.
        """
        normalized = os.path.normpath(source or "")
        for prefix in ("/host_mnt", "/run/desktop/mnt/host"):
            if normalized.startswith(prefix + os.sep):
                normalized = normalized[len(prefix):]
        return normalized

    def _mounts_shared_certs(self, container) -> bool:
        """True when the container mounts *this* API's shared cert set.

        The Gateway reads its server cert from the directory we bind-mount at
        ``GATEWAY_CERTS_BIND``. If that mount's source is our cert dir, the Gateway is
        serving a cert signed by the CA our clients trust - consistent by construction.
        A Gateway started by a different API instance (or a different checkout) mounts a
        different source path, which is exactly the mismatch we need to detect.
        """
        expected = self._normalize_mount_source(gateway_certs_dir(host=True))
        for mount in container.attrs.get("Mounts", []):
            if mount.get("Destination") != self.GATEWAY_CERTS_BIND:
                continue
            return self._normalize_mount_source(mount.get("Source", "")) == expected
        return False

    def _get_self_container(self) -> Optional[docker.models.containers.Container]:
        """Best-effort lookup of the container this API process is running in.

        Used to attach the Gateway to the same Docker network(s) the API is on, so the
        Gateway is reachable by container name (https://gateway:15888) regardless of the
        Compose project prefix (e.g. a workspace-scoped `185_emqx-bridge` network).
        """
        candidates = []
        # Docker sets HOSTNAME to the short container id by default.
        hostname = os.environ.get("HOSTNAME")
        if hostname:
            candidates.append(hostname)
        # Fallback: parse the container id from cgroup/mountinfo in case HOSTNAME was overridden.
        for proc_file in ("/proc/self/mountinfo", "/proc/self/cgroup"):
            try:
                with open(proc_file) as f:
                    content = f.read()
            except OSError:
                continue
            marker = "/docker/containers/"
            idx = content.find(marker)
            if idx != -1:
                rest = content[idx + len(marker):]
                cid = rest.split("/", 1)[0].strip()
                if cid:
                    candidates.append(cid)

        for ident in candidates:
            try:
                return self.client.containers.get(ident)
            except (docker.errors.NotFound, DockerException):
                continue
        return None

    def _get_api_network_names(self) -> list:
        """User-defined Docker networks the API container is attached to.

        Excludes the special `host`/`none`/default `bridge` networks since those don't
        provide container-name DNS resolution for the Gateway.
        """
        container = self._get_self_container()
        if container is None:
            return []
        networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        return [name for name in networks if name not in ("host", "none", "bridge")]

    def get_status(self) -> GatewayStatus:
        """Get the current status of the Gateway container"""
        container = self._get_gateway_container()

        if container is None:
            return GatewayStatus(
                running=False,
                container_id=None,
                image=None,
                created_at=None,
                port=None
            )

        # Extract port from container configuration
        port = None
        if container.status == "running":
            # Check if using host networking
            network_mode = container.attrs.get("HostConfig", {}).get("NetworkMode", "")
            if network_mode == "host":
                # Host networking: Gateway uses port 15888 directly
                port = 15888
            else:
                # Bridge networking: Extract from port mappings
                ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})
                if "15888/tcp" in ports and ports["15888/tcp"]:
                    port = int(ports["15888/tcp"][0]["HostPort"])

        return GatewayStatus(
            running=container.status == "running",
            container_id=container.id,
            image=container.image.tags[0] if container.image.tags else container.image.id[:12],
            created_at=container.attrs.get("Created"),
            port=port
        )

    def start(self, config: GatewayConfig) -> Dict[str, Any]:
        """
        Start the Gateway container.
        If a container already exists, it will be stopped and removed before creating a new one.
        """
        # Check if Gateway is already running
        existing_container = self._get_gateway_container()
        if existing_container:
            if existing_container.status == "running":
                return {
                    "success": False,
                    "message": "Gateway is already running. Use stop first or restart to update configuration."
                }
            else:
                # Remove stopped container
                logger.info("Removing stopped Gateway container")
                existing_container.remove(force=True)

        # Ensure directories exist
        dirs = self._ensure_gateway_directories()

        # SEC-048: the API only ever runs the Gateway secured (TLS + mTLS). There is no dev-mode
        # escape hatch — a Gateway holding wallet keys must never be served over plain HTTP.
        # A single secret (CONFIG_PASSWORD) secures the Gateway, this API, and deployed instances:
        # the Gateway uses GATEWAY_PASSPHRASE for both TLS and wallet encryption, and the shared
        # mTLS certs must be decryptable by this API's clients (which use CONFIG_PASSWORD), so the
        # passphrase is always CONFIG_PASSWORD.
        passphrase = settings.security.config_password

        # Set up volumes - bind-mount SOURCES must be HOST paths (the Docker daemon runs on the
        # host), so use the host-side base even though the API process wrote via the local base.
        host_base = self._gateway_base(host=True)
        volumes = {
            os.path.join(host_base, "conf"): {'bind': '/home/gateway/conf', 'mode': 'rw'},
            os.path.join(host_base, "logs"): {'bind': '/home/gateway/logs', 'mode': 'rw'},
        }

        # Run the Gateway with TLS + client-cert auth (DEV=false). Generate the shared mTLS cert
        # set once (idempotent; existing CA reused) into the local-base dir, and mount the matching
        # host path read-only so the Gateway decrypts its server key with the same passphrase.
        ensure_gateway_certs(passphrase, dirs["certs"])
        volumes[os.path.join(host_base, "certs")] = {'bind': self.GATEWAY_CERTS_BIND, 'mode': 'ro'}
        environment = {
            "GATEWAY_PASSPHRASE": passphrase,
            "DEV": "false",
        }
        logger.info("Starting Gateway in secured mode (TLS + mTLS, DEV=false)")

        # Configure logging
        log_config = LogConfig(
            type="json-file",
            config={
                'max-size': '10m',
                'max-file': "5",
            }
        )

        # Detect platform and configure networking
        # Native Linux: Use host networking (works natively)
        # Docker Desktop (macOS/Windows) or containerized: Use bridge networking
        system_platform = platform.system()

        # Check if running inside Docker container (Docker Desktop or containerized API)
        in_container = os.path.exists('/.dockerenv') or os.path.exists('/run/.containerenv')

        # Only use host networking on native Linux (not inside a container)
        use_host_network = system_platform == "Linux" and not in_container

        if use_host_network:
            logger.info("Detected native Linux - using host network mode for Gateway")
        else:
            logger.info(f"Detected {system_platform} (in_container={in_container}) - using bridge networking for Gateway")

        try:
            # Build container configuration
            container_config = {
                "image": config.image,
                "name": self.GATEWAY_CONTAINER_NAME,
                "volumes": volumes,
                "environment": environment,
                "detach": True,
                "restart_policy": {"Name": "always"},
                "log_config": log_config,
            }

            if use_host_network:
                # Linux: Use host networking
                container_config["network_mode"] = "host"
            else:
                # macOS/Windows: Use bridge networking with port mapping.
                # SEC-048: bind the host publish to loopback so the socket is never offered on
                # the public interface (container-to-container traffic still flows over the
                # emqx-bridge network by container name). Host networking on Linux can't be
                # loopback-scoped here; mTLS is the control there.
                container_config["ports"] = {'15888/tcp': ('127.0.0.1', config.port)}

            container = self.client.containers.run(**container_config)

            # On macOS/Windows, attach the Gateway to the same Docker network(s) the API
            # container is on so it's reachable by container name (https://gateway:15888).
            # Detecting the API's own networks handles Compose project prefixes (e.g. a
            # workspace-scoped `185_emqx-bridge`) that fixed names would miss. Fall back to
            # the legacy fixed names when self-detection isn't possible (e.g. API run on host).
            if not use_host_network:
                target_networks = self._get_api_network_names()
                if not target_networks:
                    target_networks = ["hummingbot-api_emqx-bridge", "emqx-bridge"]
                connected = False
                for net in target_networks:
                    try:
                        network = self.client.networks.get(net)
                        network.connect(container)
                        logger.info(f"Connected Gateway to {net} network")
                        connected = True
                    except docker.errors.NotFound:
                        continue
                    except DockerException as e:
                        logger.warning(f"Failed to connect Gateway to {net} network: {e}")
                if not connected:
                    logger.warning(
                        "Gateway not attached to any shared Docker network; the API may not "
                        "be able to reach it by container name"
                    )

            logger.info(f"Gateway container started successfully: {container.id}")

            # On a fresh install the app lifespan defers the Gateway status monitor
            # because the mTLS certs didn't exist yet; they were just generated above,
            # so start it now (no-op when it is already running).
            GatewayHttpClient.get_instance().start_monitor()

            return {
                "success": True,
                "message": "Gateway started successfully",
                "container_id": container.id,
                "port": config.port
            }

        except DockerException as e:
            logger.error(f"Failed to start Gateway container: {e}")
            return {
                "success": False,
                "message": f"Failed to start Gateway: {str(e)}"
            }

    def reconcile_certs(self) -> Dict[str, Any]:
        """Make a running Gateway usable by this API's mTLS client.

        "Was the Gateway started with this API?" reduces to: does the running container mount
        *our* cert dir as its server cert source? Cert presence cannot answer this — the set is
        generated up front at API startup, so it is present even for a Gateway this API never
        started. A Gateway serving a cert from some other source would fail our mTLS handshake
        on every request.

        Decision matrix:
        - container not running          -> nothing (start the Gateway)
        - running + mounts our certs     -> nothing (consistent by construction)
        - running + mounts other/no certs-> regenerate the cert set and restart the Gateway so it
                                            loads the server cert that matches our client cert

        The restart is required: the Gateway reads its server cert/key only at startup, so writing
        new certs to the shared volume has no effect on an already-running container.
        """
        container = self._get_gateway_container()
        if container is None or container.status != "running":
            return {
                "success": True,
                "action": "none",
                "message": "Gateway container not running; start it to generate certs",
            }

        if self._mounts_shared_certs(container) and certs_present(gateway_certs_dir()):
            return {
                "success": True,
                "action": "none",
                "message": "Gateway running against this API's shared mTLS cert set",
            }

        logger.warning(
            "Gateway container is running but is not serving this API's shared mTLS cert set; "
            "regenerating the cert set and restarting the Gateway so it loads a matching server cert"
        )
        dirs = self._ensure_gateway_directories()
        ensure_gateway_certs(settings.security.config_password, dirs["certs"])
        result = self.restart()
        result["action"] = "regenerated_and_restarted"
        return result

    def stop(self) -> Dict[str, Any]:
        """Stop the Gateway container"""
        container = self._get_gateway_container()

        if container is None:
            return {
                "success": False,
                "message": "Gateway container not found"
            }

        try:
            if container.status == "running":
                container.stop()
                logger.info("Gateway container stopped")
            return {
                "success": True,
                "message": "Gateway stopped successfully"
            }
        except DockerException as e:
            logger.error(f"Failed to stop Gateway container: {e}")
            return {
                "success": False,
                "message": f"Failed to stop Gateway: {str(e)}"
            }

    def restart(self, config: Optional[GatewayConfig] = None) -> Dict[str, Any]:
        """
        Restart the Gateway container.
        If config is provided, the container will be recreated with the new configuration.
        """
        container = self._get_gateway_container()

        if container is None:
            if config:
                # No existing container, just start with new config
                return self.start(config)
            else:
                return {
                    "success": False,
                    "message": "Gateway container not found. Use start with configuration to create one."
                }

        if config:
            # Stop and remove existing container, then start with new config
            try:
                container.remove(force=True)
                logger.info("Removed existing Gateway container for restart with new config")
            except DockerException as e:
                logger.error(f"Failed to remove Gateway container: {e}")
                return {
                    "success": False,
                    "message": f"Failed to remove existing container: {str(e)}"
                }
            return self.start(config)
        else:
            # Simple restart of existing container
            try:
                container.restart()
                logger.info("Gateway container restarted")
                return {
                    "success": True,
                    "message": "Gateway restarted successfully"
                }
            except DockerException as e:
                logger.error(f"Failed to restart Gateway container: {e}")
                return {
                    "success": False,
                    "message": f"Failed to restart Gateway: {str(e)}"
                }

    def remove(self, remove_data: bool = False) -> Dict[str, Any]:
        """
        Remove the Gateway container and optionally its data.

        Args:
            remove_data: If True, also remove the gateway-files directory
        """
        container = self._get_gateway_container()

        if container is None:
            if remove_data:
                # No container, but try to remove data if requested
                gateway_dir = self._gateway_base(host=False)
                if os.path.exists(gateway_dir):
                    try:
                        shutil.rmtree(gateway_dir)
                        logger.info(f"Removed Gateway data directory: {gateway_dir}")
                        return {
                            "success": True,
                            "message": "Gateway data removed (no container was found)"
                        }
                    except Exception as e:
                        logger.error(f"Failed to remove Gateway data: {e}")
                        return {
                            "success": False,
                            "message": f"Failed to remove Gateway data: {str(e)}"
                        }
            return {
                "success": False,
                "message": "Gateway container not found"
            }

        try:
            # Remove container
            container.remove(force=True)
            logger.info("Gateway container removed")

            # Remove data if requested
            if remove_data:
                gateway_dir = self._gateway_base(host=False)
                if os.path.exists(gateway_dir):
                    shutil.rmtree(gateway_dir)
                    logger.info(f"Removed Gateway data directory: {gateway_dir}")
                    return {
                        "success": True,
                        "message": "Gateway container and data removed successfully"
                    }

            return {
                "success": True,
                "message": "Gateway container removed successfully"
            }

        except DockerException as e:
            logger.error(f"Failed to remove Gateway container: {e}")
            return {
                "success": False,
                "message": f"Failed to remove Gateway: {str(e)}"
            }
        except Exception as e:
            logger.error(f"Failed to remove Gateway data: {e}")
            return {
                "success": False,
                "message": f"Gateway container removed but failed to remove data: {str(e)}"
            }

    def get_logs(self, tail: int = 100) -> Dict[str, Any]:
        """Get logs from the Gateway container"""
        container = self._get_gateway_container()

        if container is None:
            return {
                "success": False,
                "message": "Gateway container not found"
            }

        try:
            logs = container.logs(tail=tail, timestamps=True).decode('utf-8')
            return {
                "success": True,
                "logs": logs
            }
        except DockerException as e:
            logger.error(f"Failed to get Gateway logs: {e}")
            return {
                "success": False,
                "message": f"Failed to get logs: {str(e)}"
            }
