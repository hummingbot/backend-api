import logging
import os

import docker
from docker.errors import DockerException

from models import HummingbotInstanceConfig


class DockerManager:
    def __init__(self):
        try:
            self.client = docker.from_env()
        except DockerException as e:
            logging.error(f"It was not possible to connect to Docker. Please make sure Docker is running. Error: {e}")

    def get_active_containers(self):
        try:
            containers_info = [{"id": container.id, "name": container.name, "status": container.status} for
                               container in self.client.containers.list(filters={"status": "running"}) if
                               "hummingbot" in container.name]
            return {"active_instances": containers_info}
        except DockerException as e:
            return str(e)

    def get_exited_containers(self):
        try:
            containers_info = [{"id": container.id, "name": container.name, "status": container.status} for
                               container in self.client.containers.list(filters={"status": "exited"}) if
                               "hummingbot" in container.name]
            return {"exited_instances": containers_info}
        except DockerException as e:
            return str(e)

    def clean_exited_containers(self):
        try:
            self.client.containers.prune()
        except DockerException as e:
            return str(e)

    def is_docker_running(self):
        try:
            self.client.ping()
            return True
        except DockerException:
            return False

    def stop_container(self, container_name):
        try:
            container = self.client.containers.get(container_name)
            container.stop()
        except DockerException as e:
            return str(e)

    def start_container(self, container_name):
        try:
            container = self.client.containers.get(container_name)
            container.start()
        except DockerException as e:
            return str(e)

    def remove_container(self, container_name, force=True):
        try:
            container = self.client.containers.get(container_name)
            container.remove(force=force)
            return {"success": True, "message": f"Container {container_name} removed successfully."}
        except DockerException as e:
            return {"success": False, "message": str(e)}

    def create_hummingbot_instance(self, config: HummingbotInstanceConfig):
        bots_dir = "bots"  # Root directory for all bot-related files

        instance_dir = os.path.join(bots_dir, 'instances', config.instance_name)
        if not os.path.exists(instance_dir):
            os.makedirs(instance_dir)
            os.makedirs(os.path.join(instance_dir, 'data'))
            os.makedirs(os.path.join(instance_dir, 'logs'))

        # Set up Docker volumes
        volumes = {
            os.path.abspath(os.path.join(bots_dir, 'credentials', config.credentials_profile)): {'bind': '/home/hummingbot/conf', 'mode': 'ro'},
            os.path.abspath(os.path.join(bots_dir, 'credentials', config.credentials_profile, 'connectors')): {'bind': '/home/hummingbot/conf/connectors', 'mode': 'ro'},
            os.path.abspath(os.path.join(instance_dir, 'data')): {'bind': '/home/hummingbot/data', 'mode': 'rw'},
            os.path.abspath(os.path.join(instance_dir, 'logs')): {'bind': '/home/hummingbot/logs', 'mode': 'rw'},
            os.path.abspath(os.path.join(bots_dir, 'scripts')): {'bind': '/home/hummingbot/scripts', 'mode': 'rw'},
            os.path.abspath(os.path.join(bots_dir, 'controllers')): {'bind': '/home/hummingbot/smart_components/controllers', 'mode': 'rw'},
        }

        # Set up environment variables
        environment = {}
        password = os.environ.get('CONFIG_PASSWORD', None)
        if config.autostart_script:
            if password:
                environment["CONFIG_PASSWORD"] = password
                environment['CONFIG_FILE_NAME'] = config.autostart_script
            else:
                return {"success": False, "message": "Password not provided. We cannot start the bot without a password."}

        try:
            self.client.containers.run(
                config.image,
                name=config.instance_name,
                volumes=volumes,
                environment=environment,
                network_mode="host",
                detach=True,
                tty=True,
                stdin_open=True,
            )
            return {"success": True, "message": f"Instance {config.instance_name} created successfully."}
        except docker.errors.DockerException as e:
            return {"success": False, "message": str(e)}
