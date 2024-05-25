import docker
from hbotrc import BotCommands
from hbotrc.msgs import StatusCommandMessage
from hbotrc.listener import BotListener
from hbotrc.spec import TopicSpecs


class HummingbotPerformanceListener(BotListener):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        topic_prefix = TopicSpecs.PREFIX.format(
            namespace=self._ns,
            instance_id=self._bot_id
        )
        self._performance_topic = f'{topic_prefix}/performance'
        self._bot_performance = {}
        self.performance_report_sub = None

    def get_bot_performance(self):
        return self._bot_performance

    def _init_endpoints(self):
        super().__init__()
        self.performance_report_sub = self.create_psubscriber(topic=self._performance_topic,
                                                              on_message=self._update_bot_performance)

    def _update_bot_performance(self, msg, topic):
        for controller_id, performance_report in msg.items():
            self._bot_performance[controller_id] = performance_report


class BotsManager:
    def __init__(self, broker_host, broker_port, broker_username, broker_password):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.broker_username = broker_username
        self.broker_password = broker_password
        self.docker_client = docker.from_env()
        self.active_bots = {}

    @staticmethod
    def hummingbot_containers_fiter(container):
        try:
            return "hummingbot" in container.name and "broker" not in container.name
        except Exception:
            return False

    def get_active_containers(self):
        return [container.name for container in self.docker_client.containers.list()
                if container.status == 'running' and self.hummingbot_containers_fiter(container)]

    def update_active_bots(self):
        active_hbot_containers = self.get_active_containers()

        # Remove bots that are no longer active
        for bot in list(self.active_bots):
            if bot not in active_hbot_containers:
                del self.active_bots[bot]

        # Add new bots or update existing ones
        for bot in active_hbot_containers:
            if bot not in self.active_bots:
                hbot_listener = HummingbotPerformanceListener(host=self.broker_host, port=self.broker_port,
                                                              username=self.broker_username,
                                                              password=self.broker_password,
                                                              bot_id=bot)
                hbot_listener.start()
                self.active_bots[bot] = {
                    "bot_name": bot,
                    "broker_client": BotCommands(host=self.broker_host, port=self.broker_port,
                                                 username=self.broker_username, password=self.broker_password,
                                                 bot_id=bot),
                    "broker_listener": hbot_listener,
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
            return self.active_bots[bot_name]["broker_listener"].get_bot_performance()

    def get_bot_history(self, bot_name, **kwargs):
        if bot_name in self.active_bots:
            return self.active_bots[bot_name]["broker_client"].history(**kwargs)

    @staticmethod
    def determine_running_status(controller_data):
        if "Market connectors are not ready" in controller_data:
            return "starting"  # Bot is starting but not ready
        elif "No strategy is currently running" in controller_data:
            return "stopped"  # The bot is stopped
        return "running"  # Default to running if none of the above

    def get_all_bots_status(self):
        all_bots_status = {}
        for bot, bot_info in self.active_bots.items():
            all_bots_status[bot] = bot_info["broker_listener"].get_bot_performance()
        return all_bots_status
