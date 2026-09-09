import asyncio
import logging
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory, UnsupportedConnectorException
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig, HistoricalCandlesConfig

from config import settings
from deps import get_market_data_service
from models import (
    AddTradingPairRequest,
    FundingInfoRequest,
    FundingInfoResponse,
    OrderBookLevel,
    OrderBookQueryResult,
    OrderBookRequest,
    OrderBookResponse,
    PoolPricesResponse,
    PriceForQuoteVolumeRequest,
    PriceForVolumeRequest,
    PriceRequest,
    PricesResponse,
    QuoteVolumeForPriceRequest,
    RateRequest,
    RatesResponse,
    RemoveTradingPairRequest,
    SingleRateResponse,
    TickerInfo,
    TickersResponse,
    TradingPairResponse,
    VolumeForPriceRequest,
    VWAPForVolumeRequest,
)
from models.market_data import CandlesConfigRequest
from services.market_data_service import MarketDataService
from services.ticker_sources import TickerFetchError, TickerUnsupportedError
from services.unified_connector_service import UnknownConnectorError
from utils.trading_pair import split_trading_pair

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Market Data"], prefix="/market-data")


@router.post("/candles")
async def get_candles(request: Request, candles_config: CandlesConfigRequest):
    """
    Get real-time candles data for a specific trading pair.

    This endpoint uses the MarketDataProvider to get or create a candles feed that will
    automatically start and maintain real-time updates. Subsequent requests with the same
    configuration will reuse the existing feed for up-to-date data.

    Args:
        request: FastAPI request object
        candles_config: Configuration for the candles including connector, trading_pair, interval, and max_records

    Returns:
        Real-time candles data or error message
    """
    available = list(CandlesFactory._candles_map.keys())
    if candles_config.connector_name not in CandlesFactory._candles_map:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported connector '{candles_config.connector_name}'. "
                   f"Available connectors: {available}"
        )

    if "-" not in candles_config.trading_pair:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid trading pair format '{candles_config.trading_pair}'. "
                   f"Expected format: BASE-QUOTE (e.g., BTC-USDT)"
        )

    try:
        market_data_service: MarketDataService = request.app.state.market_data_service

        candles_cfg = CandlesConfig(
            connector=candles_config.connector_name, trading_pair=candles_config.trading_pair,
            interval=candles_config.interval, max_records=candles_config.max_records)

        # Creating the feed validates the trading pair on first use (cache hit afterwards);
        # an invalid pair raises ValueError.
        try:
            candles_feed = await market_data_service.get_candles_feed(candles_cfg)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # Wait for the candles feed to be ready with a timeout
        timeout = settings.market_data.candles_ready_timeout
        start = time.time()
        while not candles_feed.ready:
            if time.time() - start > timeout:
                # Clean up the stale feed so it doesn't stay cached
                market_data_service.stop_candle_feed(candles_cfg)
                raise HTTPException(
                    status_code=504,
                    detail=f"Candle feed for {candles_config.connector_name} "
                           f"{candles_config.trading_pair} did not become ready within "
                           f"{timeout}s. The trading pair may not exist on this exchange."
                )
            await asyncio.sleep(0.1)

        df = candles_feed.candles_df

        if df is not None and not df.empty:
            df = df.tail(candles_config.max_records)
            df = df.drop_duplicates(subset=["timestamp"], keep="last")
            return df.to_dict(orient="records")
        else:
            raise HTTPException(status_code=404, detail="No candles data available")

    except HTTPException:
        raise
    except UnsupportedConnectorException as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error fetching candles: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal error fetching candles: {str(e)}")


@router.post("/historical-candles")
async def get_historical_candles(request: Request, config: HistoricalCandlesConfig):
    """
    Get historical candles data for a specific trading pair.

    Args:
        config: Configuration for historical candles including connector, trading pair, interval, start and end time

    Returns:
        Historical candles data or error message
    """
    available = list(CandlesFactory._candles_map.keys())
    if config.connector_name not in CandlesFactory._candles_map:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported connector '{config.connector_name}'. "
                   f"Available connectors: {available}"
        )

    if "-" not in config.trading_pair:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid trading pair format '{config.trading_pair}'. "
                   f"Expected format: BASE-QUOTE (e.g., BTC-USDT)"
        )

    try:
        market_data_service: MarketDataService = request.app.state.market_data_service

        candles_config = CandlesConfig(
            connector=config.connector_name,
            trading_pair=config.trading_pair,
            interval=config.interval
        )

        # Creating the feed validates the trading pair on first use (cache hit afterwards);
        # an invalid pair raises ValueError.
        try:
            candles = await market_data_service.get_candles_feed(candles_config)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        timeout = settings.market_data.candles_ready_timeout
        historical_data = await asyncio.wait_for(
            candles.get_historical_candles(config=config),
            timeout=timeout
        )

        if historical_data is not None and not historical_data.empty:
            return historical_data.to_dict(orient="records")
        else:
            raise HTTPException(status_code=404, detail="No historical data available")

    except HTTPException:
        raise
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"Historical candles request for {config.connector_name} "
                   f"{config.trading_pair} timed out after "
                   f"{settings.market_data.candles_ready_timeout}s. "
                   f"The trading pair may not exist or the time range may be too large."
        )
    except UnsupportedConnectorException as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid parameters: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error fetching historical candles: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal error fetching historical candles: {str(e)}")


@router.get("/active-feeds")
async def get_active_feeds(request: Request):
    """
    Get information about currently active market data feeds.

    Args:
        request: FastAPI request object to access application state

    Returns:
        Dictionary with active feeds information including last access times and expiration
    """
    try:
        market_data_service: MarketDataService = request.app.state.market_data_service
        return market_data_service.get_active_feeds_info()
    except Exception as e:
        return {"error": str(e)}


@router.get("/settings")
async def get_market_data_settings():
    """
    Get current market data settings for debugging.

    Returns:
        Dictionary with current market data configuration including cleanup and timeout settings
    """
    from config import settings
    return {
        "cleanup_interval": settings.market_data.cleanup_interval,
        "feed_timeout": settings.market_data.feed_timeout,
        "description": "cleanup_interval: seconds between cleanup runs, feed_timeout: seconds before unused feeds expire"
    }


@router.get("/available-candle-connectors")
async def get_available_candle_connectors():
    """
    Get list of available connectors that support candle data feeds.

    Returns:
        List of connector names that can be used for fetching candle data
    """
    return list(CandlesFactory._candles_map.keys())


# Enhanced Market Data Endpoints

@router.post("/prices", response_model=PricesResponse)
async def get_prices(
        request: PriceRequest,
        market_data_manager: MarketDataService = Depends(get_market_data_service)
):
    """
    Get current prices for specified trading pairs from a connector.

    Args:
        request: Price request with connector name and trading pairs
        market_data_manager: Injected market data feed manager

    Returns:
        Current prices for the specified trading pairs

    Raises:
        HTTPException: 500 if there's an error fetching prices
    """
    try:
        prices = await market_data_manager.get_prices(
            request.connector_name,
            request.trading_pairs,
            account_name=request.account_name
        )

        if "error" in prices:
            raise HTTPException(status_code=500, detail=prices["error"])

        return PricesResponse(
            connector=request.connector_name,
            prices=prices,
            timestamp=time.time()
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching prices: {str(e)}")


# ==================== Tickers & cross-rates ====================

def _tickers_to_info(tickers) -> dict:
    """Convert {pair: Ticker} to {pair: TickerInfo}."""
    return {pair: TickerInfo(**t.to_dict()) for pair, t in tickers.items()}


def _requested_connectors(connectors: Optional[List[str]]) -> List[str]:
    """Parse the connector filter, accepting both ?connectors=a,b and ?connectors=a&connectors=b."""
    names = []
    for value in connectors or []:
        names.extend(part.strip() for part in value.split(","))
    return list(dict.fromkeys(n for n in names if n))  # de-duplicated, order preserved


def _build_tickers_response(
        tickers_by_connector: dict,
        errors: dict,
        market_data_manager: MarketDataService,
) -> TickersResponse:
    grouped = {name: _tickers_to_info(t) for name, t in tickers_by_connector.items()}
    return TickersResponse(
        tickers=grouped,
        counts={name: len(t) for name, t in grouped.items()},
        updated_at={
            name: market_data_manager.ticker_updated_at(name) for name in grouped
        },
        errors={name: str(exc) for name, exc in errors.items()},
    )


@router.get("/tickers", response_model=TickersResponse)
async def get_tickers(
        connectors: Optional[List[str]] = Query(
            None,
            description="Restrict to these connectors. Accepts a comma-separated list or the "
                        "parameter repeated. Omit to return the whole collected pool."
        ),
        refresh: bool = Query(False, description="Force a fresh fetch, ignoring the cache"),
        max_age: Optional[float] = Query(
            None, ge=0, description="Accept cached tickers up to this age in seconds"
        ),
        market_data_manager: MarketDataService = Depends(get_market_data_service)
):
    """
    Get tickers grouped by connector, with 24h base and quote volume where available.

    Without ``connectors`` this returns the collected pool as-is. Naming connectors fetches
    them on demand (concurrently) through keyless public data connectors when the cache is
    missing or stale, so it works for exchanges no API keys are configured for; those
    connectors then join the background refresh cycle.

    A connector that fails does not remove the others from the response: it is reported under
    ``errors`` alongside the successful results. An error status is returned only when nothing
    could be served at all.
    """
    names = _requested_connectors(connectors)

    # No filter and no refresh: a plain read of the pool, no requests made.
    if not names and not refresh:
        return _build_tickers_response(market_data_manager.get_tickers(), {}, market_data_manager)

    targets = names or market_data_manager.collected_connector_names()
    if not targets:
        return _build_tickers_response({}, {}, market_data_manager)

    try:
        tickers, errors = await market_data_manager.fetch_tickers_for(
            targets, max_age=max_age, force=refresh
        )
    except UnknownConnectorError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # Everything requested failed, so report why instead of an empty, seemingly-fine 200.
    if errors and not tickers:
        detail = "; ".join(f"{name}: {exc}" for name, exc in sorted(errors.items()))
        if all(isinstance(exc, TickerUnsupportedError) for exc in errors.values()):
            # Known connectors that can never serve public tickers; retrying will not help.
            raise HTTPException(status_code=400, detail=detail)
        if all(isinstance(exc, TickerFetchError) for exc in errors.values()):
            raise HTTPException(status_code=502, detail=f"Failed to fetch tickers: {detail}")
        logger.error(f"Unexpected error fetching tickers for {targets}: {detail}")
        raise HTTPException(status_code=500, detail=detail)

    return _build_tickers_response(tickers, errors, market_data_manager)


@router.post("/rates", response_model=RatesResponse)
async def get_rates(
        request: RateRequest,
        market_data_manager: MarketDataService = Depends(get_market_data_service)
):
    """
    Resolve cross-rates for trading pairs from the collected ticker pool.

    Rates are resolved via direct, reverse or bridged paths. When ``connector`` is set, only
    that exchange's tickers are used; otherwise the merged multi-exchange pool is used.
    """
    rates = {}
    for pair in request.trading_pairs:
        if request.connector:
            base, quote = split_trading_pair(pair) if "-" in pair else (pair, None)
            rate = market_data_manager.get_rate_for_connector(request.connector, base, quote) if quote else None
        else:
            rate = market_data_manager.get_pair_rate(pair)
        rates[pair] = float(rate) if rate else None
    return RatesResponse(
        quote_token=market_data_manager.quote_token,
        connector=request.connector,
        rates=rates,
    )


@router.get("/rate/{trading_pair}", response_model=SingleRateResponse)
async def get_single_rate(
        trading_pair: str,
        connector: str = None,
        market_data_manager: MarketDataService = Depends(get_market_data_service)
):
    """
    Resolve a cross-rate for a single ``BASE-QUOTE`` trading pair from the ticker pool.

    Pass ``?connector=<name>`` to restrict resolution to a single exchange's tickers.
    """
    if connector:
        base, quote = split_trading_pair(trading_pair) if "-" in trading_pair else (trading_pair, None)
        rate = market_data_manager.get_rate_for_connector(connector, base, quote) if quote else None
    else:
        rate = market_data_manager.get_pair_rate(trading_pair)
    return SingleRateResponse(
        trading_pair=trading_pair,
        rate=float(rate) if rate else None,
        quote_token=market_data_manager.quote_token,
        connector=connector,
    )


@router.get("/pool-prices", response_model=PoolPricesResponse)
async def get_pool_prices(
        market_data_manager: MarketDataService = Depends(get_market_data_service)
):
    """Get a snapshot of the merged price pool used for cross-rate resolution."""
    prices = {pair: float(price) for pair, price in market_data_manager.prices.items()}
    return PoolPricesResponse(
        quote_token=market_data_manager.quote_token,
        prices_count=len(prices),
        prices=prices,
    )


@router.post("/funding-info", response_model=FundingInfoResponse)
async def get_funding_info(
        request: FundingInfoRequest,
        market_data_manager: MarketDataService = Depends(get_market_data_service)
):
    """
    Get funding information for a perpetual trading pair.

    Args:
        request: Funding info request with connector name and trading pair
        market_data_manager: Injected market data feed manager

    Returns:
        Funding information including rates, timestamps, and prices

    Raises:
        HTTPException: 400 for non-perpetual connectors, 500 for other errors
    """
    try:
        if "_perpetual" not in request.connector_name.lower():
            raise HTTPException(status_code=400, detail="Funding info is only available for perpetual trading pairs.")
        funding_info = await market_data_manager.get_funding_info(
            request.connector_name,
            request.trading_pair,
            account_name=request.account_name
        )

        if "error" in funding_info:
            if "not supported" in funding_info["error"]:
                raise HTTPException(status_code=400, detail=funding_info["error"])
            else:
                raise HTTPException(status_code=500, detail=funding_info["error"])

        return FundingInfoResponse(**funding_info)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching funding info: {str(e)}")


@router.post("/order-book", response_model=OrderBookResponse)
async def get_order_book(
        request: OrderBookRequest,
        market_data_manager: MarketDataService = Depends(get_market_data_service)
):
    """
    Get order book snapshot with specified depth.

    Args:
        request: Order book request with connector, trading pair, and depth
        market_data_manager: Injected market data feed manager

    Returns:
        Order book snapshot with bids and asks

    Raises:
        HTTPException: 500 if there's an error fetching order book
    """
    try:
        order_book_data = await market_data_manager.get_order_book_data(
            request.connector_name,
            request.trading_pair,
            request.depth,
            account_name=request.account_name
        )

        if "error" in order_book_data:
            raise HTTPException(status_code=500, detail=order_book_data["error"])

        # Convert to response format - data comes as [price, amount] lists
        bids = [OrderBookLevel(price=bid[0], amount=bid[1]) for bid in order_book_data["bids"]]
        asks = [OrderBookLevel(price=ask[0], amount=ask[1]) for ask in order_book_data["asks"]]

        return OrderBookResponse(
            trading_pair=order_book_data["trading_pair"],
            bids=bids,
            asks=asks,
            timestamp=order_book_data["timestamp"]
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching order book: {str(e)}")


# Order Book Query Endpoints

@router.post("/order-book/price-for-volume", response_model=OrderBookQueryResult)
async def get_price_for_volume(
        request: PriceForVolumeRequest,
        market_data_manager: MarketDataService = Depends(get_market_data_service)
):
    """
    Get the price required to fill a specific volume on the order book.

    Args:
        request: Request with connector, trading pair, volume, and side
        market_data_manager: Injected market data feed manager

    Returns:
        Order book query result with price and volume information
    """
    try:
        result = await market_data_manager.get_order_book_query_result(
            request.connector_name,
            request.trading_pair,
            request.is_buy,
            volume=request.volume,
            account_name=request.account_name
        )

        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])

        return OrderBookQueryResult(**result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error in order book query: {str(e)}")


@router.post("/order-book/volume-for-price", response_model=OrderBookQueryResult)
async def get_volume_for_price(
        request: VolumeForPriceRequest,
        market_data_manager: MarketDataService = Depends(get_market_data_service)
):
    """
    Get the volume available at a specific price level on the order book.

    Args:
        request: Request with connector, trading pair, price, and side
        market_data_manager: Injected market data feed manager

    Returns:
        Order book query result with volume information
    """
    try:
        result = await market_data_manager.get_order_book_query_result(
            request.connector_name,
            request.trading_pair,
            request.is_buy,
            price=request.price,
            account_name=request.account_name
        )

        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])

        return OrderBookQueryResult(**result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error in order book query: {str(e)}")


@router.post("/order-book/price-for-quote-volume", response_model=OrderBookQueryResult)
async def get_price_for_quote_volume(
        request: PriceForQuoteVolumeRequest,
        market_data_manager: MarketDataService = Depends(get_market_data_service)
):
    """
    Get the price required to fill a specific quote volume on the order book.

    Args:
        request: Request with connector, trading pair, quote volume, and side
        market_data_manager: Injected market data feed manager

    Returns:
        Order book query result with price and volume information
    """
    try:
        result = await market_data_manager.get_order_book_query_result(
            request.connector_name,
            request.trading_pair,
            request.is_buy,
            quote_volume=request.quote_volume,
            account_name=request.account_name
        )

        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])

        return OrderBookQueryResult(**result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error in order book query: {str(e)}")


@router.post("/order-book/quote-volume-for-price", response_model=OrderBookQueryResult)
async def get_quote_volume_for_price(
        request: QuoteVolumeForPriceRequest,
        market_data_manager: MarketDataService = Depends(get_market_data_service)
):
    """
    Get the quote volume available at a specific price level on the order book.

    Args:
        request: Request with connector, trading pair, price, and side
        market_data_manager: Injected market data feed manager

    Returns:
        Order book query result with quote volume information
    """
    try:
        result = await market_data_manager.get_order_book_query_result(
            request.connector_name,
            request.trading_pair,
            request.is_buy,
            quote_price=request.price,
            account_name=request.account_name
        )

        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])

        return OrderBookQueryResult(**result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error in order book query: {str(e)}")


@router.post("/order-book/vwap-for-volume", response_model=OrderBookQueryResult)
async def get_vwap_for_volume(
        request: VWAPForVolumeRequest,
        market_data_manager: MarketDataService = Depends(get_market_data_service)
):
    """
    Get the VWAP (Volume Weighted Average Price) for a specific volume on the order book.

    Args:
        request: Request with connector, trading pair, volume, and side
        market_data_manager: Injected market data feed manager

    Returns:
        Order book query result with VWAP information
    """
    try:
        result = await market_data_manager.get_order_book_query_result(
            request.connector_name,
            request.trading_pair,
            request.is_buy,
            vwap_volume=request.volume,
            account_name=request.account_name
        )

        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])

        return OrderBookQueryResult(**result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error in order book query: {str(e)}")


# Trading Pair Management Endpoints

@router.post("/trading-pair/add", response_model=TradingPairResponse)
async def add_trading_pair(
        request: AddTradingPairRequest,
        market_data_service: MarketDataService = Depends(get_market_data_service)
):
    """
    Initialize order book for a trading pair.

    This endpoint dynamically adds a trading pair to a connector's order book tracker.
    It uses the best available connector (trading connectors are preferred over data connectors).

    Args:
        request: Request with connector name, trading pair, optional account name, and timeout

    Returns:
        TradingPairResponse with success status and message

    Raises:
        HTTPException: 500 if initialization fails
    """
    try:
        success = await market_data_service.initialize_order_book(
            connector_name=request.connector_name,
            trading_pair=request.trading_pair,
            account_name=request.account_name,
            timeout=request.timeout
        )

        if success:
            return TradingPairResponse(
                success=True,
                connector_name=request.connector_name,
                trading_pair=request.trading_pair,
                message=f"Order book initialized for {request.trading_pair}"
            )
        else:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to initialize order book for {request.trading_pair}"
            )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error initializing order book: {str(e)}"
        )


@router.post("/trading-pair/remove", response_model=TradingPairResponse)
async def remove_trading_pair(
        request: RemoveTradingPairRequest,
        market_data_service: MarketDataService = Depends(get_market_data_service)
):
    """
    Remove a trading pair from order book tracking.

    This endpoint removes a trading pair from a connector's order book tracker,
    cleaning up resources for pairs that are no longer needed.

    Args:
        request: Request with connector name, trading pair, and optional account name

    Returns:
        TradingPairResponse with success status and message

    Raises:
        HTTPException: 500 if removal fails
    """
    try:
        success = await market_data_service.remove_trading_pair(
            connector_name=request.connector_name,
            trading_pair=request.trading_pair,
            account_name=request.account_name
        )

        if success:
            return TradingPairResponse(
                success=True,
                connector_name=request.connector_name,
                trading_pair=request.trading_pair,
                message=f"Trading pair {request.trading_pair} removed"
            )
        else:
            return TradingPairResponse(
                success=False,
                connector_name=request.connector_name,
                trading_pair=request.trading_pair,
                message=f"Trading pair {request.trading_pair} not found or already removed"
            )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error removing trading pair: {str(e)}"
        )


# Order Book Tracker Diagnostics Endpoints

@router.get("/order-book/diagnostics/{connector_name}")
async def get_order_book_diagnostics(
        connector_name: str,
        account_name: str = None,
        market_data_service: MarketDataService = Depends(get_market_data_service)
):
    """
    Get diagnostics for a connector's order book tracker.

    Returns detailed information about the order book tracker status including:
    - Task status (running/crashed)
    - WebSocket connection status
    - Metrics (messages processed, latency, etc.)
    - Current order book state

    Args:
        connector_name: The connector to diagnose (e.g., "binance")
        account_name: Optional account name for trading connectors

    Returns:
        Diagnostic information dictionary
    """
    try:
        diagnostics = market_data_service.get_order_book_tracker_diagnostics(
            connector_name=connector_name,
            account_name=account_name
        )
        return diagnostics
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error getting diagnostics: {str(e)}"
        )


@router.post("/order-book/restart/{connector_name}")
async def restart_order_book_tracker(
        connector_name: str,
        account_name: str = None,
        market_data_service: MarketDataService = Depends(get_market_data_service)
):
    """
    Restart the order book tracker for a connector.

    Use this endpoint when the order book is stale (WebSocket disconnected).
    This will:
    1. Stop the existing order book tracker
    2. Restart it with the same trading pairs
    3. Wait for the WebSocket to reconnect

    Args:
        connector_name: The connector to restart (e.g., "binance")
        account_name: Optional account name for trading connectors

    Returns:
        Restart status with success/failure and trading pairs
    """
    try:
        result = await market_data_service.restart_order_book_tracker(
            connector_name=connector_name,
            account_name=account_name
        )
        return result
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error restarting order book tracker: {str(e)}"
        )
