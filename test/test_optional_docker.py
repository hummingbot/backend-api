from unittest.mock import Mock, patch

import pytest
from docker.errors import DockerException

from services.bots_orchestrator import BotsOrchestrator
from services.gateway_service import GatewayService


def test_gateway_connects_on_use_and_retries_after_unavailable_docker():
    client = Mock()
    with patch("services.gateway_service.docker.from_env", side_effect=[DockerException("offline"), client]) as connect:
        service = GatewayService()
        connect.assert_not_called()
        with pytest.raises(DockerException):
            _ = service.client
        assert service.client is client
        assert service.client is client
        assert connect.call_count == 2


def test_bot_discovery_connects_on_use_and_retries_after_unavailable_docker():
    client = Mock()
    client.containers.list.return_value = []
    with patch("services.bots_orchestrator.MQTTManager"), patch(
        "services.bots_orchestrator.docker.from_env", side_effect=[DockerException("offline"), client]
    ) as connect:
        service = BotsOrchestrator("localhost", 1883, "", "", db_manager=Mock())
        connect.assert_not_called()
        with pytest.raises(DockerException):
            service._sync_get_active_containers()
        assert service._sync_get_active_containers() == []
        assert service._sync_get_active_containers() == []
        assert connect.call_count == 2
