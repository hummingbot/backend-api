import docker
from hbotrc import BotCommands


class BotsManager:
    def __init__(self, broker_host, broker_port, broker_username, broker_password):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.broker_username = broker_username
        self.broker_password = broker_password
        self.docker_client = docker.from_env()
        self.active_bots = {}

    def get_active_containers(self):
        return [container.name for container in self.docker_client.containers.list() if container.status == 'running']

    def update_active_bots(self):
        active_containers = self.get_active_containers()
        active_hbot_containers = [container for container in active_containers if "hummingbot-" in container and "broker" not in container]

        # Remove bots that are no longer active
        for bot in list(self.active_bots):
            if bot not in active_hbot_containers:
                del self.active_bots[bot]

        # Add new bots or update existing ones
        for bot in active_hbot_containers:
            if bot not in self.active_bots:
                self.active_bots[bot] = {
                    "bot_name": bot,
                    "broker_client": BotCommands(host=self.broker_host, port=self.broker_port,
                                                 username=self.broker_username, password=self.broker_password,
                                                 bot_id=bot)
                }

    # Interact with a specific bot
    def start_bot(self, bot_name, **kwargs):
        if bot_name in self.active_bots:
            return self.active_bots[bot_name]["broker_client"].start(**kwargs)

    def stop_bot(self, bot_name, **kwargs):
        if bot_name in self.active_bots:
            return self.active_bots[bot_name]["broker_client"].stop(**kwargs)

    def import_strategy_for_bot(self, bot_name, strategy, **kwargs):
        if bot_name in self.active_bots:
            return self.active_bots[bot_name]["broker_client"].import_strategy(strategy, **kwargs)

    def configure_bot(self, bot_name, params, **kwargs):
        if bot_name in self.active_bots:
            return self.active_bots[bot_name]["broker_client"].config(params, **kwargs)

    def get_bot_status(self, bot_name, **kwargs):
        if bot_name in self.active_bots:
            return self.active_bots[bot_name]["broker_client"].status(**kwargs)

    def get_bot_history(self, bot_name, **kwargs):
        if bot_name in self.active_bots:
            return self.active_bots[bot_name]["broker_client"].history(**kwargs)
