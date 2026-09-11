"""POST /trading/orders/search must page through order history, not round one page.

The route built each order's cursor from `timestamp` and `client_order_id`, but order
rows carry `created_at` and `order_id`, so every row got the cursor "0:" and sorted as
a tie. The page after "0:" was the list minus its first row -- most of the previous
page again -- and it handed "0:" back, so a client that followed the cursor looped
forever over the same rows. Behind that sat a second cap: each account was read as its
newest `limit * 2` rows at offset 0, so even a correct cursor could never reach an
order older than that window, while `has_more` said the history had ended.

These tests drive the real route, service and repository against a real SQL table
(sync SQLite behind an async shim: the environment has no aiosqlite) and walk the
cursor the way a client does, asserting on every order id that comes back.

Run with: pytest test/test_order_history_cursor.py -v
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from database.models import Order
from deps import get_connector_service, get_trading_history_service
from routers import trading
from services.trading_history_service import TradingHistoryService

BASE = datetime(2026, 9, 11, 10, 0, 0, 123456, tzinfo=timezone.utc)
MAX_PAGES = 50


class _AsyncSessionShim:
    """The two calls the order repository makes, over a sync SQLAlchemy session."""

    def __init__(self, session):
        self._session = session

    async def execute(self, query):
        return self._session.execute(query)


class _DbManager:
    def __init__(self, engine):
        self._engine = engine

    @asynccontextmanager
    async def get_session_context(self):
        with Session(self._engine) as session:
            yield _AsyncSessionShim(session)


def _order(client_order_id, created_at, account="master_account", connector="binance", pair="SOL-USDT"):
    return Order(
        client_order_id=client_order_id,
        account_name=account,
        connector_name=connector,
        trading_pair=pair,
        trade_type="BUY",
        order_type="LIMIT",
        amount=1,
        price=200,
        status="FILLED",
        filled_amount=1,
        created_at=created_at,
        updated_at=created_at,
    )


# Eleven orders over two accounts and three connectors. Several share a created_at to
# the microsecond, which only the client_order_id tie-breaker can order.
ORDERS = [
    _order("a-01", BASE),
    _order("a-02", BASE + timedelta(seconds=1)),
    _order("a-03", BASE + timedelta(seconds=1)),
    _order("a-04", BASE + timedelta(seconds=1)),
    _order("a-05", BASE + timedelta(seconds=2), connector="okx"),
    _order("a-06", BASE + timedelta(seconds=3)),
    _order("a-07", BASE + timedelta(seconds=4), connector="okx"),
    _order("a-08", BASE + timedelta(seconds=5), connector="kraken"),
    _order("b-01", BASE + timedelta(seconds=1), account="second_account"),
    _order("b-02", BASE + timedelta(seconds=3, microseconds=1), account="second_account"),
    _order("b-03", BASE + timedelta(seconds=6), account="second_account", connector="okx"),
]


def _newest_first(orders):
    return [o.client_order_id for o in sorted(orders, key=lambda o: (o.created_at, o.client_order_id), reverse=True)]


@pytest.fixture
def client():
    # One connection for the whole test: each new connection to ":memory:" is a new,
    # empty database, and TestClient serves the app from another thread.
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Order.__table__.create(engine)
    with Session(engine) as session:
        # Detached copies, so every test inserts the same rows into its own database.
        session.add_all(
            [_order(o.client_order_id, o.created_at, o.account_name, o.connector_name, o.trading_pair) for o in ORDERS]
        )
        session.commit()

    app = FastAPI()
    app.include_router(trading.router)
    app.dependency_overrides[get_trading_history_service] = lambda: TradingHistoryService(_DbManager(engine))
    app.dependency_overrides[get_connector_service] = lambda: SimpleNamespace(
        get_all_trading_connectors=lambda: {"master_account": {}, "second_account": {}}
    )
    return TestClient(app)


def _walk(client, limit, **filters):
    """Follow next_cursor to the end, the way a client does; fail rather than loop."""
    order_ids, envelopes, cursor = [], [], None
    for _ in range(MAX_PAGES):
        body = {"limit": limit, **filters}
        if cursor:
            body["cursor"] = cursor
        response = client.post("/trading/orders/search", json=body)
        assert response.status_code == 200, response.text
        envelope = response.json()
        envelopes.append(envelope)
        order_ids.extend(order["order_id"] for order in envelope["data"])
        cursor = envelope["pagination"]["next_cursor"]
        if not cursor:
            return order_ids, envelopes
    pytest.fail(f"pagination did not end within {MAX_PAGES} pages; last cursor {cursor!r}: {order_ids}")


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 10, 11, 100])
def test_walking_the_cursor_returns_every_order_once_newest_first(client, limit):
    order_ids, envelopes = _walk(client, limit)

    assert order_ids == _newest_first(ORDERS)
    assert all(len(envelope["data"]) <= limit for envelope in envelopes)
    # has_more is only ever true on a page that hands out a cursor.
    assert [envelope["pagination"]["has_more"] for envelope in envelopes] == [True] * (len(envelopes) - 1) + [False]


def test_the_walk_reaches_orders_older_than_twice_the_page_size(client):
    """Each account used to be read as its newest limit * 2 rows, then cut off."""
    order_ids, _ = _walk(client, limit=2, account_names=["master_account"])

    assert order_ids == _newest_first([o for o in ORDERS if o.account_name == "master_account"])
    assert order_ids[-1] == "a-01"


def test_a_multi_value_filter_pages_like_an_unfiltered_one(client):
    """Two connector_names used to be filtered in memory after the rows were capped."""
    wanted = [o for o in ORDERS if o.connector_name in ("okx", "kraken")]

    order_ids, _ = _walk(client, limit=1, connector_names=["okx", "kraken"])

    assert order_ids == _newest_first(wanted)


def test_total_count_is_every_matching_order_on_every_page(client):
    _, envelopes = _walk(client, limit=3, account_names=["second_account"])

    assert [envelope["pagination"]["total_count"] for envelope in envelopes] == [3]


def test_every_page_carries_the_same_total(client):
    _, envelopes = _walk(client, limit=4)

    assert {envelope["pagination"]["total_count"] for envelope in envelopes} == {len(ORDERS)}


def test_the_cursor_is_not_a_placeholder(client):
    """The old route handed out "0:" for every order."""
    response = client.post("/trading/orders/search", json={"limit": 3})

    cursor = response.json()["pagination"]["next_cursor"]
    assert cursor and cursor != "0:"
    assert cursor.endswith(response.json()["data"][-1]["order_id"])


@pytest.mark.parametrize("cursor", ["0:", "garbage", "2026-09-11T10:00:00|", "not-a-time|a-01"])
def test_an_unreadable_cursor_is_refused_not_answered_with_page_one(client, cursor):
    """An unknown cursor used to restart the walk at page one, looking like fresh rows."""
    response = client.post("/trading/orders/search", json={"limit": 3, "cursor": cursor})

    assert response.status_code == 400, response.text
    assert "cursor" in response.json()["detail"]
