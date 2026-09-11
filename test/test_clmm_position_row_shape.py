"""A CLMM position row must not depend on which path recorded it.

Positions reach `gateway_clmm_positions` two ways: `/gateway/clmm/open` opens one, and
the transaction poller's discovery sweep finds one that was opened elsewhere (the UI, an
executor talking to Gateway directly). Each used to assemble the row itself, and the key
sets had drifted apart — discovery wrote `lower_bin_id`, `upper_bin_id`, `base_fee_pending`
and `quote_fee_pending`, the route wrote `position_rent`, and neither wrote the other's
columns. The same logical position therefore had two different shapes in one table, so
anything reading it back (PnL, the position search, the gas_token backfill) had to know
which writer it was looking at.

`build_position_row` is now the only thing that decides what a new position row contains,
and these tests pin that: the two writers produce the same key set, every key is a real
column, and each writer still records what only it can know.
"""
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest

from database.models import GatewayCLMMPosition
from services.gateway_clmm_service import GatewayCLMMService

WALLET = "82SggYRE2Vo4jN4a2pk3aQ4SET4ctafZJGbowmCqyHx5"
POOL = "2sf5NYcY4zUPXUSmG6f66mskb24t5F8S11pC1Nz5nQT3"
POSITION = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
SIGNATURE = "5xLmQ5s5xZ9jTqk3Y8bNvW2pR7cH4dF6gJ1kM3nP9qS8tU4vX6yZ2aB5cD7eF9gH1jK3zM5nP7qR9sT"
SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
NETWORK = "solana-mainnet-beta"


def _service(rows):
    """A GatewayCLMMService whose repository records the rows it is handed."""

    class _Repo:
        def __init__(self, session):
            pass

        async def create_position(self, position_data):
            rows.append(position_data)
            return SimpleNamespace(id=7)

        async def create_event(self, event_data):
            return SimpleNamespace(id=11)

    manager = SimpleNamespace()

    @asynccontextmanager
    async def session_context():
        yield object()

    manager.get_session_context = session_context

    service = GatewayCLMMService(db_manager=manager)
    service.repository_class = _Repo
    return service


async def _route_row():
    """The row `/gateway/clmm/open` writes, via the service the route calls."""
    rows = []
    await _service(rows).record_open_position(
        position_address=POSITION,
        pool_address=POOL,
        network=NETWORK,
        connector="meteora",
        wallet_address=WALLET,
        trading_pair=f"{SOL}-{USDC}",
        base_token=SOL,
        quote_token=USDC,
        lower_price=Decimal("150"),
        upper_price=Decimal("250"),
        entry_price=200.0,
        base_amount_added=Decimal("0.0099"),
        quote_amount_added=Decimal("1.98"),
        position_rent=0.05788,
        transaction_hash=SIGNATURE,
        gas_fee=0.000011772,
        gas_token="SOL",
        tx_status="CONFIRMED",
    )
    assert len(rows) == 1
    return rows[0]


# One entry of Gateway's /trading/clmm/positions-owned listing, as the sweep sees it.
DISCOVERED = {
    "address": POSITION,
    "poolAddress": POOL,
    "baseTokenAddress": SOL,
    "quoteTokenAddress": USDC,
    "price": 200.0,
    "lowerPrice": 150.0,
    "upperPrice": 250.0,
    "baseTokenAmount": 0.0099,
    "quoteTokenAmount": 1.98,
    "baseFeeAmount": 0.00031,
    "quoteFeeAmount": 0.062,
    "lowerBinId": -1841,
    "upperBinId": -1789,
}


async def _discovered_row(pos_data=None):
    """The row the poller's discovery sweep writes, via the same service."""
    rows = []
    created = await _service(rows).record_discovered_position(
        pos_data=pos_data if pos_data is not None else DISCOVERED,
        connector="meteora",
        network=NETWORK,
        wallet_address=WALLET,
    )
    assert created is True
    assert len(rows) == 1
    return rows[0]


@pytest.mark.asyncio
async def test_both_writers_produce_the_same_key_set():
    # The whole point of the item: one table, one row shape, regardless of writer.
    assert set(await _route_row()) == set(await _discovered_row())


@pytest.mark.asyncio
async def test_every_column_written_exists_on_the_model():
    # A unified key set is only useful if every key is real: create_position passes
    # the dict straight to GatewayCLMMPosition(**row), so a stray key raises.
    columns = {column.name for column in GatewayCLMMPosition.__table__.columns}
    assert set(await _route_row()) <= columns
    assert set(await _discovered_row()) <= columns


@pytest.mark.asyncio
async def test_a_discovered_position_keeps_what_only_the_chain_reports():
    # Reconciling the key sets must not cost the four columns discovery alone can
    # fill: bins identify a Meteora range, and pending fees may have been accruing
    # for days before the sweep first saw the position.
    row = await _discovered_row()
    assert row["lower_bin_id"] == -1841
    assert row["upper_bin_id"] == -1789
    assert row["base_fee_pending"] == 0.00031
    assert row["quote_fee_pending"] == 0.062
    # Unknowable for a position opened elsewhere — NULL, never a fabricated 0.
    assert row["position_rent"] is None
    # What the chain holds now stands in for the deposit nothing recorded.
    assert row["initial_base_token_amount"] == 0.0099
    assert row["entry_price"] == 200.0


@pytest.mark.asyncio
async def test_the_open_route_keeps_the_rent_and_leaves_the_rest_null():
    row = await _route_row()
    assert row["position_rent"] == 0.05788
    # Gateway reports bins on a position it lists, not on the open response.
    assert row["lower_bin_id"] is None
    assert row["upper_bin_id"] is None
    # Nothing has accrued or been collected on a position opened a moment ago.
    assert row["base_fee_pending"] == 0.0
    assert row["quote_fee_pending"] == 0.0
    assert row["base_fee_collected"] == 0.0
    assert row["quote_fee_collected"] == 0.0
    # in_range stays UNKNOWN until something observes the position on-chain.
    assert row["in_range"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_both_writers_agree_on_the_derived_columns():
    # Same range, same price: the two paths reached percentage by different
    # arithmetic (one on the request's Decimals, one on Gateway's floats) and must
    # not disagree about the same position.
    route, discovered = await _route_row(), await _discovered_row()
    assert route["percentage"] == discovered["percentage"]
    assert route["percentage"] == float(Decimal("100") / Decimal("150"))
    assert route["lower_price"] == discovered["lower_price"] == 150.0
    assert route["upper_price"] == discovered["upper_price"] == 250.0
    assert route["status"] == discovered["status"] == "OPEN"


@pytest.mark.asyncio
async def test_a_discovered_position_records_whether_it_is_in_range():
    # The sweep has a live price and the range, so unlike the open route it can say.
    assert (await _discovered_row())["in_range"] == "IN_RANGE"
    assert (await _discovered_row({**DISCOVERED, "price": 300.0}))["in_range"] == "OUT_OF_RANGE"
    # No price to compare against is not "out of range", it is not known.
    assert (await _discovered_row({**DISCOVERED, "price": 0}))["in_range"] == "UNKNOWN"
