from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CandleData(BaseModel):
    """Single candle data point"""
    timestamp: datetime = Field(description="Candle timestamp")
    open: float = Field(description="Opening price")
    high: float = Field(description="Highest price")
    low: float = Field(description="Lowest price")
    close: float = Field(description="Closing price")
    volume: float = Field(description="Trading volume")

class CandlesConfigRequest(BaseModel):
    """
    The CandlesConfig class is a data class that stores the configuration of a Candle object.
    It has the following attributes:
    - connector: str
    - trading_pair: str
    - interval: str
    - max_records: int
    """
    connector_name: str
    trading_pair: str
    interval: str = "1m"
    max_records: int = 500

class CandlesResponse(BaseModel):
    """Response for candles data"""
    candles: List[CandleData] = Field(description="List of candle data")


class ActiveFeedInfo(BaseModel):
    """Information about an active market data feed"""
    connector: str = Field(description="Connector name")
    trading_pair: str = Field(description="Trading pair")
    interval: str = Field(description="Candle interval")
    last_access: datetime = Field(description="Last access time")
    expires_at: datetime = Field(description="Expiration time")


class ActiveFeedsResponse(BaseModel):
    """Response for active market data feeds"""
    feeds: List[ActiveFeedInfo] = Field(description="List of active feeds")


class MarketDataSettings(BaseModel):
    """Market data configuration settings"""
    cleanup_interval: int = Field(description="Cleanup interval in seconds")
    feed_timeout: int = Field(description="Feed timeout in seconds")
    description: str = Field(description="Settings description")


class TradingRulesResponse(BaseModel):
    """Response for trading rules"""
    trading_pairs: Dict[str, Dict[str, Any]] = Field(description="Trading rules by pair")


class SupportedOrderTypesResponse(BaseModel):
    """Response for supported order types"""
    connector: str = Field(description="Connector name")
    supported_order_types: List[str] = Field(description="List of supported order types")


# New models for enhanced market data functionality

class PriceRequest(BaseModel):
    """Request model for getting prices"""
    connector_name: str = Field(description="Name of the connector")
    trading_pairs: List[str] = Field(description="List of trading pairs to get prices for")
    account_name: Optional[str] = Field(default=None, description="Optional account name for trading connector preference")


class PriceData(BaseModel):
    """Price data for a trading pair"""
    trading_pair: str = Field(description="Trading pair")
    price: float = Field(description="Current price")
    timestamp: float = Field(description="Price timestamp")


class PricesResponse(BaseModel):
    """Response for prices data"""
    connector: str = Field(description="Connector name")
    prices: Dict[str, float] = Field(description="Trading pair to price mapping")
    timestamp: float = Field(description="Response timestamp")


class FundingInfoRequest(BaseModel):
    """Request model for getting funding info"""
    connector_name: str = Field(description="Name of the connector")
    trading_pair: str = Field(description="Trading pair to get funding info for")
    account_name: Optional[str] = Field(default=None, description="Optional account name for trading connector preference")


class FundingInfoResponse(BaseModel):
    """Response for funding info"""
    trading_pair: str = Field(description="Trading pair")
    funding_rate: Optional[float] = Field(description="Current funding rate")
    next_funding_time: Optional[float] = Field(description="Next funding time timestamp")
    mark_price: Optional[float] = Field(description="Mark price")
    index_price: Optional[float] = Field(description="Index price")


class OrderBookRequest(BaseModel):
    """Request model for getting order book data"""
    connector_name: str = Field(description="Name of the connector")
    trading_pair: str = Field(description="Trading pair")
    depth: int = Field(default=10, ge=1, le=1000, description="Number of price levels to return")
    account_name: Optional[str] = Field(default=None, description="Optional account name for trading connector preference")


class OrderBookLevel(BaseModel):
    """Single order book level"""
    price: float = Field(description="Price level")
    amount: float = Field(description="Amount at this price level")


class OrderBookResponse(BaseModel):
    """Response for order book data"""
    trading_pair: str = Field(description="Trading pair")
    bids: List[OrderBookLevel] = Field(description="Bid levels (highest to lowest)")
    asks: List[OrderBookLevel] = Field(description="Ask levels (lowest to highest)")
    timestamp: float = Field(description="Snapshot timestamp")


class OrderBookQueryRequest(BaseModel):
    """Request model for order book queries"""
    connector_name: str = Field(description="Name of the connector")
    trading_pair: str = Field(description="Trading pair")
    is_buy: bool = Field(description="True for buy side, False for sell side")
    account_name: Optional[str] = Field(default=None, description="Optional account name for trading connector preference")


class VolumeForPriceRequest(OrderBookQueryRequest):
    """Request model for getting volume at a specific price"""
    price: float = Field(description="Price to query volume for")


class PriceForVolumeRequest(OrderBookQueryRequest):
    """Request model for getting price for a specific volume"""
    volume: float = Field(description="Volume to query price for")


class QuoteVolumeForPriceRequest(OrderBookQueryRequest):
    """Request model for getting quote volume at a specific price"""
    price: float = Field(description="Price to query quote volume for")


class PriceForQuoteVolumeRequest(OrderBookQueryRequest):
    """Request model for getting price for a specific quote volume"""
    quote_volume: float = Field(description="Quote volume to query price for")


class VWAPForVolumeRequest(OrderBookQueryRequest):
    """Request model for getting VWAP for a specific volume"""
    volume: float = Field(description="Volume to calculate VWAP for")


class OrderBookQueryResult(BaseModel):
    """Response for order book query operations"""
    trading_pair: str = Field(description="Trading pair")
    is_buy: bool = Field(description="Query side (buy/sell)")
    query_volume: Optional[float] = Field(default=None, description="Queried volume")
    query_price: Optional[float] = Field(default=None, description="Queried price")
    result_price: Optional[float] = Field(default=None, description="Resulting price")
    result_volume: Optional[float] = Field(default=None, description="Resulting volume")
    result_quote_volume: Optional[float] = Field(default=None, description="Resulting quote volume")
    average_price: Optional[float] = Field(default=None, description="Average/VWAP price")
    timestamp: float = Field(description="Query timestamp")


# Trading Pair Management Models

class AddTradingPairRequest(BaseModel):
    """Request model for adding a trading pair to order book tracking"""
    connector_name: str = Field(description="Name of the connector (e.g., 'binance', 'binance_perpetual')")
    trading_pair: str = Field(description="Trading pair to add (e.g., 'BTC-USDT')")
    account_name: Optional[str] = Field(default=None, description="Optional account name for trading connector preference")
    timeout: float = Field(default=30.0, ge=1.0, le=120.0, description="Timeout in seconds for order book initialization")


class RemoveTradingPairRequest(BaseModel):
    """Request model for removing a trading pair from order book tracking"""
    connector_name: str = Field(description="Name of the connector")
    trading_pair: str = Field(description="Trading pair to remove")
    account_name: Optional[str] = Field(default=None, description="Optional account name for trading connector preference")


class TradingPairResponse(BaseModel):
    """Response model for trading pair management operations"""
    success: bool = Field(description="Whether the operation succeeded")
    connector_name: str = Field(description="Name of the connector")
    trading_pair: str = Field(description="Trading pair that was added/removed")
    message: str = Field(description="Status message")


# Ticker & cross-rate models (in-house ticker pool, replaces the rate oracle endpoints)

class TickerInfo(BaseModel):
    """A single collected ticker."""
    price: float = Field(description="Mid price (or last price when bid/ask unavailable)")
    base_volume: Optional[float] = Field(
        default=None, description="24h volume denominated in the base asset"
    )
    quote_volume: Optional[float] = Field(
        default=None, description="24h volume denominated in the quote asset"
    )
    timestamp: float = Field(description="Collection timestamp")


class TickersResponse(BaseModel):
    """Tickers grouped by connector."""
    tickers: Dict[str, Dict[str, TickerInfo]] = Field(
        description="Connector to {trading pair: ticker} mapping"
    )
    counts: Dict[str, int] = Field(
        default_factory=dict, description="Number of tickers per connector"
    )
    updated_at: Dict[str, Optional[float]] = Field(
        default_factory=dict,
        description="Unix timestamp of the last successful fetch, per connector"
    )
    errors: Dict[str, str] = Field(
        default_factory=dict,
        description="Connectors that could not be fetched, with the reason. Present only when "
                    "some requested connectors succeeded and others failed"
    )


class RateRequest(BaseModel):
    """Request for cross-rates from the ticker pool."""
    trading_pairs: List[str] = Field(
        description="Trading pairs to price (e.g., ['BTC-USDT', 'ETH-USDT'])"
    )
    connector: Optional[str] = Field(
        default=None,
        description="If set, resolve rates using only this connector's tickers"
    )


class RatesResponse(BaseModel):
    """Cross-rates for the requested trading pairs."""
    quote_token: str = Field(description="Configured global quote token")
    connector: Optional[str] = Field(default=None, description="Connector used, if scoped")
    rates: Dict[str, Optional[float]] = Field(
        description="Trading pair to rate mapping (None if not resolvable)"
    )


class SingleRateResponse(BaseModel):
    """Cross-rate for a single trading pair."""
    trading_pair: str = Field(description="The trading pair")
    rate: Optional[float] = Field(description="The resolved rate (None if not found)")
    quote_token: str = Field(description="Configured global quote token")
    connector: Optional[str] = Field(default=None, description="Connector used, if scoped")


class PoolPricesResponse(BaseModel):
    """Snapshot of the merged price pool used for cross-rate resolution."""
    quote_token: str = Field(description="Configured global quote token")
    prices_count: int = Field(description="Number of prices in the pool")
    prices: Dict[str, float] = Field(description="Trading pair to price mapping")