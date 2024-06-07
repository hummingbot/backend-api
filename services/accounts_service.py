import asyncio
import logging
from decimal import Decimal

from fastapi import HTTPException
from hummingbot.client.config.client_config_map import ClientConfigMap
from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger
from hummingbot.client.config.config_helpers import ClientConfigAdapter, ReadOnlyClientConfigAdapter, \
    get_connector_class
from hummingbot.client.settings import AllConnectorSettings

from config import CONFIG_PASSWORD
from utils.file_system import FileSystemUtil
from utils.models import BackendAPIConfigAdapter
from utils.security import BackendAPISecurity

file_system = FileSystemUtil()


class AccountsService:
    """
    This class is responsible for managing all the accounts that are connected to the trading system. It is responsible
    to initialize all the connectors that are connected to each account, keep track of the balances of each account and
    update the balances of each account.
    """

    def __init__(self, update_balances_interval: int = 10):
        # TODO: Add database to store the balances of each account each time it is updated.
        self.secrets_manager = ETHKeyFileSecretManger(CONFIG_PASSWORD)
        self.accounts = {}
        self.initialize_accounts()
        self.update_balances_interval = update_balances_interval

    def start_update_balances_loop(self):
        """
        Start the loop that updates the balances of all the accounts at a fixed interval.
        :return:
        """
        asyncio.create_task(self.update_balances_loop())

    def get_all_balances(self):
        """
        Get the balances of all the accounts.
        :return: The balances of all the accounts.
        """
        balances_filtered = {}
        all_balances = {account_name: {connector_name: connector.get_all_balances() for connector_name, connector in
                                       connectors.items()}
                        for account_name, connectors in self.accounts.items()}
        for account_name, connectors in all_balances.items():
            balances_filtered[account_name] = {}
            for connector_name, balances in connectors.items():
                balances_filtered[account_name][connector_name] = {key: value for key, value in balances.items() if
                                                                  value != Decimal("0")}
        return balances_filtered

    async def update_balances_loop(self):
        """
        The loop that updates the balances of all the accounts at a fixed interval.
        :return:
        """
        while True:
            try:
                await self.update_balances()
            except Exception as e:
                logging.error(f"Error updating balances: {e}")
            finally:
                await asyncio.sleep(self.update_balances_interval)

    def initialize_accounts(self):
        """
        Initialize all the connectors that are connected to each account.
        :return:
        """
        for account_name in self.list_accounts():
            self.accounts[account_name] = {}
            for connector_name in self.list_credentials(account_name):
                try:
                    connector_name = connector_name.split(".")[0]
                    connector = self.get_connector(account_name, connector_name)
                    self.accounts[account_name][connector_name] = connector
                except Exception as e:
                    logging.error(f"Error initializing connector {connector_name}: {e}")

    async def update_balances(self):
        """
        Update the balances of all the accounts.
        :return:
        """
        tasks = []
        for account_name, connector in self.accounts.items():
            for connector_instance in connector.values():
                tasks.append(connector_instance._update_balances())
        await asyncio.gather(*tasks)

    @staticmethod
    def get_connector_config_map(connector_name: str):
        """
        Get the connector config map for the specified connector.
        :param connector_name: The name of the connector.
        :return: The connector config map.
        """
        connector_config = BackendAPIConfigAdapter(AllConnectorSettings.get_connector_config_keys(connector_name))
        return [key for key in connector_config.hb_config.__fields__.keys() if key != "connector"]

    async def add_connector_keys(self, account_name: str, connector_name: str, keys: dict):
        BackendAPISecurity.login_account(account_name=account_name, secrets_manager=self.secrets_manager)
        connector_config = BackendAPIConfigAdapter(AllConnectorSettings.get_connector_config_keys(connector_name))
        for key, value in keys.items():
            setattr(connector_config, key, value)
        BackendAPISecurity.update_connector_keys(account_name, connector_config)
        new_connector = self.get_connector(account_name, connector_name)
        await new_connector._update_balances()
        self.accounts[account_name][connector_name] = new_connector

    def get_connector(self, account_name: str, connector_name: str):
        """
        Get the connector object for the specified account and connector.
        :param account_name: The name of the account.
        :param connector_name: The name of the connector.
        :return: The connector object.
        """
        BackendAPISecurity.login_account(account_name=account_name, secrets_manager=self.secrets_manager)
        client_config_map = ClientConfigAdapter(ClientConfigMap())
        conn_setting = AllConnectorSettings.get_connector_settings()[connector_name]
        keys = BackendAPISecurity.api_keys(connector_name)
        read_only_config = ReadOnlyClientConfigAdapter.lock_config(client_config_map)
        init_params = conn_setting.conn_init_parameters(
            trading_pairs=[],
            trading_required=True,
            api_keys=keys,
            client_config_map=read_only_config,
        )
        connector_class = get_connector_class(connector_name)
        connector = connector_class(**init_params)
        return connector

    @staticmethod
    def list_accounts():
        """
        List all the accounts that are connected to the trading system.
        :return: List of accounts.
        """
        return file_system.list_folders('credentials')

    def list_credentials(self, account_name: str):
        """
        List all the credentials that are connected to the specified account.
        :param account_name: The name of the account.
        :return: List of credentials.
        """
        try:
            return [file for file in file_system.list_files(f'credentials/{account_name}/connectors') if
                    file.endswith('.yml')]
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @staticmethod
    def delete_credentials(account_name: str, connector_name: str):
        """
        Delete the credentials of the specified account and connector.
        :param account_name: The name of the account.
        :param connector_name: The name of the connector.
        :return:
        """
        file_system.delete_file(directory=f"credentials/{account_name}/connectors", file_name=f"{connector_name}.yml")

    @staticmethod
    def add_account(account_name: str):
        """
        Add a new account.
        :param account_name: The name of the account.
        :return:
        """
        files_to_copy = ["conf_client.yml", "conf_fee_overrides.yml", "hummingbot_logs.yml", ".password_verification"]
        file_system.create_folder('credentials', account_name)
        file_system.create_folder(f'credentials/{account_name}', "connectors")
        for file in files_to_copy:
            file_system.copy_file(f"credentials/master_account/{file}", f"credentials/{account_name}/{file}")

    @staticmethod
    def delete_account(account_name: str):
        """
        Delete the specified account.
        :param account_name:
        :return:
        """
        file_system.delete_folder('credentials', account_name)
