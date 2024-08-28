from typing import Any, Dict

from hummingbot.client.config.config_helpers import ClientConfigAdapter
from pydantic import SecretStr


class BackendAPIConfigAdapter(ClientConfigAdapter):
    def _encrypt_secrets(self, conf_dict: Dict[str, Any]):
        from utils.security import BackendAPISecurity
        for attr, value in conf_dict.items():
            attr_type = self._hb_config.__fields__[attr].type_
            if attr_type == SecretStr:
                clear_text_value = value.get_secret_value() if isinstance(value, SecretStr) else value
                conf_dict[attr] = BackendAPISecurity.secrets_manager.encrypt_secret_value(attr, clear_text_value)

    def _decrypt_secrets(self, conf_dict: Dict[str, Any]):
        from utils.security import BackendAPISecurity
        for attr, value in conf_dict.items():
            attr_type = self._hb_config.__fields__[attr].type_
            if attr_type == SecretStr:
                decrypted_value = BackendAPISecurity.secrets_manager.decrypt_secret_value(attr, value.get_secret_value())
                conf_dict[attr] = SecretStr(decrypted_value)

    def _decrypt_all_internal_secrets(self):
        from utils.security import BackendAPISecurity
        for traversal_item in self.traverse():
            if traversal_item.type_ == SecretStr:
                encrypted_value = traversal_item.value
                if isinstance(encrypted_value, SecretStr):
                    encrypted_value = encrypted_value.get_secret_value()
                decrypted_value = BackendAPISecurity.secrets_manager.decrypt_secret_value(traversal_item.attr, encrypted_value)
                parent_attributes = traversal_item.config_path.split(".")[:-1]
                config = self
                for parent_attribute in parent_attributes:
                    config = getattr(config, parent_attribute)
                setattr(config, traversal_item.attr, decrypted_value)

    def decrypt_all_secure_data(self):
        from utils.security import BackendAPISecurity

        secure_config_items = (
            traversal_item
            for traversal_item in self.traverse()
            if traversal_item.client_field_data is not None and traversal_item.client_field_data.is_secure
        )
        for traversal_item in secure_config_items:
            value = traversal_item.value
            if isinstance(value, SecretStr):
                value = value.get_secret_value()
            if value == "" or BackendAPISecurity.secrets_manager is None:
                decrypted_value = value
            else:
                decrypted_value = BackendAPISecurity.secrets_manager.decrypt_secret_value(attr=traversal_item.attr, value=value)
            *intermediate_items, final_config_element = traversal_item.config_path.split(".")
            config_model = self
            if len(intermediate_items) > 0:
                for attr in intermediate_items:
                    config_model = config_model.__getattr__(attr)
            setattr(config_model, final_config_element, decrypted_value)
