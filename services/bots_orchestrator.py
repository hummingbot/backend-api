import asyncio
from collections import deque
from typing import Optional

import docker
from hbotrc import BotCommands
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
        self._bot_error_logs = deque(maxlen=100)
        self._bot_general_logs = deque(maxlen=100)
        self.performance_report_sub = None

    def get_bot_performance(self):
        return self._bot_performance

    def get_bot_error_logs(self):
        return list(self._bot_error_logs)

    def get_bot_general_logs(self):
        return list(self._bot_general_logs)

    def _init_endpoints(self):
        super()._init_endpoints()
        self.performance_report_sub = self.create_subscriber(topic=self._performance_topic,
                                                             on_message=self._update_bot_performance)

    def _update_bot_performance(self, msg):
        for controller_id, performance_report in msg.items():
            self._bot_performance[controller_id] = performance_report

    def _on_log(self, log):
        if log.level_name == "ERROR":
            self._bot_error_logs.append(log)
        else:
            self._bot_general_logs.append(log)

    def stop(self):
        super().stop()
        self._bot_performance = {}


class BotsManager:
    def __init__(self, broker_host, broker_port, broker_username, broker_password):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.broker_username = broker_username
        self.broker_password = broker_password
        self.docker_client = docker.from_env()
        self.active_bots = {}
        self._update_bots_task: Optional[asyncio.Task] = None

    @staticmethod
    def hummingbot_containers_fiter(container):
        try:
            return "hummingbot" in container.name and "broker" not in container.name
        except Exception:
            return False

    def get_active_containers(self):
        return [container.name for container in self.docker_client.containers.list()
                if container.status == 'running' and self.hummingbot_containers_fiter(container)]

    def start_update_active_bots_loop(self):
        self._update_bots_task = asyncio.create_task(self.update_active_bots())

    def stop_update_active_bots_loop(self):
        if self._update_bots_task:
            self._update_bots_task.cancel()
        self._update_bots_task = None

    async def update_active_bots(self, sleep_time=1):
        while True:
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
            await asyncio.sleep(sleep_time)

    # Interact with a specific bot
    def start_bot(self, bot_name, **kwargs):
        if bot_name in self.active_bots:
            self.active_bots[bot_name]["broker_listener"].start()
            return self.active_bots[bot_name]["broker_client"].start(**kwargs)

    def stop_bot(self, bot_name, **kwargs):
        if bot_name in self.active_bots:
            self.active_bots[bot_name]["broker_listener"].stop()
            return self.active_bots[bot_name]["broker_client"].stop(**kwargs)

    def import_strategy_for_bot(self, bot_name, strategy, **kwargs):
        if bot_name in self.active_bots:
            return self.active_bots[bot_name]["broker_client"].import_strategy(strategy, **kwargs)

    def configure_bot(self, bot_name, params, **kwargs):
        if bot_name in self.active_bots:
            return self.active_bots[bot_name]["broker_client"].config(params, **kwargs)

    def get_bot_history(self, bot_name, **kwargs):
        if bot_name in self.active_bots:
            return self.active_bots[bot_name]["broker_client"].history(**kwargs)

    @staticmethod
    def determine_controller_performance(controllers_performance):
        cleaned_performance = {}
        for controller, performance in controllers_performance.items():
            try:
                # Check if all the metrics are numeric
                _ = sum(metric for key, metric in performance.items() if key != "close_type_counts")
                cleaned_performance[controller] = {
                    "status": "running",
                    "performance": performance
                }
            except Exception as e:
                cleaned_performance[controller] = {
                    "status": "error",
                    "error": f"Some metrics are not numeric, check logs and restart controller: {e}",
                }
        return cleaned_performance

    def get_all_bots_status(self):
        all_bots_status = {}
        for bot in self.active_bots:
            all_bots_status[bot] = self.get_bot_status(bot)
        return all_bots_status

    def get_bot_status(self, bot_name):
        if bot_name in self.active_bots:
            try:
                broker_listener = self.active_bots[bot_name]["broker_listener"]
                controllers_performance = broker_listener.get_bot_performance()
                performance = self.determine_controller_performance(controllers_performance)
                error_logs = broker_listener.get_bot_error_logs()
                general_logs = broker_listener.get_bot_general_logs()
                status = "running" if len(performance) > 0 else "stopped"
                return {
                    "status": status,
                    "performance": performance,
                    "error_logs": error_logs,
                    "general_logs": general_logs
                }
            except Exception as e:
                return {
                    "status": "error",
                    "error": str(e)
                }
