"""Real PostgreSQL concurrency test; only an explicitly named isolated test database."""
import asyncio
import json
import os
import uuid
from test.test_lending_policy import config
from types import SimpleNamespace

import pytest
from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from config import settings
from database import AsyncDatabaseManager, ExecutorRepository
from database.models import ExecutorRecord
from services.executor_service import ExecutorService


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("isolation", ["READ COMMITTED", "REPEATABLE READ"])
async def test_concurrent_admission_and_restart_keep_the_same_durable_reservation(tmp_path, monkeypatch, isolation):
    url = os.environ.get("AOMI_POLICY_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("AOMI_POLICY_TEST_DATABASE_URL not set")
    parsed = make_url(url)
    assert parsed.host in {"127.0.0.1", "localhost"}
    assert parsed.database.startswith("hummingbot_policy_check_")
    db = AsyncDatabaseManager(url)
    db.async_session = async_sessionmaker(db.engine.execution_options(isolation_level=isolation), expire_on_commit=False)
    async with db.engine.begin() as connection:
        await connection.run_sync(lambda conn: ExecutorRecord.__table__.create(conn, checkfirst=True))
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({
        "wallet": "0x" + "3" * 40, "account_name": "master_account", "controller_limits_raw": {"a": "100", "b": "100"},
        "max_total_supply_raw": "100", "max_action_raw": "100", "max_gas_quote": "1",
    }))
    monkeypatch.setattr(settings.aomi, "lending_policy_file", str(policy_file))
    original = ExecutorRepository.get_executors

    async def slow_read(self, **kwargs):
        rows = await original(self, **kwargs)
        await asyncio.sleep(0.05)  # Without the lock, both writers read the same empty history.
        return rows

    monkeypatch.setattr(ExecutorRepository, "get_executors", slow_read)
    ids = [str(uuid.uuid4()) for _ in range(3)]

    def service():
        instance = object.__new__(ExecutorService)
        instance.db_manager = db
        instance._executor_metadata = {}
        return instance

    async def submit(instance, ident, controller):
        cfg = config(60, require_lending_policy=True)
        instance._executor_metadata[ident] = {
            "executor_type": "onchain_executor", "account_name": "master_account", "controller_id": controller,
            "connector_name": "base", "trading_pair": "USDC-USDT", "config": cfg.model_dump(mode="json"),
        }
        await instance._persist_executor_created(ident, SimpleNamespace(config=cfg, status=SimpleNamespace(name="NOT_STARTED")))

    try:
        # Separate service instances model separate API processes sharing PostgreSQL.
        results = await asyncio.gather(submit(service(), ids[0], "a"), submit(service(), ids[1], "b"), return_exceptions=True)
        assert sum(result is None for result in results) == 1
        assert sum(isinstance(result, ValueError) for result in results) == 1
        # No in-memory state survives in this third instance; the DB reservation still applies.
        with pytest.raises(ValueError, match="remaining"):
            await submit(service(), ids[2], "a")
        monkeypatch.setattr(settings.aomi, "lending_policy_file", "")
        with pytest.raises(ValueError, match="active operator policy"):
            await submit(service(), ids[2], "a")
        async with db.get_session_context() as session:
            records = await original(ExecutorRepository(session), executor_type="onchain_executor", limit=None)
            assert len(records) == 1
            assert json.loads(records[0].config)["lending"]["amount"] == 60
    finally:
        async with db.get_session_context() as session:
            await session.execute(delete(ExecutorRecord).where(ExecutorRecord.executor_id.in_(ids)))
        await db.engine.dispose()
