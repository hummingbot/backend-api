import logging
import os
import shutil

import docker
from docker.errors import DockerException
from docker.types import LogConfig

from models import HummingbotInstanceConfig
from utils.file_system import FileSystemUtil

file_system = FileSystemUtil()


class DockerManager:
    def __init__(self):
        self.SOURCE_PATH = os.getcwd()
        try:
            self.client = docker.from_env()
        except DockerException as e:
            logging.error(f"It was not possible to connect to Docker. Please make sure Docker is running. Error: {e}")

    def get_active_containers(self):
        try:
            containers_info = [{"id": container.id, "name": container.name, "status": container.status} for
                               container in self.client.containers.list(filters={"status": "running"}) if
                               "hummingbot" in container.name and "broker" not in container.name]
            return {"active_instances": containers_info}
        except DockerException as e:
            return str(e)

    def get_available_images(self):
        try:
            images = self.client.images.list()
            return {"images": images}
        except DockerException as e:
            return str(e)

    def pull_image(self, image_name):
        try:
            self.client.images.pull(image_name)
        except DockerException as e:
            return str(e)

    def get_exited_containers(self):
        try:
            containers_info = [{"id": container.id, "name": container.name, "status": container.status} for
                               container in self.client.containers.list(filters={"status": "exited"}) if
                               "hummingbot" in container.name and "broker" not in container.name]
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
        bots_path = os.environ.get('BOTS_PATH', self.SOURCE_PATH)  # Default to 'SOURCE_PATH' if BOTS_PATH is not set
        instance_name = f"hummingbot-{config.instance_name}"
        instance_dir = os.path.join("bots", 'instances', instance_name)
        if not os.path.exists(instance_dir):
            os.makedirs(instance_dir)
            os.makedirs(os.path.join(instance_dir, 'data'))
            os.makedirs(os.path.join(instance_dir, 'logs'))

        # Copy credentials to instance directory
        source_credentials_dir = os.path.join("bots", 'credentials', config.credentials_profile)
        script_config_dir = os.path.join("bots", 'conf', 'scripts')
        controllers_config_dir = os.path.join("bots", 'conf', 'controllers')
        destination_credentials_dir = os.path.join(instance_dir, 'conf')
        destination_scripts_config_dir = os.path.join(instance_dir, 'conf', 'scripts')
        destination_controllers_config_dir = os.path.join(instance_dir, 'conf', 'controllers')

        # Remove the destination directory if it already exists
        if os.path.exists(destination_credentials_dir):
            shutil.rmtree(destination_credentials_dir)

        # Copy the entire contents of source_credentials_dir to destination_credentials_dir     
        shutil.copytree(source_credentials_dir, destination_credentials_dir)
        shutil.copytree(script_config_dir, destination_scripts_config_dir)
        shutil.copytree(controllers_config_dir, destination_controllers_config_dir)
        conf_file_path = f"{instance_dir}/conf/conf_client.yml"
        client_config = FileSystemUtil.read_yaml_file(conf_file_path)
        client_config['instance_id'] = instance_name
        FileSystemUtil.dump_dict_to_yaml(conf_file_path, client_config)

        # Set up Docker volumes
        volumes = {
            os.path.abspath(os.path.join(bots_path, instance_dir, 'conf')): {'bind': '/home/hummingbot/conf', 'mode': 'rw'},
            os.path.abspath(os.path.join(bots_path, instance_dir, 'conf', 'connectors')): {'bind': '/home/hummingbot/conf/connectors', 'mode': 'rw'},
            os.path.abspath(os.path.join(bots_path, instance_dir, 'conf', 'scripts')): {'bind': '/home/hummingbot/conf/scripts', 'mode': 'rw'},
            os.path.abspath(os.path.join(bots_path, instance_dir, 'conf', 'controllers')): {'bind': '/home/hummingbot/conf/controllers', 'mode': 'rw'},
            os.path.abspath(os.path.join(bots_path, instance_dir, 'data')): {'bind': '/home/hummingbot/data', 'mode': 'rw'},
            os.path.abspath(os.path.join(bots_path, instance_dir, 'logs')): {'bind': '/home/hummingbot/logs', 'mode': 'rw'},
            os.path.abspath(os.path.join(bots_path, "bots", 'scripts')): {'bind': '/home/hummingbot/scripts', 'mode': 'rw'},
            os.path.abspath(os.path.join(bots_path, "bots", 'controllers')): {'bind': '/home/hummingbot/controllers', 'mode': 'rw'},
        }

        # Set up environment variables
        environment = {}
        password = os.environ.get('CONFIG_PASSWORD', "a")
        if password:
            environment["CONFIG_PASSWORD"] = password

        if config.script:
            if password:
                environment['CONFIG_FILE_NAME'] = config.script
                if config.script_config:
                    environment['SCRIPT_CONFIG'] = config.script_config
            else:
                return {"success": False, "message": "Password not provided. We cannot start the bot without a password."}

        log_config = LogConfig(
            type="json-file",
            config={
                'max-size': '10m',
                'max-file': "5",
            })
        try:
            self.client.containers.run(
                image=config.image,
                name=instance_name,
                volumes=volumes,
                environment=environment,
                network_mode="host",
                detach=True,
                tty=True,
                stdin_open=True,
                log_config=log_config,
            )
            return {"success": True, "message": f"Instance {instance_name} created successfully."}
        except docker.errors.DockerException as e:
            return {"success": False, "message": str(e)}
