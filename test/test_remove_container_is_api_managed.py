"""
Tests for the ownership guard on ``POST /docker/remove-container/{container_name}``.

The endpoint used to refuse any name that did not start with ``hummingbot-``. Nothing names bot
containers that way — ``DockerService.create_hummingbot_instance`` passes the instance name to
Docker verbatim — so the guard rejected exactly the containers this API creates while happily
accepting the infrastructure containers that *do* carry the prefix (``hummingbot-postgres``,
``hummingbot-broker``).

The guard is now the real invariant: a container is removable here only if it owns the
``bots/instances/<container_name>`` directory that this endpoint archives.

Run with: pytest test/test_remove_container_is_api_managed.py -v
"""
import os

import pytest
from fastapi import HTTPException

from routers.docker import remove_container


class _StubDocker:
    def __init__(self):
        self.removed = []

    def remove_container(self, container_name):
        self.removed.append(container_name)
        return {"success": True, "message": f"Container {container_name} removed successfully."}


class _StubArchiver:
    def __init__(self):
        self.archived = []

    def archive_locally(self, instance_name, instance_dir):
        self.archived.append((instance_name, instance_dir))


@pytest.fixture
def instances_root(tmp_path, monkeypatch):
    """Run inside a scratch cwd so 'bots/instances' is a real, empty directory we control."""
    root = tmp_path / "bots" / "instances"
    root.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    return root


async def _remove(name, docker, archiver):
    return await remove_container(
        container_name=name,
        archive_locally=True,
        s3_bucket=None,
        docker_service=docker,
        bot_archiver=archiver,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bot_name", ["pmm-1", "hummingbot-pmm-1", "my_bot.v2"])
async def test_api_managed_container_is_removed_whatever_its_name(instances_root, bot_name):
    """A container this API created is removable regardless of whether its name carries a prefix."""
    (instances_root / bot_name).mkdir()
    docker, archiver = _StubDocker(), _StubArchiver()

    response = await _remove(bot_name, docker, archiver)

    assert response["success"] is True
    assert docker.removed == [bot_name]
    assert archiver.archived == [(bot_name, os.path.join("bots", "instances", bot_name))]


@pytest.mark.asyncio
@pytest.mark.parametrize("container_name", ["nginx", "hummingbot-postgres", "hummingbot-broker"])
async def test_unrelated_host_container_is_refused(instances_root, container_name):
    """No instance directory means it is not ours - including the prefixed infra containers."""
    docker, archiver = _StubDocker(), _StubArchiver()

    with pytest.raises(HTTPException) as exc:
        await _remove(container_name, docker, archiver)

    assert exc.value.status_code == 400
    assert "managed by this API" in exc.value.detail
    assert docker.removed == []
    assert archiver.archived == []


@pytest.mark.asyncio
async def test_name_escaping_the_instances_directory_is_refused(instances_root):
    """Containment still holds: a traversing name never reaches remove/archive."""
    docker, archiver = _StubDocker(), _StubArchiver()

    with pytest.raises(HTTPException) as exc:
        await _remove(os.path.join("..", "credentials"), docker, archiver)

    assert exc.value.status_code == 400
    assert docker.removed == []
    assert archiver.archived == []
