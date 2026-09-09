"""A Docker operation that failed must not answer 200.

`POST /docker/stop-container/{name}` used to return the service's error verbatim, so a
container that does not exist answered **HTTP 200** with a raw docker-py string as the
body:

    "404 Client Error for http+docker://localhost/v1.55/containers/x/json: Not Found
     (\"No such container: x\")"

Two defects in one line. A caller that checks the status code -- which is how a caller
checks -- read a container that was never stopped as one that had been. And the body
published the daemon's socket URL and negotiated API version, which is a detail of how
this API talks to Docker, not an answer to anything the caller asked.

Every container route in this router shared the shape: `except DockerException: return
str(e)`.

Run with: pytest test/test_docker_routes_report_failure.py -v
"""
import pytest
from docker.errors import APIError, DockerException, NotFound
from fastapi import HTTPException

from routers.docker import (
    active_containers,
    available_images,
    clean_exited_containers,
    exited_containers,
    start_container,
    stop_container,
)
from services.docker_service import DockerService

# What docker-py actually raises, socket URL and API version included.
NOT_FOUND = NotFound(
    '404 Client Error for http+docker://localhost/v1.55/containers/ghost/json: '
    'Not Found ("No such container: ghost")'
)
DAEMON_ERROR = APIError(
    "500 Server Error for http+docker://localhost/v1.55/containers/ghost/stop: "
    "Server Error (\"cannot stop container\")"
)


class _Containers:
    def __init__(self, error=None, container=None):
        self.error = error
        self.container = container
        self.pruned = 0

    def get(self, name):
        if isinstance(self.error, DockerException):
            raise self.error
        return self.container

    def list(self, **kwargs):
        if isinstance(self.error, DockerException):
            raise self.error
        return []

    def prune(self):
        if isinstance(self.error, DockerException):
            raise self.error
        self.pruned += 1


class _Container:
    def __init__(self, error=None):
        self.error = error
        self.stopped = 0
        self.started = 0

    def stop(self):
        if self.error:
            raise self.error
        self.stopped += 1

    def start(self):
        if self.error:
            raise self.error
        self.started += 1


def _service(error=None, container=None):
    service = DockerService.__new__(DockerService)
    service.client = type("_Client", (), {})()
    service.client.containers = _Containers(error=error, container=container)
    service.client.images = _Containers(error=error)
    return service


class TestAMissingContainerIs404:
    @pytest.mark.asyncio
    async def test_stopping_one_raises_404(self):
        with pytest.raises(HTTPException) as raised:
            await stop_container("ghost", _service(error=NOT_FOUND))

        assert raised.value.status_code == 404
        assert raised.value.detail == "No such container: ghost"

    @pytest.mark.asyncio
    async def test_starting_one_raises_404(self):
        with pytest.raises(HTTPException) as raised:
            await start_container("ghost", _service(error=NOT_FOUND))

        assert raised.value.status_code == 404

    @pytest.mark.asyncio
    async def test_the_detail_never_carries_the_daemon_socket_or_api_version(self):
        """The reported body leaked both. Neither is an answer to what was asked."""
        with pytest.raises(HTTPException) as raised:
            await stop_container("ghost", _service(error=NOT_FOUND))

        assert "http+docker" not in raised.value.detail
        assert "v1.55" not in raised.value.detail
        assert "Client Error" not in raised.value.detail


class TestADaemonFailureIs502:
    @pytest.mark.asyncio
    async def test_a_stop_the_daemon_refuses_raises_502(self):
        service = _service(container=_Container(error=DAEMON_ERROR))

        with pytest.raises(HTTPException) as raised:
            await stop_container("bot-1", service)

        assert raised.value.status_code == 502
        assert "http+docker" not in raised.value.detail

    @pytest.mark.asyncio
    async def test_listing_containers_with_no_daemon_raises_502(self):
        """It used to answer 200 with an error string where a list was documented."""
        for route in (active_containers, exited_containers):
            with pytest.raises(HTTPException) as raised:
                await route(None, _service(error=DAEMON_ERROR))
            assert raised.value.status_code == 502

    @pytest.mark.asyncio
    async def test_pruning_with_no_daemon_raises_502(self):
        with pytest.raises(HTTPException) as raised:
            await clean_exited_containers(_service(error=DAEMON_ERROR))

        assert raised.value.status_code == 502

    @pytest.mark.asyncio
    async def test_listing_images_with_no_daemon_raises_502(self):
        with pytest.raises(HTTPException) as raised:
            await available_images(None, _service(error=DAEMON_ERROR))

        assert raised.value.status_code == 502


class TestSuccessStillAnswersNormally:
    @pytest.mark.asyncio
    async def test_a_stop_that_works_reports_it(self):
        """It used to return null, which is indistinguishable from a failure body."""
        container = _Container()
        service = _service(container=container)

        response = await stop_container("bot-1", service)

        assert response["success"] is True
        assert container.stopped == 1

    @pytest.mark.asyncio
    async def test_a_start_that_works_reports_it(self):
        container = _Container()
        service = _service(container=container)

        response = await start_container("bot-1", service)

        assert response["success"] is True
        assert container.started == 1

    @pytest.mark.asyncio
    async def test_listing_containers_passes_the_list_through(self):
        assert await active_containers(None, _service()) == []

    @pytest.mark.asyncio
    async def test_pruning_that_works_reports_it(self):
        service = _service()

        response = await clean_exited_containers(service)

        assert response["success"] is True
        assert service.client.containers.pruned == 1


class TestTheServiceKeepsItsInBandContract:
    """stop-and-archive calls stop_container in a retry loop and reads the container's
    status to decide whether it worked, so a failure here must stay a return value."""

    def test_a_failed_stop_returns_rather_than_raises(self):
        result = _service(error=NOT_FOUND).stop_container("ghost")

        assert result["success"] is False
        assert result["error"] == "not_found"

    def test_a_failed_removal_returns_the_success_flag_it_always_did(self):
        result = _service(error=NOT_FOUND).remove_container("ghost")

        assert result["success"] is False
        assert "http+docker" not in result["message"]
