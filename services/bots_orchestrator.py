import re
from collections import defaultdict

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
        self._performance_topic = f'{topic_prefix}{TopicSpecs.LOGS}'

    def _init_endpoints(self):
        super().__init__()
        self.performance_report_sub = self.create_subscriber(topic=self._performance_topic,
                                                             on_message=self._update_bot_performance)

    def _update_bot_performance(self, msg):
        pass


class BotsManager:
    def __init__(self, broker_host, broker_port, broker_username, broker_password):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.broker_username = broker_username
        self.broker_password = broker_password
        self.docker_client = docker.from_env()
        # self.performance_listener = ETopicListenerFactory()
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
                self.active_bots[bot] = {
                    "bot_name": bot,
                    "broker_client": BotCommands(host=self.broker_host, port=self.broker_port,
                                                 username=self.broker_username, password=self.broker_password,
                                                 bot_id=bot)
                }
        for bot, data in self.active_bots.items():
            data["status"] = self.get_bot_status(bot)

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
            status: StatusCommandMessage = self.active_bots[bot_name]["broker_client"].status(**kwargs)
            return self.parse_status_message(status.msg)

    def get_bot_history(self, bot_name, **kwargs):
        if bot_name in self.active_bots:
            return self.active_bots[bot_name]["broker_client"].history(**kwargs)

    def parse_status_message(self, msg):
        data = {}

        # Extracting total balances
        balance_pattern = r"Balances:\s*\n([\s\S]+?)\n\n"
        balances = re.search(balance_pattern, msg)
        balances_dict = {}
        if balances:
            balance_lines = balances.group(1).strip().split('\n')
            for line in balance_lines:
                parts = re.split(r'\s{2,}', line.strip())
                if len(parts) >= 4:
                    exchange, asset, total, available = parts[0], parts[1], parts[2], parts[3]
                    if exchange not in balances_dict:
                        balances_dict[exchange] = {}
                    balances_dict[exchange][asset] = available
        data['total_balances'] = balances_dict

        # Extract controller specific data and assess running status
        controller_data_dict = {}
        controllers = re.finditer(
            r"Controller:\s*([^\n]+)\s*\n\+[-+]+\+\n([\s\S]+?)\nRealized PNL \(Quote\): ([-\d.]+) \| Unrealized PNL \(Quote\): ([-\d.]+)--> Global PNL \(Quote\): ([-\d.]+) \| Global PNL \(%\): ([-\d.]+)%\nTotal Volume Traded: ([\d.]+)",
            msg)
        for controller in controllers:
            controller_name = controller.group(1)
            controller_data = controller.group(2)
            realized_pnl_quote = controller.group(3)
            unrealized_pnl_quote = controller.group(4)
            global_pnl_quote = controller.group(5)
            global_pnl_pct = controller.group(6)
            total_volume_traded = controller.group(7)


            controller_data_dict[controller_name] = {
                'realized_pnl_quote': realized_pnl_quote,
                'unrealized_pnl_quote': unrealized_pnl_quote,
                'global_pnl_quote': global_pnl_quote,
                'global_pnl_pct': global_pnl_pct,
                'total_volume_traded': total_volume_traded,
            }

        data['controllers'] = controller_data_dict

        # Determining running status from the controller data
        running_status = self.determine_running_status(msg)
        data['running_status'] = running_status
        # Extract global performance summary
        global_performance_pattern = (
            r"Global Performance Summary:\nGlobal PNL \(Quote\): ([-\d.]+) \| Global PNL \(%\): ([-\d.]+)% \| "
            r"Total Volume Traded \(Global\): ([\d.]+)"
        )
        global_performance = re.search(global_performance_pattern, msg)
        if global_performance:
            data['global_performance'] = {
                'global_pnl_quote': global_performance.group(1),
                'global_pnl_pct': global_performance.group(2),
                'total_volume_traded': global_performance.group(3)
            }

        return data

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
            all_bots_status[bot] = {key: value for key, value in bot_info.items() if key != "broker_client"}
        return all_bots_status
