"""`stopped_at` is written as an aware UTC instant, on both paths that write it.

`update_bot_run_stopped` used `datetime.utcnow()` -- correct in value, naive in type --
against a `TIMESTAMP(timezone=True)` column, so the driver stored it as if it were
already in the session's local timezone and the row landed the server's UTC offset
behind the real stop time (8 hours, on the machine this was reported from).

stop-and-archive masked it: `update_bot_run_archived` runs afterwards with an aware
value and overwrites the wrong one. A bot stopped through plain
`POST /bot-orchestration/stop-bot` and never archived kept the skew forever, and both
run duration and the attribution of performance-history windows to a run are read off
this field.

Run with: pytest test/test_bot_run_stopped_at_is_utc.py -v
"""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from database.repositories.bot_run_repository import BotRunRepository

REPOSITORIES = Path(__file__).resolve().parent.parent / "database" / "repositories"


class _FakeSession:
    """Just the surface these two methods touch."""

    def __init__(self, bot_run):
        self.bot_run = bot_run

    async def execute(self, statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self.bot_run)

    async def flush(self):
        pass

    async def refresh(self, instance):
        pass


def _bot_run():
    return SimpleNamespace(
        bot_name="tz_repro-20260908-233145",
        run_status="RUNNING",
        deployment_status="DEPLOYED",
        stopped_at=None,
        final_status=None,
        error_message=None,
    )


@pytest.mark.asyncio
async def test_stopping_a_bot_records_an_aware_utc_instant():
    bot_run = _bot_run()

    await BotRunRepository(_FakeSession(bot_run)).update_bot_run_stopped("tz_repro")

    assert bot_run.run_status == "STOPPED"
    assert bot_run.stopped_at.tzinfo is not None, (
        "naive datetime on a TIMESTAMP(timezone=True) column: it is stored as local time"
    )
    assert bot_run.stopped_at.utcoffset() == timedelta(0)
    assert abs(bot_run.stopped_at - datetime.now(timezone.utc)) < timedelta(seconds=10)


@pytest.mark.asyncio
async def test_an_errored_stop_records_it_the_same_way():
    """The error branch writes the same field and had the same defect."""
    bot_run = _bot_run()

    await BotRunRepository(_FakeSession(bot_run)).update_bot_run_stopped(
        "tz_repro", error_message="container exited"
    )

    assert bot_run.run_status == "ERROR"
    assert bot_run.stopped_at.utcoffset() == timedelta(0)


@pytest.mark.asyncio
async def test_archiving_agrees_with_stopping():
    """The archive path was already correct; it is what masked the bug on stop-and-archive.

    Pinned so the two never drift apart again: whichever call lands last must write the
    same kind of instant.
    """
    bot_run = _bot_run()
    repository = BotRunRepository(_FakeSession(bot_run))

    await repository.update_bot_run_stopped("tz_repro")
    stopped_at = bot_run.stopped_at
    await repository.update_bot_run_archived("tz_repro")

    assert bot_run.stopped_at.utcoffset() == stopped_at.utcoffset() == timedelta(0)
    assert abs(bot_run.stopped_at - stopped_at) < timedelta(seconds=10)


def test_no_repository_writes_a_naive_utcnow():
    """The class of bug, not just the instance.

    Every timestamp column in database/models.py is TIMESTAMP(timezone=True), so
    `datetime.utcnow()` anywhere in a repository is a row that will be read back at the
    server's UTC offset. Nothing in the code or its tests said so until this test.
    """
    offenders = []
    for path in sorted(REPOSITORIES.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "utcnow"
            ):
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, (
        f"naive utcnow() written to timezone-aware columns at: {', '.join(offenders)} -- "
        f"use datetime.now(timezone.utc)"
    )
