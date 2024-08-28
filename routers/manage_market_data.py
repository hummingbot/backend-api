import asyncio

from fastapi import APIRouter
from hummingbot.data_feed.candles_feed.candles_factory import CandlesConfig, CandlesFactory
from pydantic import BaseModel

router = APIRouter(tags=["Market Data"])
candles_factory = CandlesFactory()


class HistoricalCandlesConfig(BaseModel):
    connector_name: str = "binance_perpetual"
    trading_pair: str = "BTC-USDT"
    interval: str = "3m"
    start_time: int = 1672542000
    end_time: int = 1672628400


@router.post("/real-time-candles")
async def get_candles(candles_config: CandlesConfig):
    try:
        candles = candles_factory.get_candle(candles_config)
        candles.start()
        while not candles.ready:
            await asyncio.sleep(1)
        df = candles.candles_df
        candles.stop()
        df.drop_duplicates(subset=["timestamp"], inplace=True)
        return df
    except Exception as e:
        return {"error": str(e)}


@router.post("/historical-candles")
async def get_historical_candles(config: HistoricalCandlesConfig):
    try:
        candles_config = CandlesConfig(
            connector=config.connector_name,
            trading_pair=config.trading_pair,
            interval=config.interval
        )
        candles = candles_factory.get_candle(candles_config)
        return await candles.get_historical_candles(config=config)
    except Exception as e:
        return {"error": str(e)}
