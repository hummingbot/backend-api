import docker
from docker.errors import DockerException


class DockerManager:
    def __init__(self):
        self.client = docker.from_env()

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
