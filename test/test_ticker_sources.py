import pytest

from services.ticker_sources import fetch_tickers


class FakeBingXConnector:
    def __init__(self):
        self.calls = []

    async def trading_pair_symbol_map(self):
        return {"BTC-USDT": "BTC-USDT"}

    async def _api_get(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "code": 0,
            "data": [
                {
                    "symbol": "BTC-USDT",
                    "bidPrice": "59999.00",
                    "askPrice": "60001.00",
                    "lastPrice": "60000.00",
                    "volume": "12.5",
                    "quoteVolume": "750000.00",
                },
            ],
        }

    async def _api_post(self, **kwargs):
        raise AssertionError(f"unexpected POST request: {kwargs}")


@pytest.mark.asyncio
async def test_bing_x_bulk_ticker_request_and_mapping():
    connector = FakeBingXConnector()

    tickers = await fetch_tickers(connector, "bing_x", raise_on_error=True)

    assert connector.calls == [
        {"path_url": "/openApi/spot/v1/ticker/24hr", "is_auth_required": False},
    ]
    ticker = tickers["BTC-USDT"]
    assert ticker.price == 60000
    assert ticker.base_volume == 12.5
    assert ticker.quote_volume == 750000
