"""
Tests for the stop-and-archive bot-name plumbing.

``POST /stop-and-archive-bot/{bot_name}`` used to thread three aliases of the
same string (``actual_bot_name`` / ``container_name`` /
``bot_name_for_orchestrator``) into the background task. They are now a single
``bot_name``; these tests pin that the name actually used for every
destructive step (stop container, archive directory, remove container) is the
path parameter, unmodified.

Run with: pytest test/test_stop_and_archive_bot_name.py -v
"""
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

import services.bots_orchestrator as orchestrator_module
from routers.bot_orchestration import stop_and_archive_bot as stop_and_archive_endpoint
from services.bots_orchestrator import BotsOrchestrator

BOT_NAME = "hummingbot-pmm-1"


class _StubOrchestrator(BotsOrchestrator):
    """Real stop_and_archive_bot, but no Docker/MQTT/database side effects."""

    def __init__(self):  # noqa: D107 - deliberately skips BotsOrchestrator.__init__
        self.active_bots = {BOT_NAME: {"status": "running"}}
        self.stopping_bots = set()
        self.mqtt_manager = SimpleNamespace(clear_bot_data=lambda name: self.calls["clear_bot_data"].append(name))
        self.db_manager = SimpleNamespace(get_session_context=self._session)
        self.calls = {
            "get_bot_status": [],
            "mark_bot_run_stopped": [],
            "stop_bot": [],
            "clear_bot_data": [],
        }

    @asynccontextmanager
    async def _session(self):
        yield None

    def get_bot_status(self, bot_name):
        self.calls["get_bot_status"].append(bot_name)
        return {"performance": {}}

    async def mark_bot_run_stopped(self, bot_name, final_status=None):
        self.calls["mark_bot_run_stopped"].append(bot_name)

    async def stop_bot(self, bot_name, **kwargs):
        self.calls["stop_bot"].append(bot_name)
        return {"success": True}


class _StubDocker:
    def __init__(self):
        self.stopped = []
        self.status_checked = []
        self.removed = []

    def stop_container(self, container_name):
        self.stopped.append(container_name)

    def get_container_status(self, container_name):
        self.status_checked.append(container_name)
        return {"state": {"status": "exited"}}

    def remove_container(self, container_name, force=True):
        self.removed.append((container_name, force))
        return {"success": True}


class _StubArchiver:
    def __init__(self):
        self.archived = []

    def archive_locally(self, bot_name, instance_dir):
        self.archived.append((bot_name, instance_dir))

    def archive_and_upload(self, bot_name, instance_dir, bucket_name=None):
        self.archived.append((bot_name, instance_dir, bucket_name))


@pytest.fixture
def no_sleep(monkeypatch):
    """The workflow sleeps 15s for graceful shutdown; skip the wall-clock wait."""
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(orchestrator_module.asyncio, "sleep", _instant)


@pytest.fixture
def archived_runs(monkeypatch):
    """Capture the name passed to the ARCHIVED bot-run update."""
    seen = []

    class _StubRepo:
        def __init__(self, session):
            pass

        async def update_bot_run_archived(self, bot_name):
            seen.append(bot_name)

        async def update_bot_run_stopped(self, bot_name, error_message=None):
            seen.append(("stopped", bot_name, error_message))

    monkeypatch.setattr(orchestrator_module, "BotRunRepository", _StubRepo)
    return seen


async def test_every_step_uses_the_bot_name_verbatim(no_sleep, archived_runs):
    """All 8 steps address the bot by the exact name they were given."""
    orch = _StubOrchestrator()
    docker_manager = _StubDocker()
    bot_archiver = _StubArchiver()

    await orch.stop_and_archive_bot(
        bot_name=BOT_NAME,
        skip_order_cancellation=True,
        archive_locally=True,
        s3_bucket=None,
        docker_manager=docker_manager,
        bot_archiver=bot_archiver,
    )

    # Steps 1-3: MQTT-side identity
    assert orch.calls["get_bot_status"] == [BOT_NAME]
    assert orch.calls["mark_bot_run_stopped"] == [BOT_NAME]
    assert orch.calls["stop_bot"] == [BOT_NAME]
    # Steps 5 and 7: container identity
    assert docker_manager.stopped == [BOT_NAME]
    assert docker_manager.status_checked == [BOT_NAME]
    assert docker_manager.removed == [(BOT_NAME, False)]
    # Step 6: archive identity and instance directory
    assert bot_archiver.archived == [(BOT_NAME, os.path.join("bots", "instances", BOT_NAME))]
    # Step 8: bot run marked archived
    assert archived_runs == [BOT_NAME]
    # Cleanup: stopping flag cleared and bot dropped from active_bots
    assert orch.stopping_bots == set()
    assert orch.calls["clear_bot_data"] == [BOT_NAME]
    assert BOT_NAME not in orch.active_bots


async def test_s3_archive_uses_the_bot_name_verbatim(no_sleep, archived_runs):
    """The S3 branch archives under the same unmodified name."""
    orch = _StubOrchestrator()
    bot_archiver = _StubArchiver()

    await orch.stop_and_archive_bot(
        bot_name=BOT_NAME,
        skip_order_cancellation=True,
        archive_locally=False,
        s3_bucket="my-bucket",
        docker_manager=_StubDocker(),
        bot_archiver=bot_archiver,
    )

    assert bot_archiver.archived == [
        (BOT_NAME, os.path.join("bots", "instances", BOT_NAME), "my-bucket")
    ]


async def test_endpoint_schedules_the_background_task_with_the_path_name():
    """The endpoint hands the background task the path parameter, unmodified."""
    background_tasks = BackgroundTasks()
    bots_manager = SimpleNamespace(
        active_bots={BOT_NAME: {}},
        stop_and_archive_bot="sentinel",
    )
    docker_manager = object()
    bot_archiver = object()

    response = await stop_and_archive_endpoint(
        bot_name=BOT_NAME,
        background_tasks=background_tasks,
        bots_manager=bots_manager,
        docker_manager=docker_manager,
        bot_archiver=bot_archiver,
    )

    assert response["status"] == "success"
    assert response["details"]["bot_name"] == BOT_NAME
    assert len(background_tasks.tasks) == 1
    task = background_tasks.tasks[0]
    assert task.func == "sentinel"
    assert task.kwargs["bot_name"] == BOT_NAME
    assert task.kwargs["docker_manager"] is docker_manager
    assert task.kwargs["bot_archiver"] is bot_archiver
    # The three aliases are gone: the name is passed exactly once.
    assert [k for k in task.kwargs if "name" in k] == ["bot_name"]


async def test_endpoint_reports_not_found_for_an_inactive_bot():
    """An unknown bot is refused before anything destructive is scheduled."""
    background_tasks = BackgroundTasks()
    bots_manager = SimpleNamespace(active_bots={"other-bot": {}}, stop_and_archive_bot="sentinel")

    response = await stop_and_archive_endpoint(
        bot_name=BOT_NAME,
        background_tasks=background_tasks,
        bots_manager=bots_manager,
        docker_manager=object(),
        bot_archiver=object(),
    )

    assert response["status"] == "error"
    assert response["details"]["bot_name"] == BOT_NAME
    assert response["details"]["active_bots"] == ["other-bot"]
    assert background_tasks.tasks == []
