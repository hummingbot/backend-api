from pathlib import Path

from hummingbot.client.config.config_crypt import PASSWORD_VERIFICATION_WORD, BaseSecretsManager
from hummingbot.client.config.config_helpers import (
    ClientConfigAdapter,
    _load_yml_data_into_map,
    connector_name_from_file,
    get_connector_hb_config,
    read_yml_file,
    update_connector_hb_config,
)
from hummingbot.client.config.security import Security

from config import PASSWORD_VERIFICATION_PATH
from utils.file_system import FileSystemUtil
from utils.models import BackendAPIConfigAdapter


class BackendAPISecurity(Security):
    fs_util = FileSystemUtil(base_path="bots/credentials")

    @classmethod
    def login_account(cls, account_name: str, secrets_manager: BaseSecretsManager) -> bool:
        if not cls.validate_password(secrets_manager):
            return False
        cls.secrets_manager = secrets_manager
        cls.decrypt_all(account_name=account_name)
        return True

    @classmethod
    def decrypt_all(cls, account_name: str = "master_account"):
        cls._secure_configs.clear()
        cls._decryption_done.clear()
        encrypted_files = [file for file in cls.fs_util.list_files(directory=f"{account_name}/connectors") if
                           file.endswith(".yml")]
        for file in encrypted_files:
            path = Path(cls.fs_util.base_path + f"/{account_name}/connectors/" + file)
            cls.decrypt_connector_config(path)
        cls._decryption_done.set()

    @classmethod
    def decrypt_connector_config(cls, file_path: Path):
        connector_name = connector_name_from_file(file_path)
        cls._secure_configs[connector_name] = cls.load_connector_config_map_from_file(file_path)

    @classmethod
    def load_connector_config_map_from_file(cls, yml_path: Path) -> BackendAPIConfigAdapter:
        config_data = read_yml_file(yml_path)
        connector_name = connector_name_from_file(yml_path)
        hb_config = get_connector_hb_config(connector_name)
        config_map = BackendAPIConfigAdapter(hb_config)
        _load_yml_data_into_map(config_data, config_map)
        return config_map

    @classmethod
    def update_connector_keys(cls, account_name: str, connector_config: ClientConfigAdapter):
        connector_name = connector_config.connector
        file_path = cls.fs_util.get_connector_keys_path(account_name=account_name, connector_name=connector_name)
        cm_yml_str = connector_config.generate_yml_output_str_with_comments()
        cls.fs_util.ensure_file_and_dump_text(file_path, cm_yml_str)
        update_connector_hb_config(connector_config)
        cls._secure_configs[connector_name] = connector_config

    @staticmethod
    def new_password_required() -> bool:
        return not PASSWORD_VERIFICATION_PATH.exists()

    @staticmethod
    def store_password_verification(secrets_manager: BaseSecretsManager):
        encrypted_word = secrets_manager.encrypt_secret_value(PASSWORD_VERIFICATION_WORD, PASSWORD_VERIFICATION_WORD)
        FileSystemUtil.ensure_file_and_dump_text(PASSWORD_VERIFICATION_PATH, encrypted_word)

    @staticmethod
    def validate_password(secrets_manager: BaseSecretsManager) -> bool:
        valid = False
        with open(PASSWORD_VERIFICATION_PATH, "r") as f:
            encrypted_word = f.read()
        try:
            decrypted_word = secrets_manager.decrypt_secret_value(PASSWORD_VERIFICATION_WORD, encrypted_word)
            valid = decrypted_word == PASSWORD_VERIFICATION_WORD
        except ValueError as e:
            if str(e) != "MAC mismatch":
                raise e
        return valid
