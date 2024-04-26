import asyncio
from datetime import date

import numpy as np
import pandas as pd
from fastapi import APIRouter
from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory, CandlesConfig
from pydantic import BaseModel

router = APIRouter(tags=["Market Data"])
candles_factory = CandlesFactory()

class HistoricalCandlesConfig(BaseModel):
    connector_name: str = "binance_perpetual"
    trading_pair: str = "BTC-USDT"
    interval: str = "3m"
    start_time: int = 1672542000000  # 2023-01-01 00:00:00
    end_time: int = 1672628400000  # 2023-01-01 23:59:00


@router.post("/real-time-candles")
async def get_candles(candles_config: CandlesConfig):
    try:
        candles = candles_factory.get_candle(candles_config)
        candles.start()
        while not candles.is_ready:
            await asyncio.sleep(1)
        df = candles.candles_df
        candles.stop()
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
        all_candles = []
        current_start_time = config.start_time

        while current_start_time <= config.end_time:
            fetched_candles = await candles.fetch_candles(start_time=current_start_time)
            if fetched_candles.size == 0:
                break

            all_candles.append(fetched_candles)
            last_timestamp = fetched_candles[-1][0]  # Assuming the first column is the timestamp
            current_start_time = last_timestamp

        final_candles = np.concatenate(all_candles, axis=0) if all_candles else np.array([])
        candles_df = pd.DataFrame(final_candles, columns=candles.columns)
        candles_df.drop_duplicates(subset=["timestamp"], inplace=True)
        return candles_df
    except Exception as e:
        return {"error": str(e)}
