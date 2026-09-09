"""A config field that holds objects has to say what those objects look like.

``GET /connectors/{name}/config-map`` is what a client reads to build a credentials form.
Its type resolver handled ``Literal`` and ``Optional[...]`` and fell through to
``str(field_type)`` for everything else, so xrpl's ``custom_markets`` came back as::

    "typing.Dict[str, hummingbot.connector.exchange.xrpl.xrpl_utils.XRPLMarket]"

a bare Python repr. It names no field of the object the caller has to send, so a client
cannot render an input for it without hardcoding per-connector knowledge.
"""
import pytest

pytest.importorskip("hummingbot")

from hummingbot.client.settings import AllConnectorSettings  # noqa: E402

from services.unified_connector_service import UnifiedConnectorService  # noqa: E402


@pytest.fixture(autouse=True)
def _connector_settings():
    AllConnectorSettings.create_connector_settings()


def test_a_dict_of_models_reports_the_shape_of_one_entry():
    field = UnifiedConnectorService.get_connector_config_map("xrpl")["custom_markets"]
    assert field["type"] == "Dict"
    assert field["value_shape"] == {
        "base": "str",
        "quote": "str",
        "base_issuer": "str",
        "quote_issuer": "str",
        "trading_pair_symbol": "Optional[str]",
    }


def test_every_other_field_is_described_exactly_as_before():
    """Blast radius: only dict-of-model fields change shape."""
    config_map = UnifiedConnectorService.get_connector_config_map("xrpl")
    assert config_map["xrpl_secret_key"]["type"] == "SecretStr"
    assert config_map["wss_node_urls"]["type"] == "list[str]"
    assert config_map["max_request_per_minute"]["type"] == "int"
    for name, field in config_map.items():
        if name != "custom_markets":
            assert "value_shape" not in field


def test_a_plain_dict_is_left_alone():
    """Only a dict whose values are models has a shape worth describing."""
    assert UnifiedConnectorService._dict_value_shape(dict[str, str]) is None
    assert UnifiedConnectorService._dict_value_shape(list[str]) is None
    assert UnifiedConnectorService._dict_value_shape(str) is None


def test_optional_is_unwrapped_rather_than_printed_raw():
    from typing import Optional

    assert UnifiedConnectorService._type_label(Optional[str]) == "Optional[str]"
    assert UnifiedConnectorService._type_label(str) == "str"
