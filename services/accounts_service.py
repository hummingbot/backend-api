import asyncio
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, TradeType

from config import settings
from database import AccountRepository, AsyncDatabaseManager
from services.gateway_client import GatewayClient
from services.gateway_transaction_poller import GatewayTransactionPoller
from services.gateway_wallet_service import GatewayWalletService, balance_entry
from services.perpetual_trading_service import PerpetualTradingService
from services.portfolio_analytics_service import PortfolioAnalyticsService
from utils.file_system import fs_util
from utils.gateway_certs import build_client_ssl_context

# Create module-specific logger
logger = logging.getLogger(__name__)

# Safe single path component names: prevents path traversal via '/', '\' or '..'
SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_safe_name(name: str, label: str = "name") -> str:
    """
    Validate that a name is safe to use as a single path component (no separators or traversal sequences).
    :param name: The name to validate.
    :param label: Human readable label used in the error message.
    :return: The validated name.
    :raises HTTPException: 400 if the name is invalid.
    """
    if not name or not SAFE_NAME_PATTERN.fullmatch(name):
        raise HTTPException(status_code=400,
                            detail=f"Invalid {label}: '{name}'. Only letters, numbers, underscores and hyphens are allowed.")
    return name


class AccountsService:
    """
    This class is responsible for managing all the accounts that are connected to the trading system. It is responsible
    to initialize all the connectors that are connected to each account, keep track of the balances of each account and
    update the balances of each account.
    """
    default_quotes = {
        "hyperliquid": "USDC",
        "hyperliquid_perpetual": "USD",
        "lighter": "USDC",
        "lighter_perpetual": "USDC",
        "xrpl": "RLUSD",
        "kraken": "USD",
        "backpack": "USDC",
        "backpack_perpetual": "USDC",
        "cube": "USDC",
        "derive": "USDC",
        "derive_options": "USDC",
        "derive_perpetual": "USDC",
        "dexalot": "USDC",
        "vertex": "USDC",
        "aevo_perpetual": "USDC",
        "pacifica_perpetual": "USDC",
        "dydx_v4_perpetual": "USD",
        "decibel_perpetual": "USD",
        "architect_perpetual": "USD",
    }
    potential_wrapped_tokens = ["ETH", "SOL", "BNB", "POL", "AVAX"]

    def __init__(self,
                 db_manager: AsyncDatabaseManager,
                 connector_service,
                 market_data_service,
                 trading_service,
                 account_update_interval: int = 5,
                 default_quote: str = "USDT",
                 gateway_url: str = "http://localhost:15888"):
        """
        Initialize the AccountsService.

        Args:
            db_manager: AsyncDatabaseManager for persistence (shared, created once at startup)
            connector_service: UnifiedConnectorService (required, injected from main.py)
            market_data_service: MarketDataService (required, injected from main.py)
            trading_service: TradingService (required, injected from main.py)
            account_update_interval: How often to update account states in minutes (default: 5)
            default_quote: Default quote currency for trading pairs (default: "USDT")
            gateway_url: URL for Gateway service (default: "http://localhost:15888")
        """
        self.secrets_manager = ETHKeyFileSecretManger(settings.security.config_password)
        self.accounts_state = {}
        self.update_account_state_interval = account_update_interval * 60
        self.order_status_poll_interval = 60  # Poll order status every 1 minute
        self.default_quote = default_quote
        self._update_account_state_task: Optional[asyncio.Task] = None
        self._order_status_polling_task: Optional[asyncio.Task] = None

        # Cache for storing last successful prices by trading pair (per-instance)
        self._last_known_prices = {}

        # Database setup for account states and orders (shared manager injected from main.py;
        # tables are created once at startup so no per-service bootstrap is needed)
        self.db_manager = db_manager

        # Services injected from main.py (required). Set BEFORE any composed service below
        # uses them: perpetual_trading_service binds self.get_connector_instance, which relies
        # on _connector_service being available.
        self._connector_service = connector_service  # UnifiedConnectorService
        self._market_data_service = market_data_service  # MarketDataService
        self._trading_service = trading_service  # TradingService

        # Initialize Gateway client. For a secured (https) Gateway, present the shared client
        # cert over mTLS; certs are decrypted with CONFIG_PASSWORD (SEC-048). The SSL context is
        # built lazily so certs generated once the Gateway is started are picked up without an
        # API restart.
        self.gateway_base_url = gateway_url
        ssl_context_factory = None
        if gateway_url.lower().startswith("https://"):
            ssl_context_factory = lambda: build_client_ssl_context(settings.security.config_password)  # noqa: E731
        self.gateway_client = GatewayClient(gateway_url, ssl_context_factory=ssl_context_factory)

        # Composed services: gateway wallet CRUD/balances, perpetual trading and pure portfolio analytics
        self.gateway_wallet_service = GatewayWalletService(self.gateway_client, market_data_service)
        self.perpetual_trading_service = PerpetualTradingService(self.get_connector_instance)
        self.portfolio_analytics_service = PortfolioAnalyticsService()

        # Initialize Gateway transaction poller
        self.gateway_tx_poller = GatewayTransactionPoller(
            db_manager=self.db_manager,
            gateway_client=self.gateway_client,
            poll_interval=10,  # Poll every 10 seconds for transactions
            position_poll_interval=60,  # Poll every 1 minute for positions
            max_retry_age=3600  # Stop retrying after 1 hour
        )
        self._gateway_poller_started = False

    def get_accounts_state(self):
        return self.accounts_state

    def _market_components(self, token: str, connector_name: str) -> tuple[str, str]:
        """Resolve the (base, quote) used to price a token on a connector.

        Unwraps binance-earn staked tokens (LD-prefix) and picks the connector's native quote
        (e.g. USDC on hyperliquid, USD on hyperliquid_perpetual), falling back to the global
        default quote. This is the single source of truth for both ticker-pool lookups and the
        exchange fallback fetch, so they always agree on base and quote.
        """
        if token.startswith("LD") and token != "LDO":
            # These tokens are staked in binance earn
            token = token[2:]
        quote = self.default_quotes.get(connector_name, self.default_quote)
        return token, quote

    def get_default_market(self, token: str, connector_name: str) -> str:
        base, quote = self._market_components(token, connector_name)
        return f"{base}-{quote}"

    def start(self):
        """
        Start the loop that updates the account state at a fixed interval.
        Note: Balance updates are now handled by manual connector state updates.
        :return:
        """
        # Start the update loop which will call check_all_connectors
        self._update_account_state_task = asyncio.create_task(self.update_account_state_loop())

        # Start order status polling loop (every 1 minute)
        self._order_status_polling_task = asyncio.create_task(self.order_status_polling_loop())
        logger.info("Order status polling started (1 minute interval)")

        # Start Gateway transaction poller
        if not self._gateway_poller_started:
            asyncio.create_task(self._start_gateway_poller())
            self._gateway_poller_started = True
            logger.info("Gateway transaction poller startup initiated")

    async def _start_gateway_poller(self):
        """Start the Gateway transaction poller (async helper)."""
        try:
            await self.gateway_tx_poller.start()
            logger.info("Gateway transaction poller started successfully")
        except Exception as e:
            logger.error(f"Error starting Gateway transaction poller: {e}", exc_info=True)

    async def stop(self):
        """
        Stop all accounts service tasks and cleanup resources.
        This is the main cleanup method that should be called during application shutdown.
        """
        logger.info("Stopping AccountsService...")

        # Stop the account state update loop
        if self._update_account_state_task:
            self._update_account_state_task.cancel()
            self._update_account_state_task = None
            logger.info("Stopped account state update loop")

        # Stop the order status polling loop
        if self._order_status_polling_task:
            self._order_status_polling_task.cancel()
            self._order_status_polling_task = None
            logger.info("Stopped order status polling loop")

        # Stop Gateway transaction poller
        if self._gateway_poller_started:
            try:
                await self.gateway_tx_poller.stop()
                logger.info("Gateway transaction poller stopped")
                self._gateway_poller_started = False
            except Exception as e:
                logger.error(f"Error stopping Gateway transaction poller: {e}", exc_info=True)

        # Close the GeckoTerminal price source HTTP client
        try:
            await self.gateway_wallet_service.close()
        except Exception as e:
            logger.error(f"Error closing Gateway wallet service: {e}", exc_info=True)

        # Stop all connectors through the connector service
        await self._connector_service.stop_all()

        logger.info("AccountsService stopped successfully")

    async def _refresh_and_get_tokens_info(self, connector, connector_name: str, account_name: str) -> List[Dict]:
        """Refresh connector state from exchange, then get token info with prices.

        Combines the connector state refresh and token info retrieval into a
        single awaitable so both can run in parallel across all connectors.
        """
        try:
            await self._connector_service.refresh_connector_state(connector, connector_name, account_name)
        except Exception as e:
            logger.error(f"Error refreshing {connector_name}, using stale data: {e}")
        # skip_balance_refresh=True since refresh_connector_state already called _update_balances
        return await self._get_connector_tokens_info(connector, connector_name, skip_balance_refresh=True)

    async def update_account_state_loop(self):
        """
        The loop that updates the account state at a fixed interval.
        Performs connector state refresh + token info retrieval in a single parallel pass.
        """
        while True:
            try:
                await self.check_all_connectors()

                # Single parallel pass: refresh connector state + get token info + gateway
                all_connectors = self._connector_service.get_all_trading_connectors()
                tasks = []
                task_meta = []  # (account_name, connector_name)

                gateway_meta = []  # (account_name, connector_name) of Gateway connectors

                for account_name, connectors in all_connectors.items():
                    if account_name not in self.accounts_state:
                        self.accounts_state[account_name] = {}
                    for connector_name, connector in connectors.items():
                        # Gateway connectors only track the tokens of their trading pairs; the
                        # full wallet balance is fetched by _update_gateway_balances below.
                        if self._connector_service.is_gateway_connector(connector):
                            gateway_meta.append((account_name, connector_name))
                            continue
                        tasks.append(self._refresh_and_get_tokens_info(connector, connector_name, account_name))
                        task_meta.append((account_name, connector_name))

                has_connector_tasks = len(tasks) > 0
                tasks.append(self._update_gateway_balances())
                results = await asyncio.gather(*tasks, return_exceptions=True)

                # Process connector results (last result is always gateway)
                connector_results = results[:-1] if has_connector_tasks else []
                for (account_name, connector_name), result in zip(task_meta, connector_results):
                    if isinstance(result, Exception):
                        logger.error(f"Error updating {connector_name} in {account_name}: {result}")
                        self.accounts_state[account_name][connector_name] = []
                    else:
                        self.accounts_state[account_name][connector_name] = result

                gw_result = results[-1]
                if isinstance(gw_result, Exception):
                    logger.error(f"Error updating gateway balances: {gw_result}")

                self._mirror_gateway_state_to_accounts(gateway_meta)

                await self.dump_account_state()
            except Exception as e:
                logger.error(f"Error updating account state: {e}")
            finally:
                await asyncio.sleep(self.update_account_state_interval)

    async def order_status_polling_loop(self):
        """
        Sync order state to database for all connectors at a frequent interval (1 minute).

        The connector's built-in _lost_orders_update_polling_loop already polls the exchange.
        This loop just syncs that state to our database and cleans up closed orders.
        """
        while True:
            try:
                await self._connector_service.sync_all_orders_to_database()
            except Exception as e:
                logger.error(f"Error syncing order state to database: {e}")
            finally:
                await asyncio.sleep(self.order_status_poll_interval)

    async def dump_account_state(self):
        """
        Save the current account state to the database.
        All account/connector combinations from the same snapshot will use the same timestamp.
        The whole snapshot is persisted atomically in a single transaction: save_account_state
        only flushes, and get_session_context commits once on successful exit.
        :return:
        """
        # Snapshot the live dict synchronously (no awaits) so concurrent mutations of
        # accounts_state cannot raise "dictionary changed size during iteration"
        accounts_state_snapshot = {account: dict(connectors) for account, connectors in self.accounts_state.items()}

        try:
            # Generate a single timestamp for this entire snapshot
            snapshot_timestamp = datetime.now(timezone.utc)

            async with self.db_manager.get_session_context() as session:
                repository = AccountRepository(session)

                # Save each account-connector combination with the same timestamp.
                # No commit happens inside the loop; the session context commits once
                # after all rows are added (one transaction per snapshot).
                for account_name, connectors in accounts_state_snapshot.items():
                    for connector_name, tokens_info in connectors.items():
                        if tokens_info:  # Only save if there's token data
                            await repository.save_account_state(account_name, connector_name, tokens_info, snapshot_timestamp)

        except Exception as e:
            logger.error(f"Error saving account state to database: {e}")
            # Re-raise the exception since we no longer have a fallback
            raise

    async def load_account_state_history(self,
                                        limit: Optional[int] = None,
                                        cursor: Optional[str] = None,
                                        start_time: Optional[datetime] = None,
                                        end_time: Optional[datetime] = None,
                                        interval: str = "5m",
                                        account_names: Optional[List[str]] = None):
        """
        Load the account state history from the database with pagination and interval sampling.

        Args:
            limit: Maximum number of records to return
            cursor: Cursor for pagination
            start_time: Start time filter
            end_time: End time filter
            interval: Sampling interval (5m, 15m, 30m, 1h, 4h, 12h, 1d)
            account_names: Optional list of account names to filter by (single IN query)

        :return: Tuple of (data, next_cursor, has_more).
        """
        try:
            async with self.db_manager.get_session_context() as session:
                repository = AccountRepository(session)
                return await repository.get_account_state_history(
                    limit=limit,
                    account_names=account_names,
                    cursor=cursor,
                    start_time=start_time,
                    end_time=end_time,
                    interval=interval
                )
        except Exception as e:
            logger.error(f"Error loading account state history from database: {e}")
            # Return empty result since we no longer have a fallback
            return [], None, False

    async def check_all_connectors(self):
        """
        Check all available credentials for all accounts and ensure connectors are initialized.
        This method is idempotent - it only initializes missing connectors.
        """
        for account_name in self.list_accounts():
            await self._ensure_account_connectors_initialized(account_name)

    async def _ensure_account_connectors_initialized(self, account_name: str):
        """
        Ensure all connectors for a specific account are initialized.
        This delegates to the connector service for actual initialization.

        :param account_name: The name of the account to initialize connectors for.
        """
        # Initialize missing connectors
        for connector_name in self._connector_service.list_available_credentials(account_name):
            try:
                # Only initialize if connector doesn't exist
                if not self._connector_service.is_trading_connector_initialized(account_name, connector_name):
                    # Get connector will now handle all initialization
                    await self._connector_service.get_trading_connector(account_name, connector_name)
            except Exception as e:
                logger.error(f"Error initializing connector {connector_name} for account {account_name}: {e}")

    async def update_account_state(
        self,
        skip_gateway: bool = False,
        account_names: Optional[List[str]] = None,
        connector_names: Optional[List[str]] = None
    ):
        """Update account state for filtered connectors and optionally Gateway wallets.

        Args:
            skip_gateway: If True, skip Gateway wallet balance updates for faster CEX-only queries.
            account_names: If provided, only update these accounts. If None, update all accounts.
            connector_names: If provided, only update these connectors. If None, update all connectors.
                            For Gateway, this filters by chain-network (e.g., 'solana-mainnet-beta').
        """
        all_connectors = self._connector_service.get_all_trading_connectors()

        # Prepare parallel tasks
        tasks = []
        task_meta = []  # (account_name, connector_name)
        gateway_meta = []  # (account_name, connector_name) of Gateway connectors

        for account_name, connectors in all_connectors.items():
            # Filter by account_names if specified
            if account_names and account_name not in account_names:
                continue

            if account_name not in self.accounts_state:
                self.accounts_state[account_name] = {}
            for connector_name, connector in connectors.items():
                # Filter by connector_names if specified
                if connector_names and connector_name not in connector_names:
                    continue

                # Gateway connectors only track the tokens of their trading pairs; the full
                # wallet balance is fetched by _update_gateway_balances below. Running both
                # would overwrite the complete wallet state with the narrower connector view.
                if self._connector_service.is_gateway_connector(connector):
                    gateway_meta.append((account_name, connector_name))
                    continue

                tasks.append(self._get_connector_tokens_info(connector, connector_name))
                task_meta.append((account_name, connector_name))

        # Execute connectors + gateway in parallel (unless skip_gateway is True)
        if skip_gateway:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        else:
            # Pass connector_names filter to gateway for chain-network filtering
            results = await asyncio.gather(
                *tasks,
                self._update_gateway_balances(chain_networks=connector_names),
                return_exceptions=True
            )
            # Remove gateway result from processing (it handles its own state internally)
            results = results[:-1]

        # Process results
        for (account_name, connector_name), result in zip(task_meta, results):
            if isinstance(result, Exception):
                logger.error(f"Error updating balances for connector {connector_name} in account {account_name}: {result}")
                self.accounts_state[account_name][connector_name] = []
            else:
                self.accounts_state[account_name][connector_name] = result

        if not skip_gateway:
            self._mirror_gateway_state_to_accounts(gateway_meta)

    def _mirror_gateway_state_to_accounts(self, gateway_meta: List[tuple]):
        """Mirror Gateway wallet balances onto non-master accounts holding the same connector.

        _update_gateway_balances stores the full wallet balances under master_account. A
        Gateway connector configured on another account trades the same Gateway default
        wallet, so it reports the same balances.

        Args:
            gateway_meta: (account_name, connector_name) pairs of Gateway connectors.
        """
        master_state = self.accounts_state.get("master_account", {})
        for account_name, connector_name in gateway_meta:
            if account_name == "master_account":
                continue
            if connector_name in master_state:
                self.accounts_state[account_name][connector_name] = master_state[connector_name]

    async def _get_connector_tokens_info(self, connector, connector_name: str, skip_balance_refresh: bool = False) -> List[Dict]:
        """Get token info from a connector instance using the ticker pool prices.

        Fetches fresh balances from the exchange, then tries the ticker pool (instant, in-memory
        cross-rate resolution) first for each token price. Only falls back to a batch exchange
        call for tokens the pool can't price.

        Args:
            connector: The connector instance
            connector_name: Name of the connector
            skip_balance_refresh: If True, skip fetching fresh balances (use when caller already refreshed)
        """
        # Fetch fresh balances from the exchange unless caller already did
        if not skip_balance_refresh and hasattr(connector, '_update_balances'):
            try:
                await connector._update_balances()
            except Exception as e:
                logger.warning(f"Failed to refresh balances for {connector_name}, using cached data: {e}")

        balances = [{"token": key, "units": value} for key, value in connector.get_all_balances().items() if
                    value != Decimal("0") and key not in settings.banned_tokens]

        tokens_info = []
        missing_pairs = []  # trading pairs the oracle can't price
        missing_indices = []  # indices into tokens_info that need patching

        for balance in balances:
            token = balance["token"]
            if "USD" in token and "-" not in token:
                price = Decimal("1")
            else:
                # Check if connector provides custom token price (e.g. option contract mark price)
                custom_price = None
                if hasattr(connector, "get_token_price"):
                    try:
                        raw_price = connector.get_token_price(token)
                        if raw_price is not None:
                            custom_price = Decimal(str(raw_price))
                    except Exception:
                        custom_price = None

                if custom_price is not None and custom_price > Decimal("0"):
                    price = custom_price
                else:
                    # Price using THIS connector's own tickers and its native quote (instant,
                    # in-memory cross-rate resolution). Using the connector's own quote avoids
                    # mismatches on exchanges that don't list the global quote (e.g. hyperliquid
                    # quotes in USDC/USD, not USDT).
                    base, quote = self._market_components(token, connector_name)
                    rate = self._market_data_service.get_rate_for_connector(connector_name, base, quote)
                    if rate and rate > 0:
                        price = rate
                    else:
                        # Queue for fallback batch fetch from exchange
                        market = f"{base}-{quote}"
                        missing_pairs.append(market)
                        missing_indices.append(len(tokens_info))
                        price = None  # resolved below

            tokens_info.append(balance_entry(
                token,
                balance["units"],
                price,
                available_units=connector.get_available_balance(token),
            ))

        # Batch-fetch only the missing prices from the exchange
        if missing_pairs:
            fallback_prices = await self._safe_get_last_traded_prices(connector, missing_pairs)
            for pair_idx, info_idx in enumerate(missing_indices):
                market = missing_pairs[pair_idx]
                price = Decimal(str(fallback_prices.get(market, 0)))
                tokens_info[info_idx]["price"] = float(price)
                tokens_info[info_idx]["value"] = float(price * Decimal(str(tokens_info[info_idx]["units"])))

        return tokens_info
    
    async def _safe_get_last_traded_prices(self, connector, trading_pairs, timeout=10):
        """Safely get last traded prices with timeout and error handling.
        Fetches each pair individually via gather so one bad pair doesn't kill the rest."""

        async def _fetch_single(pair):
            return pair, await connector._get_last_traded_price(trading_pair=pair)

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*[_fetch_single(p) for p in trading_pairs], return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.error(f"Timeout getting last traded prices for trading pairs {trading_pairs}")
            return self._get_fallback_prices(trading_pairs)

        last_traded = {}
        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"Failed to get price for a pair: {result}")
                continue
            pair, price = result
            price_dec = None
            if price is not None:
                try:
                    price_dec = Decimal(str(price))
                except Exception:
                    pass
            if price_dec is not None and price_dec > 0:
                self._last_known_prices[pair] = price_dec
                last_traded[pair] = price_dec

        # Fill in fallbacks for any pairs that failed
        missing_pairs = [pair for pair in trading_pairs if pair not in last_traded]
        last_traded.update(self._get_fallback_prices(missing_pairs))

        return last_traded
    
    def _get_fallback_prices(self, trading_pairs):
        """Get fallback prices using cached values, only setting to 0 if no previous price exists."""
        fallback_prices = {}
        for pair in trading_pairs:
            if pair in self._last_known_prices:
                fallback_prices[pair] = self._last_known_prices[pair]
                logger.info(f"Using cached price {self._last_known_prices[pair]} for {pair}")
            else:
                fallback_prices[pair] = Decimal("0")
                logger.warning(f"No cached price available for {pair}, using 0")
        return fallback_prices

    def get_connector_config_map(self, connector_name: str):
        """
        Get the connector config map for the specified connector.
        :param connector_name: The name of the connector.
        :return: The connector config map.
        """
        from services.unified_connector_service import UnifiedConnectorService
        return UnifiedConnectorService.get_connector_config_map(connector_name)

    async def add_credentials(self, account_name: str, connector_name: str, credentials: dict):
        """
        Add or update connector credentials and initialize the connector with validation.

        :param account_name: The name of the account.
        :param connector_name: The name of the connector.
        :param credentials: Dictionary containing the connector credentials.
        :raises Exception: If credentials are invalid or connector cannot be initialized.
        """
        validate_safe_name(account_name, "account name")
        validate_safe_name(connector_name, "connector name")

        # Capture the original credential file BEFORE the in-place overwrite performed by
        # update_connector_keys. This determines whether a failure is a brand-new CREATE
        # (rollback the partial file) or an UPDATE (restore the previous file byte-for-byte).
        credentials_path = f"credentials/{account_name}/connectors/{connector_name}.yml"
        credentials_existed = fs_util.path_exists(credentials_path)
        original_content = fs_util.read_file(credentials_path) if credentials_existed else None

        try:
            # Update the connector keys (this saves the credentials to file and validates them)
            connector = await self._connector_service.update_connector_keys(account_name, connector_name, credentials)

            await self.update_account_state()
        except Exception as e:
            logger.error(f"Error adding connector credentials for account {account_name}: {e}")
            # Roll back the file write. For a brand-new creation, delete the partial file. For an
            # update, update_connector_keys overwrote the previous (valid) credentials in-place, so
            # we restore the captured original content to keep the file byte-for-byte intact.
            if not credentials_existed:
                await self.delete_credentials(account_name, connector_name)
            elif original_content is not None:
                fs_util.ensure_file_and_dump_text(credentials_path, original_content)
            raise e

    @staticmethod
    def list_accounts():
        """
        List all the accounts that are connected to the trading system.
        :return: List of accounts.
        """
        return fs_util.list_folders('credentials')

    @staticmethod
    def list_credentials(account_name: str):
        """
        List all the credentials that are connected to the specified account.
        :param account_name: The name of the account.
        :return: List of credentials.
        """
        validate_safe_name(account_name, "account name")
        try:
            return [file for file in fs_util.list_files(f'credentials/{account_name}/connectors') if
                    file.endswith('.yml')]
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

    async def delete_credentials(self, account_name: str, connector_name: str):
        """
        Delete the credentials of the specified connector for the specified account.
        :param account_name:
        :param connector_name:
        :return:
        """
        validate_safe_name(account_name, "account name")
        validate_safe_name(connector_name, "connector name")
        # Delete credentials file if it exists
        if fs_util.path_exists(f"credentials/{account_name}/connectors/{connector_name}.yml"):
            fs_util.delete_file(directory=f"credentials/{account_name}/connectors", file_name=f"{connector_name}.yml")

        # Always perform cleanup regardless of file existence
        # Stop the connector if it's running
        await self._connector_service.stop_trading_connector(account_name, connector_name)
        # Clear the connector from cache
        self._connector_service.clear_trading_connector(account_name, connector_name)

        # Remove from account state
        if account_name in self.accounts_state and connector_name in self.accounts_state[account_name]:
            self.accounts_state[account_name].pop(connector_name)

    def add_account(self, account_name: str):
        """
        Add a new account.
        :param account_name:
        :return:
        """
        validate_safe_name(account_name, "account name")
        # Check if account already exists by looking at folders
        if account_name in self.list_accounts():
            raise HTTPException(status_code=400, detail="Account already exists.")
        
        files_to_copy = ["conf_client.yml", "conf_fee_overrides.yml", "hummingbot_logs.yml", ".password_verification"]
        fs_util.create_folder('credentials', account_name)
        fs_util.create_folder(f'credentials/{account_name}', "connectors")
        for file in files_to_copy:
            fs_util.copy_file(f"credentials/master_account/{file}", f"credentials/{account_name}/{file}")
        
        # Initialize account state
        self.accounts_state[account_name] = {}

    async def delete_account(self, account_name: str):
        """
        Delete the specified account.
        :param account_name:
        :return:
        """
        validate_safe_name(account_name, "account name")
        # Stop all connectors for this account
        for connector_name in self._connector_service.list_account_connectors(account_name):
            await self._connector_service.stop_trading_connector(account_name, connector_name)
        # Clear all connectors for this account from cache
        self._connector_service.clear_trading_connector(account_name)

        # Delete account folder
        fs_util.delete_folder('credentials', account_name)

        # Remove from account state
        if account_name in self.accounts_state:
            self.accounts_state.pop(account_name)
    
    async def get_account_current_state(self, account_name: str) -> Dict[str, List[Dict]]:
        """
        Get current state for a specific account from database.
        """
        try:
            async with self.db_manager.get_session_context() as session:
                repository = AccountRepository(session)
                return await repository.get_account_current_state(account_name)
        except Exception as e:
            logger.error(f"Error getting account current state: {e}")
            # Fallback to in-memory state
            return self.accounts_state.get(account_name, {})
    
    async def get_account_state_history(self,
                                        account_name: str,
                                        limit: Optional[int] = None,
                                        cursor: Optional[str] = None,
                                        start_time: Optional[datetime] = None,
                                        end_time: Optional[datetime] = None,
                                        interval: str = "5m"):
        """
        Get historical state for a specific account with pagination and interval sampling.

        Args:
            account_name: Account name to filter by
            limit: Maximum number of records to return
            cursor: Cursor for pagination
            start_time: Start time filter
            end_time: End time filter
            interval: Sampling interval (5m, 15m, 30m, 1h, 4h, 12h, 1d)
        """
        try:
            async with self.db_manager.get_session_context() as session:
                repository = AccountRepository(session)
                return await repository.get_account_state_history(
                    account_name=account_name,
                    limit=limit,
                    cursor=cursor,
                    start_time=start_time,
                    end_time=end_time,
                    interval=interval
                )
        except Exception as e:
            logger.error(f"Error getting account state history: {e}")
            return [], None, False
    
    async def get_connector_current_state(self, account_name: str, connector_name: str) -> List[Dict]:
        """
        Get current state for a specific connector.
        """
        try:
            async with self.db_manager.get_session_context() as session:
                repository = AccountRepository(session)
                return await repository.get_connector_current_state(account_name, connector_name)
        except Exception as e:
            logger.error(f"Error getting connector current state: {e}")
            # Fallback to in-memory state
            return self.accounts_state.get(account_name, {}).get(connector_name, [])
    
    async def get_connector_state_history(self, 
                                          account_name: str, 
                                          connector_name: str, 
                                          limit: Optional[int] = None,
                                          cursor: Optional[str] = None,
                                          start_time: Optional[datetime] = None,
                                          end_time: Optional[datetime] = None):
        """
        Get historical state for a specific connector with pagination.
        """
        try:
            async with self.db_manager.get_session_context() as session:
                repository = AccountRepository(session)
                return await repository.get_account_state_history(
                    account_name=account_name, 
                    connector_name=connector_name,
                    limit=limit,
                    cursor=cursor,
                    start_time=start_time,
                    end_time=end_time
                )
        except Exception as e:
            logger.error(f"Error getting connector state history: {e}")
            return [], None, False
    
    async def get_all_unique_tokens(self) -> List[str]:
        """
        Get all unique tokens across all accounts and connectors.
        """
        try:
            async with self.db_manager.get_session_context() as session:
                repository = AccountRepository(session)
                return await repository.get_all_unique_tokens()
        except Exception as e:
            logger.error(f"Error getting unique tokens: {e}")
            # Fallback to in-memory state
            tokens = set()
            for account_data in self.accounts_state.values():
                for connector_data in account_data.values():
                    for token_info in connector_data:
                        tokens.add(token_info.get("token"))
            return sorted(list(tokens))
    
    async def get_token_current_state(self, token: str) -> List[Dict]:
        """
        Get current state of a specific token across all accounts.
        """
        try:
            async with self.db_manager.get_session_context() as session:
                repository = AccountRepository(session)
                return await repository.get_token_current_state(token)
        except Exception as e:
            logger.error(f"Error getting token current state: {e}")
            return []
    
    async def get_portfolio_value(self, account_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Get total portfolio value, optionally filtered by account.
        """
        try:
            async with self.db_manager.get_session_context() as session:
                repository = AccountRepository(session)
                return await repository.get_portfolio_value(account_name)
        except Exception as e:
            logger.error(f"Error getting portfolio value: {e}")
            # Fallback to in-memory calculation
            portfolio = {"accounts": {}, "total_value": 0}
            
            accounts_to_process = [account_name] if account_name else self.accounts_state.keys()
            
            for acc_name in accounts_to_process:
                account_value = 0
                if acc_name in self.accounts_state:
                    for connector_data in self.accounts_state[acc_name].values():
                        for token_info in connector_data:
                            account_value += token_info.get("value", 0)
                    portfolio["accounts"][acc_name] = account_value
                    portfolio["total_value"] += account_value
            
            return portfolio
    
    def get_portfolio_distribution(self, account_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Get portfolio distribution by tokens with percentages.
        Delegates the pure math to PortfolioAnalyticsService (snapshots the live state internally).
        """
        return self.portfolio_analytics_service.get_portfolio_distribution(self.accounts_state, account_name)

    def get_account_distribution(self) -> Dict[str, Any]:
        """
        Get portfolio distribution by accounts with percentages.
        Delegates the pure math to PortfolioAnalyticsService (snapshots the live state internally).
        """
        return self.portfolio_analytics_service.get_account_distribution(self.accounts_state)

    async def place_trade(self, account_name: str, connector_name: str, trading_pair: str,
                         trade_type: TradeType, amount: Decimal, order_type: OrderType = OrderType.LIMIT,
                         price: Optional[Decimal] = None, position_action: PositionAction = PositionAction.OPEN) -> str:
        """
        Place a trade using the specified account and connector.

        Args:
            account_name: Name of the account to trade with
            connector_name: Name of the connector/exchange
            trading_pair: Trading pair (e.g., BTC-USDT)
            trade_type: "BUY" or "SELL"
            amount: Amount to trade
            order_type: "LIMIT", "MARKET", or "LIMIT_MAKER"
            price: Price for limit orders (required for LIMIT and LIMIT_MAKER)
            position_action: Position action for perpetual contracts (OPEN/CLOSE)

        Returns:
            Client order ID assigned by the connector

        Raises:
            HTTPException: If account, connector not found, or trade fails
        """
        # Validate account exists
        if account_name not in self.list_accounts():
            raise HTTPException(status_code=404, detail=f"Account '{account_name}' not found")

        connector = await self._connector_service.get_trading_connector(account_name, connector_name)

        # Validate price for limit orders
        if order_type in [OrderType.LIMIT, OrderType.LIMIT_MAKER] and price is None:
            raise HTTPException(status_code=400, detail="Price is required for LIMIT and LIMIT_MAKER orders")
        
        # Check if trading rules are loaded
        if not connector.trading_rules:
            raise HTTPException(
                status_code=503, 
                detail=f"Trading rules not yet loaded for {connector_name}. Please try again in a moment."
            )
        
        # Validate trading pair and get trading rule
        if trading_pair not in connector.trading_rules:
            available_pairs = list(connector.trading_rules.keys())[:10]  # Show first 10
            more_text = f" (and {len(connector.trading_rules) - 10} more)" if len(connector.trading_rules) > 10 else ""
            raise HTTPException(
                status_code=400, 
                detail=f"Trading pair '{trading_pair}' not supported on {connector_name}. "
                       f"Available pairs: {available_pairs}{more_text}"
            )
        
        trading_rule = connector.trading_rules[trading_pair]
        
        # Validate order type is supported
        if order_type not in connector.supported_order_types():
            supported_types = [ot.name for ot in connector.supported_order_types()]
            raise HTTPException(status_code=400, detail=f"Order type '{order_type.name}' not supported. Supported types: {supported_types}")
        
        # Quantize amount according to trading rules
        quantized_amount = connector.quantize_order_amount(trading_pair, amount)
        
        # Validate minimum order size
        if quantized_amount < trading_rule.min_order_size:
            raise HTTPException(
                status_code=400, 
                detail=f"Order amount {quantized_amount} is below minimum order size {trading_rule.min_order_size} for {trading_pair}"
            )
        
        # Calculate and validate notional size
        if order_type in [OrderType.LIMIT, OrderType.LIMIT_MAKER]:
            quantized_price = connector.quantize_order_price(trading_pair, price)
            notional_size = quantized_price * quantized_amount
        else:
            # For market orders without price, get current market price for validation
            try:
                prices = await self._market_data_service.get_prices(connector_name, [trading_pair])
                if trading_pair in prices and "error" not in prices:
                    price = Decimal(str(prices[trading_pair]))
            except Exception as e:
                logger.error(f"Error getting market price for {trading_pair}: {e}")
            notional_size = price * quantized_amount if price else Decimal("0")
            
        if notional_size < trading_rule.min_notional_size:
            raise HTTPException(
                status_code=400,
                detail=f"Order notional value {notional_size} is below minimum notional size {trading_rule.min_notional_size} for {trading_pair}. "
                       f"Increase the amount or price to meet the minimum requirement."
            )
        


        try:
            # Place the order using the connector with quantized values
            # (position_action will be ignored by non-perpetual connectors)
            if trade_type == TradeType.BUY:
                order_id = connector.buy(
                    trading_pair=trading_pair,
                    amount=quantized_amount,
                    order_type=order_type,
                    price=price or Decimal("1"),
                    position_action=position_action
                )
            else:
                order_id = connector.sell(
                    trading_pair=trading_pair,
                    amount=quantized_amount,
                    order_type=order_type,
                    price=price or Decimal("1"),
                    position_action=position_action
                )

            logger.info(f"Placed {trade_type} order for {amount} {trading_pair} on {connector_name} (Account: {account_name}). Order ID: {order_id}")
            return order_id
            
        except HTTPException:
            # Re-raise HTTP exceptions as-is
            raise
        except Exception as e:
            logger.error(f"Failed to place {trade_type} order: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to place trade: {str(e)}")
    
    async def get_connector_instance(self, account_name: str, connector_name: str):
        """
        Get a connector instance for direct access.

        Args:
            account_name: Name of the account
            connector_name: Name of the connector

        Returns:
            Connector instance

        Raises:
            HTTPException: If account or connector not found
        """
        if account_name not in self.list_accounts():
            raise HTTPException(status_code=404, detail=f"Account '{account_name}' not found")

        return await self._connector_service.get_trading_connector(account_name, connector_name)

    async def get_active_orders(self, account_name: str, connector_name: str) -> Dict[str, Any]:
        """
        Get active orders for a specific connector.
        
        Args:
            account_name: Name of the account
            connector_name: Name of the connector
            
        Returns:
            Dictionary of active orders
        """
        connector = await self.get_connector_instance(account_name, connector_name)
        return {order_id: order.to_json() for order_id, order in connector.in_flight_orders.items()}
    
    async def cancel_order(self, account_name: str, connector_name: str, client_order_id: str) -> str:
        """
        Cancel an active order.
        
        Args:
            account_name: Name of the account
            connector_name: Name of the connector
            client_order_id: Client order ID to cancel
            
        Returns:
            Client order ID that was cancelled
            
        Raises:
            HTTPException: 404 if order not found, 500 if cancellation fails
        """
        connector = await self.get_connector_instance(account_name, connector_name)
        
        # Check if order exists in in-flight orders
        if client_order_id not in connector.in_flight_orders:
            raise HTTPException(status_code=404, detail=f"Order '{client_order_id}' not found in active orders")
        
        try:
            result = connector.cancel(trading_pair="NA", client_order_id=client_order_id)
            logger.info(f"Initiated cancellation for order {client_order_id} on {connector_name} (Account: {account_name})")
            return result
        except Exception as e:
            logger.error(f"Failed to initiate cancellation for order {client_order_id}: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to initiate order cancellation: {str(e)}")
    
    async def set_leverage(self, account_name: str, connector_name: str,
                          trading_pair: str, leverage: int) -> Dict[str, str]:
        """
        Set leverage for a specific trading pair on a perpetual connector.
        Delegates to PerpetualTradingService.
        """
        return await self.perpetual_trading_service.set_leverage(account_name, connector_name, trading_pair, leverage)

    async def set_position_mode(self, account_name: str, connector_name: str,
                               position_mode: PositionMode) -> Dict[str, str]:
        """
        Set position mode for a perpetual connector.
        Delegates to PerpetualTradingService.
        """
        return await self.perpetual_trading_service.set_position_mode(account_name, connector_name, position_mode)

    async def get_position_mode(self, account_name: str, connector_name: str) -> Dict[str, str]:
        """
        Get current position mode for a perpetual connector.
        Delegates to PerpetualTradingService.
        """
        return await self.perpetual_trading_service.get_position_mode(account_name, connector_name)

    async def get_account_positions(self, account_name: str, connector_name: str) -> List[Dict]:
        """
        Get current positions for a specific perpetual connector.
        Delegates to PerpetualTradingService.
        """
        return await self.perpetual_trading_service.get_account_positions(account_name, connector_name)

    # ============================================
    # Gateway Wallet Management Methods
    # ============================================

    async def _update_gateway_balances(self, chain_networks: Optional[List[str]] = None):
        """Update Gateway wallet balances in master_account state.

        Only queries the defaultWallet on each network in defaultNetworks for each chain.
        This is more efficient than querying all wallets on all networks.

        Args:
            chain_networks: If provided, only update these chain-network combinations
                           (e.g., ['solana-mainnet-beta', 'ethereum-mainnet']).
                           If None, update all defaultNetworks for each chain.
        """
        try:
            # Check if Gateway is available
            if not await self.gateway_client.ping():
                logger.debug("Gateway service is not available, skipping wallet balance update")
                return

            # Get all available chains
            chains_result = await self.gateway_client.get_chains()
            if not chains_result or "chains" not in chains_result:
                logger.error("Could not get chains from Gateway")
                return

            known_chains = {c["chain"] for c in chains_result["chains"]}

            # Ensure master_account exists in accounts_state
            if "master_account" not in self.accounts_state:
                self.accounts_state["master_account"] = {}

            # Collect all balance query tasks for parallel execution
            balance_tasks = []
            task_metadata = []  # Store (chain, network, address) for each task

            # Fetch every chain's config concurrently first, instead of one HTTP round-trip
            # per chain in serial. Each config is the merged chain-network namespace
            # (e.g., solana-mainnet-beta), returning both chain-level fields
            # (defaultWallet, defaultNetworks) and network fields.
            chains_with_networks = [
                chain_info for chain_info in chains_result["chains"] if chain_info.get("networks")
            ]
            for chain_info in chains_result["chains"]:
                if not chain_info.get("networks"):
                    logger.debug(f"Chain '{chain_info['chain']}' has no networks configured, skipping")

            config_results = await asyncio.gather(
                *[
                    self.gateway_client.get_config(f"{chain_info['chain']}-{chain_info['networks'][0]}")
                    for chain_info in chains_with_networks
                ],
                return_exceptions=True,
            )

            # For each chain, build balance tasks from its resolved config
            for chain_info, config in zip(chains_with_networks, config_results):
                chain = chain_info["chain"]
                first_network = chain_info["networks"][0]

                # A chain whose get_config raised is skipped/logged, same as before
                if isinstance(config, Exception):
                    logger.warning(f"Could not get config for '{chain}-{first_network}': {config}")
                    continue

                # A Gateway HTTP error dict would otherwise read as "missing defaultWallet" below
                if config is None or set(config.keys()) == {"error", "status"}:
                    logger.warning(f"Gateway error getting config for '{chain}-{first_network}': {config}")
                    continue

                default_wallet = config.get("defaultWallet")
                default_networks = config.get("defaultNetworks", [])

                if not default_wallet:
                    logger.debug(f"Chain '{chain}' missing defaultWallet, skipping")
                    continue

                # Skip placeholder wallet addresses from Gateway templates (e.g., '<ethereum-wallet-address>')
                if default_wallet.startswith("<") and default_wallet.endswith(">"):
                    logger.debug(f"Chain '{chain}' has placeholder defaultWallet '{default_wallet}', skipping")
                    continue

                if not default_networks:
                    # Fall back to defaultNetwork (singular) if defaultNetworks not set
                    default_network = config.get("defaultNetwork")
                    if default_network:
                        default_networks = [default_network]
                    else:
                        logger.debug(f"Chain '{chain}' missing defaultNetworks, skipping")
                        continue

                # Create balance tasks for each default network
                for network in default_networks:
                    chain_network_key = f"{chain}-{network}"

                    # Filter by chain_networks if specified
                    if chain_networks and chain_network_key not in chain_networks:
                        continue

                    balance_tasks.append(self.get_gateway_balances(chain, default_wallet, network=network))
                    task_metadata.append((chain, network, default_wallet))

            # Build set of active chain-network keys
            active_chain_networks = {f"{chain}-{network}" for chain, network, _ in task_metadata}

            # Execute all balance queries in parallel
            if balance_tasks:
                results = await asyncio.gather(*balance_tasks, return_exceptions=True)

                # Process results
                for result, (chain, network, address) in zip(results, task_metadata):
                    chain_network = f"{chain}-{network}"

                    if isinstance(result, Exception):
                        logger.error(f"Error updating Gateway balances for {chain}-{network} wallet {address}: {result}")
                        # Store empty list for error state
                        self.accounts_state["master_account"][chain_network] = []
                    elif result:
                        # Only store if there are actual balances (non-empty list)
                        self.accounts_state["master_account"][chain_network] = result
                    else:
                        # Store empty list to indicate we checked this network
                        self.accounts_state["master_account"][chain_network] = []

            # Only remove stale keys if we're doing a full update (no filter)
            # When filtering, we don't want to remove keys that weren't in the filter
            if not chain_networks:
                # Remove stale gateway chain-network keys (default network/wallet changed or no longer configured)
                # Gateway keys follow pattern: chain-network (e.g., "solana-mainnet-beta", "ethereum-mainnet")
                stale_keys = []
                for key in self.accounts_state["master_account"]:
                    # Check if key looks like a gateway chain-network (contains hyphen and matches chain pattern)
                    if "-" in key and key not in active_chain_networks:
                        # Verify it's a gateway key by checking if chain part matches known chains
                        chain_part = key.split("-")[0]
                        if chain_part in known_chains:
                            stale_keys.append(key)

                for key in stale_keys:
                    logger.info(f"Removing stale Gateway balance data for {key} (no longer default network)")
                    del self.accounts_state["master_account"][key]

        except Exception as e:
            logger.error(f"Error updating Gateway balances: {e}")

    async def get_gateway_wallets(self) -> List[Dict]:
        """
        Get all wallets from Gateway. Gateway manages its own encrypted wallets.
        Delegates to GatewayWalletService.
        """
        return await self.gateway_wallet_service.get_gateway_wallets()

    async def add_gateway_wallet(self, chain: str, private_key: str, set_default: bool = True) -> Dict:
        """
        Add a wallet to Gateway. Gateway handles encryption internally.
        Delegates to GatewayWalletService.
        """
        return await self.gateway_wallet_service.add_gateway_wallet(chain, private_key, set_default=set_default)

    async def remove_gateway_wallet(self, chain: str, address: str) -> Dict:
        """
        Remove a wallet from Gateway.
        Delegates to GatewayWalletService.
        """
        return await self.gateway_wallet_service.remove_gateway_wallet(chain, address)

    async def get_gateway_balances(self, chain: str, address: str, network: Optional[str] = None,
                                   tokens: Optional[List[str]] = None) -> List[Dict]:
        """
        Get Gateway wallet balances with pricing from rate sources.
        Delegates to GatewayWalletService.
        """
        return await self.gateway_wallet_service.get_gateway_balances(chain, address, network=network, tokens=tokens)

    def get_unwrapped_token(self, token: str) -> str:
        """Get the unwrapped version of a wrapped token symbol (e.g., WSOL -> SOL)."""
        if token.startswith("W") and token[1:] in self.potential_wrapped_tokens:
            return token[1:]
        return token
