"""Market-data routes must be able to name the account whose connector to use.

``MarketDataService`` has taken an ``account_name`` for a long time — it is what makes
``get_best_connector_for_market`` prefer that account's live trading connector over the
shared keyless data connector. The request models never carried the field, so the routes
could not pass it and every caller silently landed on whichever connector the fallback
happened to pick. Two calls seconds apart could resolve against different connectors.

``AddTradingPairRequest`` already had the field; these tests pin that the read-side
models grew the same one, and that the value actually reaches the service.
"""
import pytest

pytest.importorskip("hummingbot")

from models.market_data import (  # noqa: E402
    FundingInfoRequest,
    OrderBookQueryRequest,
    OrderBookRequest,
    PriceRequest,
)

MODELS = [
    (PriceRequest, {"connector_name": "xrpl", "trading_pairs": ["BTC-XRP"]}),
    (FundingInfoRequest, {"connector_name": "binance_perpetual", "trading_pair": "BTC-USDT"}),
    (OrderBookRequest, {"connector_name": "xrpl", "trading_pair": "BTC-XRP"}),
    (OrderBookQueryRequest, {"connector_name": "xrpl", "trading_pair": "BTC-XRP", "is_buy": True}),
]


@pytest.mark.parametrize("model,payload", MODELS, ids=lambda v: getattr(v, "__name__", ""))
def test_account_name_is_accepted(model, payload):
    assert model(**payload, account_name="master_account").account_name == "master_account"


@pytest.mark.parametrize("model,payload", MODELS, ids=lambda v: getattr(v, "__name__", ""))
def test_account_name_is_optional(model, payload):
    """Omitting it keeps the previous fallback behaviour, so this is not a breaking change."""
    assert model(**payload).account_name is None


@pytest.mark.asyncio
async def test_the_route_passes_it_through():
    """The half that actually matters: the field is threaded into the service call."""
    from routers.market_data import get_prices

    seen = {}

    class FakeService:
        async def get_prices(self, connector_name, trading_pairs, account_name=None):
            seen["account_name"] = account_name
            return {"BTC-XRP": 1.0}

    await get_prices(
        PriceRequest(connector_name="xrpl", trading_pairs=["BTC-XRP"], account_name="master_account"),
        market_data_manager=FakeService(),
    )
    assert seen["account_name"] == "master_account"
