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
        # TODO: Check if this method works after candles refactor
        # await candles.initialize_exchange_data()
        all_candles = []
        current_end_time = config.end_time + candles.interval_in_seconds
        current_start_time = config.start_time - candles.interval_in_seconds
        candles.max_records = int((current_end_time - current_start_time) / candles.interval_in_seconds)
        while current_end_time >= current_start_time:
            fetched_candles = await candles.fetch_candles(end_time=current_end_time)
            if fetched_candles.size < 1:
                break

            all_candles.append(fetched_candles)
            last_timestamp = candles.ensure_timestamp_in_seconds(
                fetched_candles[0][0])  # Assuming the first column is the timestamp
            current_end_time = last_timestamp - candles.interval_in_seconds
            candles.check_candles_sorted_and_equidistant(all_candles)
        final_candles = np.concatenate(all_candles[::-1], axis=0) if all_candles else np.array([])
        candles_df = pd.DataFrame(final_candles, columns=candles.columns)
        candles_df.drop_duplicates(subset=["timestamp"], inplace=True)
        candles_df = candles_df[
            (candles_df["timestamp"] <= config.end_time) & (candles_df["timestamp"] >= config.start_time)]
        return candles_df
    except Exception as e:
        return {"error": str(e)}