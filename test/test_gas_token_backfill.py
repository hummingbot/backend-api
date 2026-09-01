"""Historical liquidity rows get the gas token their chain actually pays in (CORR-104).

Before ARCH-054 unified the chain -> native-gas-token map, two write paths damaged
gateway_clmm_events.gas_token and neither will revisit the rows:

  - the add/remove handlers' two-branch ternary ("SOL" if solana else "ETH" if ethereum
    else None) wrote NULL on base/arbitrum/polygon, and a row inserted CONFIRMED is never
    re-polled, so the NULL is permanent;
  - the transaction poller's old 6-entry dict wrote the literal "UNKNOWN" for those same
    chains.

The parser is fixed going forward; these tests pin the one-shot repair of what it left
behind — including that it resolves through get_native_gas_token rather than a second copy
of the map, that it refuses to invent a token for a chain the map does not know, and that
running it twice is the same as running it once.
"""
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from database.models import GatewayCLMMEvent, GatewayCLMMPosition
from database.repositories.gateway_clmm_repository import GatewayCLMMRepository
from services.gateway_client import get_native_gas_token


class _AsyncSessionAdapter:
    """The async surface the repository uses, over a real synchronous Session.

    aiosqlite is not installed in this environment, and the backfill only awaits
    execute/flush — so this runs the real SELECT, the real join and the real UPDATE
    against the real schema. Mocking the session away would prove nothing about a
    query whose whole job is finding the right rows.
    """

    def __init__(self, session: Session):
        self._session = session

    async def execute(self, statement):
        return self._session.execute(statement)

    async def flush(self):
        self._session.flush()


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    GatewayCLMMPosition.__table__.create(engine)
    GatewayCLMMEvent.__table__.create(engine)
    session = Session(engine)

    def position(network):
        row = GatewayCLMMPosition(
            position_address=f"pos-{network}-{id(network)}",
            pool_address="pool",
            network=network,
            connector="raydium/clmm",
            wallet_address="wallet",
            trading_pair="SOL-USDC",
            base_token="SOL",
            quote_token="USDC",
            lower_price=Decimal("1"),
            upper_price=Decimal("2"),
        )
        session.add(row)
        session.flush()
        return row

    def event(network, gas_token, event_type="ADD_LIQUIDITY", gas_fee=Decimal("0.005")):
        row = GatewayCLMMEvent(
            position_id=position(network).id,
            transaction_hash=f"tx-{network}-{gas_token}-{event_type}-{gas_fee}",
            event_type=event_type,
            gas_fee=gas_fee,
            gas_token=gas_token,
            status="CONFIRMED",
        )
        session.add(row)
        session.flush()
        return row

    @asynccontextmanager
    async def run():
        """One backfill run against the same database."""
        yield GatewayCLMMRepository(_AsyncSessionAdapter(session))

    try:
        yield type("DB", (), {
            "event": staticmethod(event),
            "run": staticmethod(run),
            "session": session,
        })
    finally:
        session.close()
        engine.dispose()


async def _backfill(db):
    async with db.run() as repo:
        return await repo.backfill_liquidity_gas_tokens()


@pytest.mark.asyncio
async def test_a_null_gas_token_is_filled_from_the_positions_chain(db):
    """The add/remove ternary's damage: CONFIRMED with a fee, no currency named."""
    row = db.event("base-mainnet", None)

    report = await _backfill(db)

    assert row.gas_token == "ETH"
    assert report["fixed"] == 1


@pytest.mark.asyncio
async def test_an_unknown_gas_token_is_replaced(db):
    """The poller's 6-entry dict answered "UNKNOWN" for base, arbitrum and polygon."""
    rows = [db.event("arbitrum-mainnet", "UNKNOWN"), db.event("polygon-mainnet", "UNKNOWN")]

    report = await _backfill(db)

    assert [row.gas_token for row in rows] == ["ETH", "MATIC"]
    assert report["fixed"] == 2


@pytest.mark.asyncio
async def test_an_already_correct_row_is_left_alone(db):
    row = db.event("solana-mainnet-beta", "SOL")

    report = await _backfill(db)

    assert row.gas_token == "SOL"
    assert report == {"fixed": 0, "unresolved": 0, "unresolved_networks": []}


@pytest.mark.asyncio
async def test_every_value_written_is_the_one_the_helper_returns(db):
    """Acceptance criterion: the map is not reimplemented here."""
    rows = {chain: db.event(f"{chain}-mainnet", None)
            for chain in ("solana", "ethereum", "polygon", "avalanche",
                          "optimism", "arbitrum", "base", "bsc", "cronos")}

    await _backfill(db)

    assert {chain: row.gas_token for chain, row in rows.items()} == {
        chain: get_native_gas_token(chain) for chain in rows
    }


@pytest.mark.asyncio
async def test_a_chain_the_map_does_not_know_is_reported_not_papered_over(db):
    """An unmapped chain must surface as a count, not as a fabricated token."""
    row = db.event("hyperliquid-mainnet", None)

    report = await _backfill(db)

    assert row.gas_token is None
    assert report["fixed"] == 0
    assert report["unresolved"] == 1
    assert report["unresolved_networks"] == ["hyperliquid-mainnet"]


@pytest.mark.asyncio
async def test_a_row_that_recorded_no_gas_fee_names_no_currency(db):
    """A pending/feeless row's NULL is not damage: the routes write gas_token only
    alongside a fee, and the poller still owns that row."""
    row = db.event("base-mainnet", None, gas_fee=None)

    report = await _backfill(db)

    assert row.gas_token is None
    assert report["fixed"] == 0


@pytest.mark.asyncio
async def test_running_it_twice_produces_the_same_result(db):
    """Acceptance criterion: idempotent."""
    rows = [db.event("base-mainnet", None), db.event("arbitrum-mainnet", "UNKNOWN"),
            db.event("solana-mainnet-beta", "SOL"), db.event("hyperliquid-mainnet", None)]

    first = await _backfill(db)
    after_first = [row.gas_token for row in rows]
    second = await _backfill(db)

    assert after_first == [row.gas_token for row in rows] == ["ETH", "ETH", "SOL", None]
    assert first["fixed"] == 2 and second["fixed"] == 0
    assert first["unresolved"] == second["unresolved"] == 1
