"""
Tests for the bot-runs payload size fix.

The ``final_status`` blob is ~99% of a bot run record's bytes, so the list
endpoint must omit it by default while the detail endpoint keeps returning it.

Run with: pytest test/test_bot_runs_payload.py -v
"""
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest

from services.bots_orchestrator import BotsOrchestrator


def _make_run(run_id: int = 1):
    """A BotRun-shaped stub with every field the serializer reads."""
    return SimpleNamespace(
        id=run_id,
        bot_name=f"bot-{run_id}",
        instance_name=f"hummingbot-bot-{run_id}",
        deployed_at=datetime(2026, 7, 30, 12, 0, 0),
        stopped_at=None,
        strategy_type="controller",
        strategy_name="pmm_simple",
        config_name="conf.yml",
        account_name="master_account",
        image_version="latest",
        deployment_status="ARCHIVED",
        run_status="STOPPED",
        deployment_config={"script": "v2_with_controllers.py"},
        final_status={"performance": ["x" * 10000]},
        error_message=None,
    )


class _StubOrchestrator(BotsOrchestrator):
    """Real orchestrator methods, but built without Docker/MQTT side effects."""

    def __init__(self, runs):  # noqa: D107 - deliberately skips BotsOrchestrator.__init__
        self.runs = runs
        self.db_manager = SimpleNamespace(get_session_context=self._session)

    @asynccontextmanager
    async def _session(self):
        yield None


@pytest.fixture
def patched_repo(monkeypatch):
    """Patch BotRunRepository so the orchestrator methods hit stub data."""
    runs = [_make_run(1), _make_run(2)]

    class _StubRepo:
        def __init__(self, session):
            pass

        async def get_bot_runs(self, **kwargs):
            return runs

        async def get_bot_run_by_id(self, bot_run_id):
            return next((r for r in runs if r.id == bot_run_id), None)

    monkeypatch.setattr("services.bots_orchestrator.BotRunRepository", _StubRepo)
    return runs


class TestSerializer:
    """Direct tests of the parameterized serializer."""

    def test_includes_final_status_by_default(self):
        serialized = BotsOrchestrator._serialize_bot_run(_make_run())
        assert "final_status" in serialized

    def test_omits_final_status_when_disabled(self):
        serialized = BotsOrchestrator._serialize_bot_run(_make_run(), include_final_status=False)
        assert "final_status" not in serialized

    def test_other_fields_are_unchanged_when_omitting_final_status(self):
        run = _make_run()
        full = BotsOrchestrator._serialize_bot_run(run)
        slim = BotsOrchestrator._serialize_bot_run(run, include_final_status=False)

        assert set(full) - set(slim) == {"final_status"}
        assert all(slim[key] == full[key] for key in slim)
        # deployment_config stays in the slim payload on purpose (~359 B/record).
        assert slim["deployment_config"] == run.deployment_config


class TestOrchestratorPaths:
    """The list path drops the blob; the detail path keeps it."""

    @pytest.mark.asyncio
    async def test_list_omits_final_status_by_default(self, patched_repo):
        orchestrator = _StubOrchestrator(patched_repo)
        result = await orchestrator.get_bot_runs()

        assert len(result) == 2
        assert all("final_status" not in run for run in result)

    @pytest.mark.asyncio
    async def test_list_opt_in_restores_final_status(self, patched_repo):
        orchestrator = _StubOrchestrator(patched_repo)
        result = await orchestrator.get_bot_runs(include_final_status=True)

        assert all("final_status" in run for run in result)

    @pytest.mark.asyncio
    async def test_detail_includes_final_status(self, patched_repo):
        orchestrator = _StubOrchestrator(patched_repo)
        result = await orchestrator.get_bot_run_by_id(1)

        assert result is not None
        assert result["final_status"] == {"performance": ["x" * 10000]}


class TestRouterDefaults:
    """The query param must default to off — that is the whole point of the fix."""

    def test_include_final_status_defaults_to_false(self):
        import inspect

        from routers.bot_orchestration import get_bot_runs

        param = inspect.signature(get_bot_runs).parameters["include_final_status"]
        assert param.default is False
