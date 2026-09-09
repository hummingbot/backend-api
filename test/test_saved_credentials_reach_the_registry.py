"""A saved connector config must behave the same after a restart as when it was added.

``update_connector_keys`` publishes a connector's config to ``AllConnectorSettings`` when
credentials are added. Loading the same config back from disk did not, so the registry —
which is what anything building a connector without an account in hand reads, notably the
shared keyless data connector — kept the connector's *class defaults* instead.

The visible effect was a custom market that worked until the process bounced: a public
price lookup for it succeeded while the adding process was alive, then failed with
"Market <PAIR> not found in markets list" after a restart, with the credential file on
disk unchanged the whole time. Found by running the API in a container and restarting it.
"""
import pytest

pytest.importorskip("hummingbot")

from pathlib import Path  # noqa: E402

from hummingbot.client.settings import AllConnectorSettings  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from services.unified_connector_service import UnifiedConnectorService  # noqa: E402
from utils.security import BackendAPISecurity  # noqa: E402

CUSTOM_PAIR = "BTC-XRP"
ISSUER = "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B"


@pytest.fixture
def saved_config(monkeypatch, tmp_path):
    """A decrypted xrpl config, as load_connector_config_map_from_file would return it."""
    from hummingbot.connector.exchange.xrpl.xrpl_utils import XRPLConfigMap, XRPLMarket

    from utils.hummingbot_api_config_adapter import HummingbotAPIConfigAdapter

    AllConnectorSettings.create_connector_settings()
    config = HummingbotAPIConfigAdapter(
        XRPLConfigMap(
            xrpl_secret_key=SecretStr("sEdNEVERLEAKTHIS"),
            custom_markets={
                CUSTOM_PAIR: XRPLMarket(
                    base="BTC", quote="XRP", base_issuer=ISSUER, quote_issuer=""
                )
            },
        )
    )
    monkeypatch.setattr("utils.security.connector_name_from_file", lambda p: "xrpl")
    monkeypatch.setattr(
        BackendAPISecurity, "load_connector_config_map_from_file", classmethod(lambda cls, p: config)
    )
    yield config
    AllConnectorSettings.reset_connector_config_keys("xrpl")


def test_loading_from_disk_publishes_to_the_registry(saved_config):
    """The regression: without this the registry keeps the class default, so the custom
    market is invisible to anything built without an account."""
    assert CUSTOM_PAIR not in AllConnectorSettings.get_connector_config_keys("xrpl").custom_markets

    BackendAPISecurity.decrypt_connector_config(Path("credentials/master_account/connectors/xrpl.yml"))

    assert CUSTOM_PAIR in AllConnectorSettings.get_connector_config_keys("xrpl").custom_markets


def test_it_still_lands_in_the_decrypted_config_store(saved_config):
    """The existing behaviour this must not disturb: authenticated connectors read here."""
    BackendAPISecurity.decrypt_connector_config(Path("credentials/master_account/connectors/xrpl.yml"))
    assert BackendAPISecurity.decrypted_value("xrpl") is saved_config


def test_a_keyless_connector_gets_the_market_but_not_the_secret(saved_config):
    """Publishing carries a decrypted secret into the registry, exactly as the add path
    already does. Nothing keyless may be handed that value."""
    BackendAPISecurity.decrypt_connector_config(Path("credentials/master_account/connectors/xrpl.yml"))

    values = UnifiedConnectorService._public_config_values(
        AllConnectorSettings.get_connector_config_keys("xrpl")
    )
    assert CUSTOM_PAIR in values["custom_markets"]
    assert values["xrpl_secret_key"] == ""
    assert "NEVERLEAKTHIS" not in str(values)
