from pathlib import Path

from hummingbot.client.config.config_crypt import PASSWORD_VERIFICATION_WORD, BaseSecretsManager
from hummingbot.client.config.config_helpers import (
    ClientConfigAdapter,
    connector_name_from_file,
    get_connector_hb_config,
    read_yml_file,
    update_connector_hb_config,
)
from hummingbot.client.config.security import Security

from config import settings
from utils.file_system import fs_util
from utils.hummingbot_api_config_adapter import HummingbotAPIConfigAdapter


class BackendAPISecurity(Security):
    @classmethod
    def login_account(cls, account_name: str, secrets_manager: BaseSecretsManager) -> bool:
        if not cls.validate_password(secrets_manager):
            return False
        cls.secrets_manager = secrets_manager
        # Also set on parent Security class for hummingbot's ClientConfigAdapter methods
        # that access Security.secrets_manager directly
        Security.secrets_manager = secrets_manager
        cls.decrypt_all(account_name=account_name)
        return True

    @classmethod
    def decrypt_all(cls, account_name: str = "master_account"):
        cls._secure_configs.clear()
        cls._decryption_done.clear()
        encrypted_files = [file for file in fs_util.list_files(directory=f"credentials/{account_name}/connectors") if
                           file.endswith(".yml")]
        for file in encrypted_files:
            path = Path(fs_util.base_path + f"/credentials/{account_name}/connectors/" + file)
            cls.decrypt_connector_config(path)
        cls._decryption_done.set()

    @classmethod
    def decrypt_connector_config(cls, file_path: Path):
        connector_name = connector_name_from_file(file_path)
        config_map = cls.load_connector_config_map_from_file(file_path)
        cls._secure_configs[connector_name] = config_map
        # Publish it to the connector-settings registry, which is what anything building a
        # connector without an account in hand reads — notably the shared keyless data
        # connector behind public market-data lookups.
        #
        # update_connector_keys already does this when credentials are added, so without it
        # here the same account behaved differently before and after a restart: a custom
        # market added through the API resolved until the process bounced, then silently
        # went back to the connector's class defaults, and public price lookups for it
        # failed with "Market <PAIR> not found in markets list". Loading from disk now
        # leaves the registry in the same state adding them does.
        #
        # This carries the account's decrypted secret into the registry, exactly as the
        # add path already does. Nothing keyless is handed that value:
        # _public_config_values() blanks every SecretStr before it reaches a data connector.
        update_connector_hb_config(config_map)

    @classmethod
    def load_connector_config_map_from_file(cls, yml_path: Path) -> HummingbotAPIConfigAdapter:
        config_data = read_yml_file(yml_path)
        connector_name = connector_name_from_file(yml_path)
        hb_config = get_connector_hb_config(connector_name).model_validate(config_data)
        config_map = HummingbotAPIConfigAdapter(hb_config)
        config_map.decrypt_all_secure_data()
        return config_map

    @classmethod
    def update_connector_keys(cls, account_name: str, connector_config: ClientConfigAdapter):
        connector_name = connector_config.connector
        file_path = fs_util.get_connector_keys_path(account_name=account_name, connector_name=connector_name)
        cm_yml_str = connector_config.generate_yml_output_str_with_comments()
        fs_util.ensure_file_and_dump_text(str(file_path), cm_yml_str)
        update_connector_hb_config(connector_config)
        cls._secure_configs[connector_name] = connector_config

    @staticmethod
    def new_password_required() -> bool:
        full_path = fs_util._get_full_path(settings.app.password_verification_path)
        return not Path(full_path).exists()

    @staticmethod
    def validate_password(secrets_manager: BaseSecretsManager) -> bool:
        valid = False
        full_path = fs_util._get_full_path(settings.app.password_verification_path)
        with open(full_path, "r") as f:
            encrypted_word = f.read()
        try:
            decrypted_word = secrets_manager.decrypt_secret_value(PASSWORD_VERIFICATION_WORD, encrypted_word)
            valid = decrypted_word == PASSWORD_VERIFICATION_WORD
        except ValueError as e:
            if str(e) != "MAC mismatch":
                raise e
        return valid

    @staticmethod
    def store_password_verification(secrets_manager: BaseSecretsManager):
        encrypted_word = secrets_manager.encrypt_secret_value(PASSWORD_VERIFICATION_WORD, PASSWORD_VERIFICATION_WORD)
        fs_util.ensure_file_and_dump_text(settings.app.password_verification_path, encrypted_word)
