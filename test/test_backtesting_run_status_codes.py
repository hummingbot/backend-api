"""
Regression tests for POST /backtesting/run reporting failures by status code.

The route used to wrap the whole call in `except Exception: return {"error": str(e)}`,
so every failure -- a bad controller config, a dead worker, a run abandoned at the
wall-clock budget -- came back as HTTP 200 with an error string in the body. The
api-client raises only on non-2xx, so it handed that dict straight to the caller and
nothing inspected it: the failure was silent end to end.

Pinned here: a run that raises answers non-2xx, a run terminated by the budget answers
504 with the budget message intact (log-scraping still matches), a successful run is
still a plain 200 payload, and the /tasks submission path is untouched.

Run with: pytest test/test_backtesting_run_status_codes.py -v
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.backtesting_service import BacktestTaskStatus, BacktestTimeout

TIMEOUT_MESSAGE = "Backtest exceeded its wall-clock budget of 1800s and was terminated"

RESULT = {
    "executors": [],
    "processed_data": {},
    "results": {"sharpe_ratio": 0, "net_pnl": 1.5},
    "position_holds": [],
    "position_held_timeseries": [],
    "pnl_timeseries": [],
}


def _body():
    return {
        "start_time": 1735689600,
        "end_time": 1738368000,
        "backtesting_resolution": "1m",
        "trade_cost": 0.0006,
        "config": {"controller_name": "pmm_simple"},
    }


@pytest.fixture
def client_for():
    from deps import get_backtesting_service
    from routers import backtesting

    app = FastAPI()
    app.include_router(backtesting.router)

    def build(service):
        app.dependency_overrides[get_backtesting_service] = lambda: service
        return TestClient(app, raise_server_exceptions=False)

    return build


def _service(**kwargs):
    return SimpleNamespace(**kwargs)


def test_run_failure_is_not_a_200(client_for):
    client = client_for(_service(
        run_backtest_sync=AsyncMock(side_effect=RuntimeError("ValueError: unknown controller 'nope'")),
    ))
    response = client.post("/backtesting/run", json=_body())
    assert response.status_code == 500
    assert "unknown controller" in response.json()["detail"]


def test_run_timeout_is_504_with_the_budget_message(client_for):
    client = client_for(_service(
        run_backtest_sync=AsyncMock(side_effect=BacktestTimeout(TIMEOUT_MESSAGE)),
    ))
    response = client.post("/backtesting/run", json=_body())
    assert response.status_code == 504
    # The text is unchanged so existing log-scraping still matches.
    assert response.json()["detail"] == TIMEOUT_MESSAGE


def test_successful_run_is_still_a_plain_200_payload(client_for):
    client = client_for(_service(run_backtest_sync=AsyncMock(return_value=RESULT)))
    response = client.post("/backtesting/run", json=_body())
    assert response.status_code == 200
    assert response.json()["results"]["net_pnl"] == 1.5


def test_task_submission_is_unchanged(client_for):
    task = SimpleNamespace(task_id="abc12345", status=BacktestTaskStatus.PENDING)
    client = client_for(_service(submit_task=lambda config: task))
    response = client.post("/backtesting/tasks", json=_body())
    assert response.status_code == 200
    assert response.json() == {"task_id": "abc12345", "status": "pending"}
