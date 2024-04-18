import asyncio

from fastapi import APIRouter
from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory, CandlesConfig

router = APIRouter(tags=["Market Data"])
candles_factory = CandlesFactory()


@router.post("/candles")
async def get_candles(candles_config: CandlesConfig):
    candles = candles_factory.get_candle(candles_config)
    candles.start()
    while not candles.is_ready:
        await asyncio.sleep(1)
    df = candles.candles_df
    candles.stop()
    return df
