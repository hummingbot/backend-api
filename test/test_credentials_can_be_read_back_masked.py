"""An operator has to be able to see what the server loaded, without seeing the secrets.

Only the *names* of an account's credentials were readable. There was no way to ask
"does the running server actually have the custom_markets I just saved?" except to
trigger a side-effecting call and infer the answer from how it behaved — which is
exactly how a mis-set custom market stays invisible for hours.

The endpoint that answers it is only safe if it cannot leak a key, so that is what most
of these tests are about.
"""
import pytest

pytest.importorskip("hummingbot")

from fastapi import HTTPException  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from services.accounts_service import AccountsService  # noqa: E402

SECRET = "sEdVERYSECRETVALUEthatmustneverappear"


@pytest.fixture
def service(monkeypatch):
    """An AccountsService with just enough wired up to call get_credentials."""
    from hummingbot.client.config.config_helpers import ClientConfigAdapter
    from hummingbot.connector.exchange.xrpl.xrpl_utils import XRPLConfigMap, XRPLMarket

    svc = AccountsService.__new__(AccountsService)
    svc.secrets_manager = object()

    config = ClientConfigAdapter(
        XRPLConfigMap(
            xrpl_secret_key=SecretStr(SECRET),
            custom_markets={
                "BTC-XRP": XRPLMarket(
                    base="BTC",
                    quote="XRP",
                    base_issuer="rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B",
                    quote_issuer="",
                    trading_pair_symbol="BTC-XRP",
                )
            },
            max_request_per_minute=30,
        )
    )

    monkeypatch.setattr("services.accounts_service.fs_util.path_exists", lambda *_: True)
    monkeypatch.setattr(
        "services.accounts_service.BackendAPISecurity.login_account",
        classmethod(lambda cls, **kwargs: True),
    )
    monkeypatch.setattr(
        "services.accounts_service.BackendAPISecurity.decrypted_value",
        classmethod(lambda cls, name: config),
    )
    return svc


def test_the_secret_is_masked(service):
    values = service.get_credentials("master_account", "xrpl")
    assert values["xrpl_secret_key"] == AccountsService.MASKED_SECRET


def test_the_secret_does_not_appear_anywhere_in_the_response(service):
    """The test that actually matters: no nesting or repr may smuggle it out."""
    assert "VERYSECRETVALUE" not in str(service.get_credentials("master_account", "xrpl"))


def test_the_configuration_you_came_to_check_is_visible(service):
    values = service.get_credentials("master_account", "xrpl")
    assert values["custom_markets"]["BTC-XRP"]["base_issuer"] == "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B"
    assert values["custom_markets"]["BTC-XRP"]["trading_pair_symbol"] == "BTC-XRP"
    assert values["max_request_per_minute"] == 30


def test_the_response_is_json_serialisable(service):
    """It is returned straight from a route, so nested models must already be dicts."""
    import json

    json.dumps(service.get_credentials("master_account", "xrpl"))


def test_a_missing_credential_is_a_404_not_a_500(monkeypatch):
    svc = AccountsService.__new__(AccountsService)
    monkeypatch.setattr("services.accounts_service.fs_util.path_exists", lambda *_: False)
    with pytest.raises(FileNotFoundError):
        svc.get_credentials("master_account", "xrpl")


def test_a_traversal_attempt_is_refused(monkeypatch):
    svc = AccountsService.__new__(AccountsService)
    monkeypatch.setattr("services.accounts_service.fs_util.path_exists", lambda *_: True)
    with pytest.raises(HTTPException) as excinfo:
        svc.get_credentials("../../etc", "xrpl")
    assert excinfo.value.status_code == 400
