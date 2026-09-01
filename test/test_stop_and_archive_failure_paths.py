"""
Tests for the stop-and-archive failure paths.

``BotsOrchestrator.stop_and_archive_bot`` used to ``return`` silently when the
bot process failed to stop, or when the container was still alive after every
retry. The run row was left stopped-but-not-archived with no ``error_message``,
and the caller had already been told the background task started fine. These
tests pin that both paths now close the run out with an error state.

Run with: pytest test/test_stop_and_archive_failure_paths.py -v
"""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import services.bots_orchestrator as orchestrator_module
from services.bots_orchestrator import BotsOrchestrator

BOT_NAME = "hummingbot-pmm-1"


class _StubOrchestrator(BotsOrchestrator):
    """Real stop_and_archive_bot, but no Docker/MQTT/database side effects."""

    def __init__(self, stop_response):  # noqa: D107 - deliberately skips BotsOrchestrator.__init__
        self.active_bots = {BOT_NAME: {"status": "running"}}
        self.stopping_bots = set()
        self.mqtt_manager = SimpleNamespace(clear_bot_data=lambda name: None)
        self.db_manager = SimpleNamespace(get_session_context=self._session)
        self._stop_response = stop_response

    @asynccontextmanager
    async def _session(self):
        yield None

    def get_bot_status(self, bot_name):
        return {"performance": {}}

    async def mark_bot_run_stopped(self, bot_name, final_status=None):
        return None

    async def stop_bot(self, bot_name, **kwargs):
        return self._stop_response


class _StubDocker:
    """Container that never reaches the ``exited`` state."""

    def __init__(self):
        self.stop_attempts = 0
        self.removed = []

    def stop_container(self, container_name):
        self.stop_attempts += 1

    def get_container_status(self, container_name):
        return {"state": {"status": "running"}}

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
    """The workflow sleeps between steps; skip the wall-clock waits."""
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(orchestrator_module.asyncio, "sleep", _instant)


@pytest.fixture
def run_updates(monkeypatch):
    """Capture every terminal update applied to the bot run row."""
    seen = []

    class _StubRepo:
        def __init__(self, session):
            pass

        async def update_bot_run_archived(self, bot_name):
            seen.append(("archived", bot_name))

        async def update_bot_run_stopped(self, bot_name, final_status=None, error_message=None):
            seen.append(("stopped", bot_name, error_message))

    monkeypatch.setattr(orchestrator_module, "BotRunRepository", _StubRepo)
    return seen


async def _run(orch, docker_manager):
    await orch.stop_and_archive_bot(
        bot_name=BOT_NAME,
        skip_order_cancellation=True,
        archive_locally=True,
        s3_bucket=None,
        docker_manager=docker_manager,
        bot_archiver=_StubArchiver(),
    )


async def test_failed_bot_stop_marks_the_run_errored(no_sleep, run_updates):
    """A refused bot stop leaves an error state naming the failed step."""
    orch = _StubOrchestrator({"success": False, "error": "mqtt timeout"})
    docker_manager = _StubDocker()

    await _run(orch, docker_manager)

    assert len(run_updates) == 1
    kind, bot_name, error_message = run_updates[0]
    assert (kind, bot_name) == ("stopped", BOT_NAME)
    assert "Failed to stop bot process" in error_message
    assert "mqtt timeout" in error_message
    # It really did bail out before touching the container.
    assert docker_manager.stop_attempts == 0
    assert docker_manager.removed == []
    # Cleanup still runs.
    assert orch.stopping_bots == set()
    assert BOT_NAME not in orch.active_bots


async def test_no_stop_response_marks_the_run_errored(no_sleep, run_updates):
    """A missing stop response is recorded too, not swallowed."""
    orch = _StubOrchestrator(None)

    await _run(orch, _StubDocker())

    assert len(run_updates) == 1
    assert run_updates[0][0:2] == ("stopped", BOT_NAME)
    assert "No response from bot orchestrator" in run_updates[0][2]


async def test_container_stop_retry_exhaustion_marks_the_run_errored(no_sleep, run_updates):
    """A container that never exits leaves an error naming the retry count."""
    orch = _StubOrchestrator({"success": True})
    docker_manager = _StubDocker()

    await _run(orch, docker_manager)

    assert docker_manager.stop_attempts == 10
    assert len(run_updates) == 1
    kind, bot_name, error_message = run_updates[0]
    assert (kind, bot_name) == ("stopped", BOT_NAME)
    assert error_message == "Failed to stop container after 10 attempts"
    # It bailed out before archiving or removing anything.
    assert docker_manager.removed == []
    assert orch.stopping_bots == set()
    assert BOT_NAME not in orch.active_bots


async def test_database_failure_while_recording_the_error_is_swallowed(no_sleep, monkeypatch):
    """Recording the error must never raise out of the background task."""
    class _ExplodingRepo:
        def __init__(self, session):
            raise RuntimeError("database is down")

    monkeypatch.setattr(orchestrator_module, "BotRunRepository", _ExplodingRepo)
    orch = _StubOrchestrator({"success": False, "error": "mqtt timeout"})

    await _run(orch, _StubDocker())

    assert orch.stopping_bots == set()
    assert BOT_NAME not in orch.active_bots
