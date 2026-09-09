"""A keyless data connector must withhold credentials, not its whole configuration.

Data connectors serve public market data, so they are built without API keys. The way
that used to be done was to blank *every* field in the connector's config map::

    api_keys = {key: "" for key in connector_config.__class__.model_fields if key != "connector"}

which also destroys the plain configuration a connector needs to describe its markets.
On xrpl that field is ``custom_markets`` (a ``Dict[str, XRPLMarket]``): blanked to ``""``
it becomes ``{}`` inside the constructor via ``custom_markets or {}``, so every market
defined there vanishes and public price lookups for those pairs fail with
"Market <PAIR> not found in markets list" — while the very same pair resolves fine
through an authenticated trading connector. Kraken lost its ``kraken_api_tier`` default
("Starter") to the same line.

These tests pin both halves of the contract: credentials stay out, configuration stays in.
"""
import pytest

pytest.importorskip("hummingbot")

from hummingbot.client.settings import AllConnectorSettings  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from services.unified_connector_service import UnifiedConnectorService  # noqa: E402


@pytest.fixture(autouse=True)
def _connector_settings():
    AllConnectorSettings.create_connector_settings()


def _public_values(connector_name: str) -> dict:
    config = AllConnectorSettings.get_connector_config_keys(connector_name)
    return UnifiedConnectorService._public_config_values(config)


def test_credentials_are_blanked():
    """The point of a data connector: no secret ever reaches it."""
    values = _public_values("binance")
    assert values["binance_api_key"] == ""
    assert values["binance_api_secret"] == ""


def test_a_saved_secret_still_never_reaches_a_keyless_connector():
    """Adding credentials publishes the account's config map into the global registry
    (``update_connector_hb_config``), so the secret is genuinely present at this point.
    It must still be withheld — this is the test that keeps the fix honest."""
    from hummingbot.connector.exchange.xrpl.xrpl_utils import XRPLConfigMap

    AllConnectorSettings.update_connector_config_keys(
        XRPLConfigMap(xrpl_secret_key=SecretStr("sEdTHISMUSTNEVERLEAK"))
    )
    try:
        values = _public_values("xrpl")
        assert values["xrpl_secret_key"] == ""
        assert "THISMUSTNEVERLEAK" not in str(values)
    finally:
        AllConnectorSettings.reset_connector_config_keys("xrpl")


def test_structured_config_survives_so_its_markets_resolve():
    """The regression itself: a custom market must still be there afterwards."""
    from hummingbot.connector.exchange.xrpl.xrpl_utils import XRPLConfigMap, XRPLMarket

    AllConnectorSettings.update_connector_config_keys(
        XRPLConfigMap(
            xrpl_secret_key=SecretStr("sEdIrrelevant"),
            custom_markets={
                "BTC-XRP": XRPLMarket(
                    base="BTC",
                    quote="XRP",
                    base_issuer="rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B",
                    quote_issuer="",
                    trading_pair_symbol="BTC-XRP",
                )
            },
        )
    )
    try:
        assert "BTC-XRP" in _public_values("xrpl")["custom_markets"]
    finally:
        AllConnectorSettings.reset_connector_config_keys("xrpl")


def test_plain_defaults_survive_too():
    """Not an xrpl quirk: any non-secret default was being flattened to ""."""
    assert _public_values("kraken")["kraken_api_tier"] == "Starter"
    xrpl = _public_values("xrpl")
    assert xrpl["max_request_per_minute"] == 12
    assert xrpl["wss_node_urls"]


def test_a_required_field_has_no_default_to_keep():
    """Required fields stay blank: there is nothing to fall back on, and in practice a
    required connector field is a credential."""
    config = AllConnectorSettings.get_connector_config_keys("binance")
    assert config.__class__.model_fields["binance_api_key"].is_required()
    assert _public_values("binance")["binance_api_key"] == ""
