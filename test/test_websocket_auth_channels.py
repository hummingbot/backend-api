"""
Tests for WebSocket handshake authentication (SEC-059).

Credentials must travel in headers only — never in the query string, which uvicorn writes
to its access log for every handshake — and an unauthenticated peer must be refused at the
handshake instead of being accepted and then closed with 4001.

Run with: pytest test/test_websocket_auth_channels.py -v
"""
import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from config import settings
from routers.websocket import AUTH_SUBPROTOCOL, router

WS_PATHS = ["/ws/market-data", "/ws/executors"]


class _StubManager:
    """Minimal stand-in for the real managers: the tests never get past the handshake."""

    def generate_connection_id(self):
        return "test-conn"

    async def handle_subscribe(self, *args, **kwargs):
        pass

    async def handle_unsubscribe(self, *args, **kwargs):
        pass

    def remove_connection(self, conn_id):
        pass


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    app.state.websocket_manager = _StubManager()
    app.state.executor_ws_manager = _StubManager()
    return TestClient(app)


@pytest.fixture
def credentials():
    return settings.security.username, settings.security.password


def _basic_header(user: str, password: str) -> dict:
    blob = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {blob}"}


def _subprotocols(user: str, password: str) -> list:
    blob = base64.urlsafe_b64encode(f"{user}:{password}".encode()).decode().rstrip("=")
    return [AUTH_SUBPROTOCOL, blob]


class TestQueryParamCredentialsAreRejected:
    """The query-string channel is gone; it must not authenticate anything."""

    @pytest.mark.parametrize("path", WS_PATHS)
    def test_username_password_query_params_are_rejected(self, client, credentials, path):
        user, password = credentials
        with pytest.raises(WebSocketDenialResponse) as exc:
            with client.websocket_connect(f"{path}?username={user}&password={password}"):
                pass
        assert exc.value.status_code == 401

    @pytest.mark.parametrize("path", WS_PATHS)
    def test_token_query_param_is_rejected(self, client, credentials, path):
        user, password = credentials
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        with pytest.raises(WebSocketDenialResponse) as exc:
            with client.websocket_connect(f"{path}?token={token}"):
                pass
        assert exc.value.status_code == 401

    def test_no_code_path_reads_credentials_from_the_query_string(self):
        import inspect

        import routers.websocket as ws_module

        source = inspect.getsource(ws_module)
        assert "query_params" not in source


class TestHeaderChannelsAuthenticate:
    @pytest.mark.parametrize("path", WS_PATHS)
    def test_authorization_basic_header_succeeds(self, client, credentials, path):
        user, password = credentials
        with client.websocket_connect(path, headers=_basic_header(user, password)) as ws:
            message = ws.receive_json()
        assert message["type"] == "connected"

    @pytest.mark.parametrize("path", WS_PATHS)
    def test_browser_subprotocol_channel_succeeds_and_is_echoed(self, client, credentials, path):
        user, password = credentials
        with client.websocket_connect(path, subprotocols=_subprotocols(user, password)) as ws:
            message = ws.receive_json()
            assert ws.accepted_subprotocol == AUTH_SUBPROTOCOL
        assert message["type"] == "connected"

    @pytest.mark.parametrize("path", WS_PATHS)
    def test_padded_standard_base64_in_the_subprotocol_also_decodes(self, client, credentials, path):
        user, password = credentials
        blob = base64.b64encode(f"{user}:{password}".encode()).decode()
        with client.websocket_connect(path, subprotocols=[AUTH_SUBPROTOCOL, blob]) as ws:
            message = ws.receive_json()
        assert message["type"] == "connected"


class TestUnauthenticatedIsRefusedAtTheHandshake:
    @pytest.mark.parametrize("path", WS_PATHS)
    def test_no_credentials_at_all(self, client, path):
        with pytest.raises(WebSocketDenialResponse) as exc:
            with client.websocket_connect(path):
                pass
        assert exc.value.status_code == 401
        assert "basic" in exc.value.headers.get("www-authenticate", "").lower()

    @pytest.mark.parametrize("path", WS_PATHS)
    def test_wrong_password_in_the_header(self, client, credentials, path):
        user, _ = credentials
        with pytest.raises(WebSocketDenialResponse) as exc:
            with client.websocket_connect(path, headers=_basic_header(user, "not-the-password")):
                pass
        assert exc.value.status_code == 401

    @pytest.mark.parametrize("path", WS_PATHS)
    def test_wrong_password_in_the_subprotocol(self, client, credentials, path):
        user, _ = credentials
        with pytest.raises(WebSocketDenialResponse) as exc:
            with client.websocket_connect(path, subprotocols=_subprotocols(user, "nope")):
                pass
        assert exc.value.status_code == 401

    @pytest.mark.parametrize("path", WS_PATHS)
    def test_garbage_credentials_do_not_raise_out_of_the_handler(self, client, path):
        with pytest.raises(WebSocketDenialResponse) as exc:
            with client.websocket_connect(path, headers={"Authorization": "Basic !!!not-base64"}):
                pass
        assert exc.value.status_code == 401

    @pytest.mark.parametrize("path", WS_PATHS)
    def test_subprotocol_without_a_credential_blob(self, client, path):
        with pytest.raises(WebSocketDenialResponse) as exc:
            with client.websocket_connect(path, subprotocols=[AUTH_SUBPROTOCOL]):
                pass
        assert exc.value.status_code == 401
