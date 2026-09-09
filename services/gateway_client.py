import logging
import ssl
import time
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

import aiohttp

from models.gateway_generated import (
    AmmAddRequest,
    AmmCreatePoolRequest,
    AmmExecuteSwapRequest,
    AmmPoolInfoRequest,
    AmmPositionInfoRequest,
    AmmPositionsOwnedRequest,
    AmmQuoteLiquidityRequest,
    AmmQuoteSwapRequest,
    AmmRemoveRequest,
    ClmmAddRequest,
    ClmmCloseRequest,
    ClmmCollectFeesRequest,
    ClmmCreatePoolRequest,
    ClmmExecuteSwapRequest,
    ClmmFetchPoolsRequest,
    ClmmOpenRequest,
    ClmmPoolInfoRequest,
    ClmmPositionInfoRequest,
    ClmmPositionsOwnedRequest,
    ClmmQuoteLiquidityRequest,
    ClmmQuoteSwapRequest,
    ClmmRemoveRequest,
    RouterExecuteQuoteRequest,
    RouterExecuteSwapRequest,
    RouterQuoteSwapRequest,
)

logger = logging.getLogger(__name__)

# The request model for each unified /trading route, keyed by the trading type resolved
# at call time. The three surfaces do not take the same fields — only the router accepts
# approximateIfNoExactOut, only the pool-scoped ones accept poolAddress — so each has its
# own model rather than one shape covering all three.
_QUOTE_SWAP_REQUESTS = {
    "router": RouterQuoteSwapRequest,
    "clmm": ClmmQuoteSwapRequest,
    "amm": AmmQuoteSwapRequest,
}
_EXECUTE_SWAP_REQUESTS = {
    "router": RouterExecuteSwapRequest,
    "clmm": ClmmExecuteSwapRequest,
    "amm": AmmExecuteSwapRequest,
}


def _wire_str(value: Any) -> str:
    """A query value as text, keeping whole numbers whole.

    Gateway types every numeric field as `number`, so pydantic holds a page index or a
    row limit as a float and ``str`` would render it "2.0" — which is not what a page
    index looks like to the DEX listing APIs behind fetch-pools. Integral values are
    emitted without the fractional part; the amounts are unaffected either way, since
    Gateway coerces the string back per its schema.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)) and value == int(value):
        return str(int(value))
    return str(value)


def _query(request: Any) -> Dict[str, str]:
    """A request model as query parameters.

    Everything is stringified because aiohttp rejects a non-string query value, and
    Gateway coerces the strings back per its schema. Fields left as None are dropped:
    Gateway applies its own default for an absent parameter, which is not the same as
    being told the value is null.
    """
    return {
        key: _wire_str(value)
        for key, value in request.model_dump(by_alias=True, exclude_none=True).items()
    }


def _body(request: Any) -> Dict[str, Any]:
    """A request model as a JSON body.

    Dumped in python mode and widened here rather than with ``mode="json"``, which
    renders Decimal as a string. Gateway declares these fields as `type: number` — its
    `decimal` format tells a client to *hold* the value as a decimal, not to send it as
    text — so a string would arrive as the wrong JSON type.
    """
    return {
        key: (float(value) if isinstance(value, Decimal) else value)
        for key, value in request.model_dump(by_alias=True, exclude_none=True).items()
    }


# When a caller names a connector without a trading type, Gateway's own
# config/connectors listing decides the type — preferring a router route, then
# CLMM, then AMM. A hardcoded roster silently misrouted every connector Gateway
# added after it was written.
_SWAP_TYPE_PREFERENCE = ("router", "clmm", "amm")

# The single source for chain -> native gas token. Every writer of gas_token
# columns (routers and the transaction poller) must use this — drifted local
# copies previously produced "MATIC", None, and "UNKNOWN" for the same chain.
_NATIVE_GAS_TOKENS = {
    "solana": "SOL",
    "ethereum": "ETH",
    "polygon": "MATIC",
    "avalanche": "AVAX",
    "optimism": "ETH",
    "arbitrum": "ETH",
    "base": "ETH",
    "bsc": "BNB",
    "cronos": "CRO",
}


def get_native_gas_token(chain: str) -> str:
    """Native gas token symbol for a chain (e.g. 'solana' -> 'SOL')."""
    return _NATIVE_GAS_TOKENS.get(chain.lower(), "UNKNOWN")


class GatewayError(Exception):
    """A Gateway HTTP request that completed with a non-OK status."""

    def __init__(self, message: str, status: int, code: Optional[str] = None):
        super().__init__(message)
        self.status = status
        # Gateway's machine-readable error code (TRANSACTION_TIMEOUT,
        # SLIPPAGE_EXCEEDED, ...). Callers branch on this instead of
        # string-matching the message.
        self.code = code


def check_gateway_error(result: Optional[Any]) -> Any:
    """
    Validate a GatewayClient response, raising instead of letting error shapes flow onward.

    ``_request`` returns ``{"error": <str>, "status": <int>}`` on non-OK HTTP responses and
    ``None`` on connection errors. Callers that treat those as data silently corrupt results
    (e.g. rendering a 404 as price 0), so pass every response through this helper unless the
    caller explicitly branches on the error shape.
    """
    if result is None:
        raise GatewayError("No response from Gateway (connection error)", 503)
    if isinstance(result, dict) and {"error", "status"} <= set(result.keys()) <= {"error", "status", "code"}:
        raise GatewayError(str(result["error"]), int(result["status"]), result.get("code"))
    return result


class GatewayClient:
    """
    Simplified Gateway HTTP client for API integration.
    Provides essential functionality for wallet management and balance queries.
    """

    # How long a ``ping`` verdict stays reusable. The availability guard
    # (``deps.require_gateway_online``) runs ahead of every guarded request, so without a cache
    # each one pays a full round-trip to Gateway. The guard exists to catch "Gateway is down",
    # not to timestamp the exact moment it went down, so a couple of seconds of staleness is the
    # right trade — and any call that fails to connect clears the cache anyway (PERF-114).
    PING_CACHE_TTL_SECONDS = 2.0

    def __init__(
        self,
        base_url: str = "http://localhost:15888",
        ssl_context_factory: Optional[Callable[[], ssl.SSLContext]] = None,
    ):
        """
        Args:
            base_url: Gateway base URL. Use an ``https://`` scheme together with
                ``ssl_context_factory`` to talk to a secured (mTLS) Gateway (SEC-048).
            ssl_context_factory: Zero-arg callable returning a client SSLContext presenting the
                shared client cert. Called lazily (and cached) on the first ``https`` request, so
                certs generated *after* the API started — e.g. once the Gateway is started — are
                picked up without an API restart. Ignored for plain ``http://``.
        """
        self.base_url = base_url
        self._ssl_context_factory = ssl_context_factory
        self._ssl_context: Optional[ssl.SSLContext] = None
        self._is_https = base_url.lower().startswith("https://")
        self._session: Optional[aiohttp.ClientSession] = None
        # Guards the "certs unavailable" warning so background pollers don't spam it every cycle
        # while the Gateway is simply not started. Logged once on the transition, then suppressed
        # until certs become available again.
        self._certs_unavailable_warned = False
        # Gateway's connector -> trading_types listing, fetched on first use.
        self._connector_trading_types: Optional[Dict[str, List[str]]] = None
        # Per-(chain, network) token address -> symbol map, fetched on first use.
        self._token_symbols: Dict[tuple[str, str], Dict[str, str]] = {}
        # Last ``ping`` verdict as (monotonic expiry, is_online), or None when unknown/invalidated.
        self._ping_cache: Optional[tuple[float, bool]] = None

    @staticmethod
    def parse_network_id(network_id: str) -> tuple[str, str]:
        """
        Parse network_id in format 'chain-network' into (chain, network).

        Examples:
            'solana-mainnet-beta' -> ('solana', 'mainnet-beta')
            'ethereum-mainnet' -> ('ethereum', 'mainnet')
        """
        parts = network_id.split('-', 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid network_id format. Expected 'chain-network', got '{network_id}'")
        return parts[0], parts[1]

    async def get_wallet_address_or_default(self, chain: str, wallet_address: Optional[str] = None) -> str:
        """Get wallet address - use provided or get default for chain"""
        if wallet_address:
            return wallet_address

        default_wallet = await self.get_default_wallet_address(chain)
        if not default_wallet:
            raise ValueError(f"No wallet configured for chain '{chain}'")
        # Gateway's fresh config templates write "<solana-wallet-address>" /
        # "<ethereum-wallet-address>" as the placeholder — passing that literal
        # string on would surface as an opaque Gateway address-validation error.
        if default_wallet.startswith("<") and default_wallet.endswith(">"):
            raise ValueError(f"No valid wallet configured for chain '{chain}' (found placeholder: {default_wallet})")
        return default_wallet

    def _get_ssl_context(self) -> Optional[ssl.SSLContext]:
        """Lazily build and cache the client SSLContext for https Gateways.

        Deferred so certs created after startup (once the Gateway is started) are picked up.
        Raises FileNotFoundError (from the factory) while the cert set is still absent.
        """
        if not self._is_https or self._ssl_context_factory is None:
            return None
        if self._ssl_context is None:
            self._ssl_context = self._ssl_context_factory()
            # Certs are now available; allow a fresh warning if they ever disappear again.
            self._certs_unavailable_warned = False
        return self._ssl_context

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session"""
        if self._session is None or self._session.closed:
            ssl_context = self._get_ssl_context()
            if ssl_context is not None:
                connector = aiohttp.TCPConnector(ssl=ssl_context)
                self._session = aiohttp.ClientSession(connector=connector)
            else:
                self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        """Close the aiohttp session"""
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, path: str, params: Dict = None, json: Dict = None) -> Optional[Dict]:
        """Make HTTP request to Gateway"""
        url = f"{self.base_url}/{path}"

        try:
            session = await self._get_session()
        except FileNotFoundError as e:
            # https Gateway selected but the shared certs aren't available yet (Gateway not
            # started). Return a clean error instead of crashing the caller. Warn only once on
            # the transition so background pollers don't spam the log every cycle while the
            # Gateway stays unstarted (a normal, optional state).
            if not self._certs_unavailable_warned:
                logger.warning(f"Gateway mTLS certs unavailable, cannot reach {url}: {e}")
                self._certs_unavailable_warned = True
            else:
                logger.debug(f"Gateway mTLS certs still unavailable, cannot reach {url}: {e}")
            self._invalidate_ping_cache()
            return {"error": "Gateway client certificates not available; start the Gateway first", "status": 503}

        try:
            if method == "GET":
                async with session.get(url, params=params) as response:
                    if not response.ok:
                        error_body, error_code = await self._get_error_body(response)
                        logger.warning(f"Gateway request failed: {method} {url} - {response.status} - {error_body}")
                        return {"error": error_body, "status": response.status, "code": error_code}
                    return await response.json()
            elif method == "POST":
                async with session.post(url, params=params, json=json) as response:
                    if not response.ok:
                        error_body, error_code = await self._get_error_body(response)
                        logger.warning(f"Gateway request failed: {method} {url} - {response.status} - {error_body}")
                        return {"error": error_body, "status": response.status, "code": error_code}
                    return await response.json()
            elif method == "DELETE":
                async with session.delete(url, params=params, json=json) as response:
                    if not response.ok:
                        error_body, error_code = await self._get_error_body(response)
                        logger.warning(f"Gateway request failed: {method} {url} - {response.status} - {error_body}")
                        return {"error": error_body, "status": response.status, "code": error_code}
                    return await response.json()
        except aiohttp.ClientError as e:
            logger.debug(f"Gateway request error: {method} {url} - {e}")
            # Gateway is unreachable: drop any cached "available" verdict so the next guarded
            # request re-pings instead of waiting out the TTL on a stale answer.
            self._invalidate_ping_cache()
            return None
        except Exception as e:
            logger.debug(f"Gateway request failed: {method} {url} - {e}")
            raise

    async def _get_error_body(self, response: aiohttp.ClientResponse) -> tuple:
        """Extract (message, code) from an error response body.

        Gateway's error envelope is {statusCode, error, message, code?}; the
        code is machine-readable (TRANSACTION_TIMEOUT, SLIPPAGE_EXCEEDED, ...)
        and is what callers should branch on.
        """
        try:
            data = await response.json()
            if isinstance(data, dict):
                return (data.get("message") or data.get("error") or str(data), data.get("code"))
            return (str(data), None)
        except Exception:
            try:
                return (await response.text(), None)
            except Exception:
                return (f"HTTP {response.status}", None)

    def _invalidate_ping_cache(self) -> None:
        """Forget the cached availability verdict; the next ``ping`` hits Gateway again."""
        self._ping_cache = None

    async def ping(self) -> bool:
        """Check if Gateway is online, reusing a verdict at most PING_CACHE_TTL_SECONDS old."""
        cached = self._ping_cache
        if cached is not None and time.monotonic() < cached[0]:
            return cached[1]
        try:
            response = await self._request("GET", "")
            online = response.get("status") == "ok"
        except Exception:
            online = False
        # Assigned after the request so the invalidation _request performs on a connection
        # failure cannot outrun the verdict it caused.
        self._ping_cache = (time.monotonic() + self.PING_CACHE_TTL_SECONDS, online)
        return online

    async def get_wallets(self) -> List[Dict]:
        """Get all connected wallets"""
        return await self._request("GET", "wallet")

    async def get_default_wallet_address(self, chain: str) -> Optional[str]:
        """Get default wallet address for a chain from Gateway config.

        Raises GatewayError(503) when Gateway is unreachable — an unreachable
        Gateway must not masquerade as "no wallet configured" (a 400 that sends
        the operator chasing the wrong problem).
        """
        config = await self._request("GET", "config", params={"namespace": chain})
        if config is None:
            raise GatewayError("Gateway service is not available", status=503)
        if isinstance(config, dict) and set(config.keys()) == {"error", "status"}:
            raise GatewayError(str(config["error"]), status=config.get("status", 502))
        return config.get("defaultWallet")

    async def get_all_wallet_addresses(self, chain: Optional[str] = None) -> Dict[str, List[str]]:
        """
        Get all wallet addresses, optionally filtered by chain.

        Args:
            chain: Optional chain filter (e.g., 'solana', 'ethereum').
                   If not provided, returns wallets for all chains.

        Returns:
            Dict mapping chain name to list of wallet addresses.
            Example: {"solana": ["addr1", "addr2"], "ethereum": ["addr3"]}
        """
        try:
            wallets = await self.get_wallets()
            if wallets is None:
                return {}

            result = {}
            for wallet in wallets:
                wallet_chain = wallet.get("chain")
                if chain and wallet_chain != chain:
                    continue

                # Hardware (Ledger) wallets live in a separate list — without them
                # the discovery/balance sweeps never see hardware-held positions.
                addresses = list(wallet.get("walletAddresses", [])) + list(wallet.get("hardwareWalletAddresses", []))
                if addresses and wallet_chain:
                    result[wallet_chain] = addresses

            return result
        except Exception as e:
            logger.error(f"Error getting all wallet addresses: {e}")
            return {}

    async def add_wallet(self, chain: str, private_key: str, set_default: bool = True) -> Dict:
        """Add a wallet to Gateway"""
        return await self._request("POST", "wallet/add", json={
            "chain": chain,
            "privateKey": private_key,
            "setDefault": set_default
        })

    async def remove_wallet(self, chain: str, address: str) -> Dict:
        """Remove a wallet from Gateway"""
        return await self._request("DELETE", "wallet/remove", json={
            "chain": chain,
            "address": address
        })

    async def set_default_wallet(self, chain: str, address: str) -> Dict:
        """Set the default wallet for a chain in Gateway"""
        return await self._request("POST", "wallet/setDefault", json={
            "chain": chain,
            "address": address
        })

    async def get_balances(self, chain: str, network: str, address: str, tokens: Optional[List[str]] = None) -> Dict:
        """Get token balances for a wallet"""
        return await self._request("POST", f"chains/{chain}/balances", json={
            "network": network,
            "address": address,
            "tokens": tokens if tokens is not None else []
        })

    async def get_chains(self) -> Dict:
        """Get available chains"""
        return await self._request("GET", "config/chains")

    async def get_connectors(self) -> Dict:
        """Get available connectors with their trading types"""
        return await self._request("GET", "config/connectors")

    async def get_default_network(self, chain: str) -> Optional[str]:
        """Get default network for a chain"""
        try:
            config = await self._request("GET", "config", params={"namespace": chain})
            return config.get("defaultNetwork")
        except Exception:
            return None

    async def get_tokens(self, chain: str, network: str) -> Dict:
        """Get available tokens for a chain/network"""
        return await self._request("GET", "tokens", params={
            "chainNetwork": f"{chain}-{network}"
        })

    async def _get_token_symbols(self, chain: str, network: str) -> Dict[str, str]:
        """Gateway's token address -> symbol map for a network, fetched once per client."""
        key = (chain, network)
        if key not in self._token_symbols:
            listing = check_gateway_error(await self.get_tokens(chain, network))
            self._token_symbols[key] = {
                token["address"]: token["symbol"]
                for token in listing.get("tokens", [])
                if token.get("address") and token.get("symbol")
            }
        return self._token_symbols[key]

    async def resolve_token_symbol(self, chain: str, network: str, address: str) -> str:
        """
        Symbol for a token address, falling back to the address itself.

        Gateway knows only the tokens in its configured list, so a pool on an unlisted
        mint has no symbol to report. The full address then stands in as the identifier:
        it is at least unambiguous and usable, where the truncated fragment this
        replaced ('11111112' for wrapped SOL) named nothing and matched nothing.
        """
        if not address:
            return ""
        return (await self._get_token_symbols(chain, network)).get(address, address)

    async def add_token(self, chain: str, network: str, address: str, symbol: str, name: str, decimals: int) -> Dict:
        """Add a custom token to Gateway's token list"""
        return await self._request("POST", "tokens", json={
            "chainNetwork": f"{chain}-{network}",
            "token": {
                "address": address,
                "symbol": symbol,
                "name": name,
                "decimals": decimals
            }
        })

    async def delete_token(self, chain: str, network: str, token_address: str) -> Dict:
        """Delete a custom token from Gateway's token list"""
        return await self._request("DELETE", f"tokens/{token_address}", params={
            "chainNetwork": f"{chain}-{network}"
        })

    async def save_token(self, chain: str, network: str, token_address: str) -> Dict:
        """Save a token by address - auto-fetches info from GeckoTerminal"""
        chain_network = f"{chain}-{network}"
        return await self._request("POST", f"tokens/save/{token_address}", params={
            "chainNetwork": chain_network
        }, json={})

    async def get_config(self, namespace: str) -> Dict:
        """Get configuration for a specific namespace (connector or chain-network)"""
        return await self._request("GET", "config", params={"namespace": namespace})

    async def update_config(self, namespace: str, path: str, value: Any) -> Dict:
        """Update a configuration value for a namespace"""
        return await self._request("POST", "config/update", json={
            "namespace": namespace,
            "path": path,
            "value": value
        })

    async def get_api_keys(self) -> Dict:
        """Get all configured API keys from Gateway"""
        return await self._request("GET", "config", params={"namespace": "apiKeys"})

    async def update_api_keys(self, api_keys: Dict[str, str]) -> List[Dict]:
        """
        Update API keys in Gateway configuration.

        Args:
            api_keys: Dict mapping provider name to API key value
                     (e.g., {"helius": "abc123", "infura": "xyz789"})

        Returns:
            List of results for each API key update
        """
        results = []
        for provider, api_key in api_keys.items():
            result = await self._request("POST", "config/update", json={
                "namespace": "apiKeys",
                "path": provider,
                "value": api_key
            })
            results.append(result)
        return results

    async def get_pools(
        self,
        chain: str,
        network: str,
        connector: Optional[str] = None,
        pool_type: Optional[str] = None,
        search: Optional[str] = None
    ) -> List[Dict]:
        """Get pools for a chain and network with optional filtering"""
        params = {
            "chainNetwork": f"{chain}-{network}"
        }
        if connector:
            params["connector"] = connector
        if pool_type:
            params["type"] = pool_type.lower()
        if search:
            params["search"] = search
        return await self._request("GET", "pools", params=params)

    async def add_pool(
        self,
        chain: str,
        network: str,
        connector: str,
        pool_type: str,
        address: str,
        base_symbol: str,
        quote_symbol: str,
        base_token_address: str,
        quote_token_address: str,
        fee_pct: Optional[float] = None
    ) -> Dict:
        """Add a new pool"""
        payload = {
            "chainNetwork": f"{chain}-{network}",
            "connector": connector,
            "type": pool_type.lower(),  # Gateway expects lowercase (amm, clmm)
            "address": address,
            "baseSymbol": base_symbol,
            "quoteSymbol": quote_symbol,
            "baseTokenAddress": base_token_address,
            "quoteTokenAddress": quote_token_address
        }
        if fee_pct is not None:
            payload["feePct"] = fee_pct
        return await self._request("POST", "pools", json=payload)

    async def save_pool(
        self,
        chain_network: str,
        address: str,
        connector: Optional[str] = None,
        pool_type: Optional[str] = None,
    ) -> Dict:
        """Save a pool by address.

        Gateway asks GeckoTerminal which DEX an address belongs to, and whether it is
        amm or clmm; the pool's own facts always come from the connector. Passing
        connector and pool_type answers that question directly and skips the lookup —
        a caller holding an LP provider config such as 'meteora/clmm' already knows it.
        """
        params = {"chainNetwork": chain_network}
        if connector and pool_type:
            params["connector"] = connector
            params["type"] = pool_type
        return await self._request("POST", f"pools/save/{address}", params=params, json={})

    async def delete_pool(self, chain: str, network: str, address: str) -> Dict:
        """Delete a pool from Gateway's pool list"""
        return await self._request("DELETE", f"pools/{address}", params={
            "chainNetwork": f"{chain}-{network}"
        })

    # ============================================
    # Swap Operations (/trading/{router,clmm,amm}/{quote,execute}-swap)
    # ============================================

    async def _get_connector_trading_types(self) -> Dict[str, List[str]]:
        """Gateway's connector -> trading_types map, fetched once per client."""
        if self._connector_trading_types is None:
            listing = check_gateway_error(await self.get_connectors())
            self._connector_trading_types = {
                entry["name"]: list(entry.get("trading_types", []))
                for entry in listing.get("connectors", [])
                if entry.get("name")
            }
        return self._connector_trading_types

    async def resolve_swap_route(self, connector: str) -> tuple[str, str]:
        """
        Split a swap provider into the (bare name, trading type) the routes need.

        Gateway carries the trading type in the path — /trading/router, /trading/clmm,
        /trading/amm — and constrains each route's `connector` to bare names, so a typed
        value has to be taken apart rather than passed through. A typed input
        ('raydium/amm') is split as given; a bare one takes the connector's most
        swap-appropriate type as Gateway reports it: router, else clmm, else amm.
        Unknown names raise rather than guessing a type Gateway would reject with an
        opaque 400.
        """
        if "/" in connector:
            name, trading_type = connector.split("/", 1)
            return name, trading_type
        trading_types = (await self._get_connector_trading_types()).get(connector)
        if trading_types is None:
            raise GatewayError(
                f"Unknown swap connector '{connector}'. Gateway reports: "
                f"{', '.join(sorted((await self._get_connector_trading_types()).keys()))}",
                400,
            )
        for candidate in _SWAP_TYPE_PREFERENCE:
            if candidate in trading_types:
                return connector, candidate
        raise GatewayError(
            f"Connector '{connector}' supports no swap trading type "
            f"(Gateway reports: {', '.join(trading_types) or 'none'})",
            400,
        )

    async def quote_swap(
        self,
        connector: str,
        chain_network: str,
        base_asset: str,
        quote_asset: str,
        amount: float,
        side: str,
        slippage_pct: Optional[float] = None,
        extra_params: Optional[Dict] = None,
        pool_address: Optional[str] = None
    ) -> Dict:
        """
        Get a swap quote from the trading surface matching the connector's type.

        Args:
            connector: Swap provider, either bare ('jupiter', 'meteora') or typed
                ('jupiter/router', 'raydium/amm', 'meteora/clmm'). The type selects the
                route — /trading/{router,clmm,amm}/quote-swap — and the bare name is sent
                as `connector`.
            chain_network: 'chain-network' format (e.g. 'solana-mainnet-beta').
            pool_address: Pin an amm/clmm quote to one pool. Omitted, Gateway resolves the
                pool from its configured list by token pair, which cannot reach a pool that
                is not in it. Routers reject this — they choose their own path across pools.
            extra_params: Connector-specific query params under Gateway's own names
                (e.g. approximateIfNoExactOut). The router validates keys first.
        """
        name, trading_type = await self.resolve_swap_route(connector)
        request_model = _QUOTE_SWAP_REQUESTS[trading_type]
        if pool_address and trading_type == "router":
            # The router model has no poolAddress, and pydantic drops an unknown keyword
            # silently — which would look like the pin applied. Routers choose their own
            # path across pools, so there is nothing to pin.
            raise ValueError(
                f"Router connector '{name}' does not take a pool_address: a router routes "
                "across pools rather than executing against one. Name an amm or clmm "
                "connector to pin a pool."
            )
        # poolAddress only for the pool-scoped models: the router model does not declare
        # it, and Gateway now rejects an undeclared key rather than dropping it.
        pool_kwargs = {} if trading_type == "router" else {"poolAddress": pool_address or None}
        params = _query(
            request_model(
                chainNetwork=chain_network,
                connector=name,
                baseToken=base_asset,
                quoteToken=quote_asset,
                amount=amount,
                side=side.upper(),
                slippagePct=slippage_pct,
                **pool_kwargs,
            )
        )
        if extra_params:
            # Connector-specific params, merged after the model: they are named by the
            # connector rather than the route, so no route schema declares them. Query
            # params must be strings for aiohttp; Gateway's schema coerces "true"/"false"
            # back to booleans.
            for key, value in extra_params.items():
                params[key] = str(value).lower() if isinstance(value, bool) else str(value)

        return await self._request("GET", f"trading/{trading_type}/quote-swap", params=params)

    async def execute_quote(
        self,
        connector: str,
        chain_network: str,
        wallet_address: str,
        quote_id: str,
    ) -> Dict:
        """Execute a quote the caller already holds, by its id.

        Router-only, and deliberately so: a quote id refers to route calldata Gateway
        cached, which pool-scoped amm/clmm swaps have no equivalent of — they price
        against a pool at execution time. Naming a non-router connector therefore fails
        here rather than silently re-pricing, which would defeat the point of the flow.
        """
        name, trading_type = await self.resolve_swap_route(connector)
        if trading_type != "router":
            raise ValueError(
                f"Connector '{name}' is a {trading_type} connector: only routers hold a quote to "
                "execute. Use /swap/execute for a pool-scoped swap, which prices at execution."
            )
        return await self._request("POST", "trading/router/execute-quote", json=_body(
            RouterExecuteQuoteRequest(
                chainNetwork=chain_network,
                connector=name,
                walletAddress=wallet_address,
                quoteId=quote_id,
            )
        ))

    async def execute_swap(
        self,
        connector: str,
        chain_network: str,
        wallet_address: str,
        base_asset: str,
        quote_asset: str,
        amount: float,
        side: str,
        slippage_pct: Optional[float] = None,
        extra_params: Optional[Dict] = None,
        pool_address: Optional[str] = None
    ) -> Dict:
        """Execute a swap on the trading surface matching the connector's type.

        The type selects the route — /trading/{router,clmm,amm}/execute-swap — and the
        bare name is sent as `connector`. pool_address pins an amm/clmm swap to one pool;
        routers reject it. extra_params carries connector-specific params under Gateway's
        own names (e.g. approximateIfNoExactOut); the router validates keys first.
        """
        name, trading_type = await self.resolve_swap_route(connector)
        if pool_address and trading_type == "router":
            # See quote_swap: the router model has no poolAddress and pydantic would drop
            # it in silence, which would read as though the pin applied.
            raise ValueError(
                f"Router connector '{name}' does not take a pool_address: a router routes "
                "across pools rather than executing against one. Name an amm or clmm "
                "connector to pin a pool."
            )
        # See quote_swap: the router model does not declare poolAddress.
        pool_kwargs = {} if trading_type == "router" else {"poolAddress": pool_address or None}
        payload = _body(
            _EXECUTE_SWAP_REQUESTS[trading_type](
                chainNetwork=chain_network,
                connector=name,
                walletAddress=wallet_address,
                baseToken=base_asset,
                quoteToken=quote_asset,
                amount=amount,
                side=side.upper(),
                slippagePct=slippage_pct,
                **pool_kwargs,
            )
        )
        if extra_params:
            # Connector-specific params, merged after the model — see quote_swap.
            payload.update(extra_params)

        return await self._request("POST", f"trading/{trading_type}/execute-swap", json=payload)

    # ============================================
    # Liquidity Operations - CLMM (unified /trading/clmm endpoints)
    # ============================================

    async def clmm_open_position(
        self,
        connector: str,
        chain_network: str,
        wallet_address: str,
        pool_address: str,
        lower_price: float,
        upper_price: float,
        base_token_amount: Optional[float] = None,
        quote_token_amount: Optional[float] = None,
        slippage_pct: Optional[float] = None,
        extra_params: Optional[Dict] = None
    ) -> Dict:
        """Open a NEW CLMM position with initial liquidity"""
        payload = _body(
            ClmmOpenRequest(
                connector=connector,
                chainNetwork=chain_network,
                walletAddress=wallet_address,
                poolAddress=pool_address,
                lowerPrice=lower_price,
                upperPrice=upper_price,
                baseTokenAmount=base_token_amount,
                quoteTokenAmount=quote_token_amount,
                slippagePct=slippage_pct,
            )
        )

        # Connector-specific parameters (e.g. Meteora's strategyType), merged after the
        # model: they are named by the connector rather than the route, so no route
        # schema declares them.
        if extra_params:
            payload.update(extra_params)

        return await self._request("POST", "trading/clmm/open", json=payload)

    async def clmm_add_liquidity(
        self,
        connector: str,
        chain_network: str,
        wallet_address: str,
        position_address: str,
        base_token_amount: Optional[float] = None,
        quote_token_amount: Optional[float] = None,
        slippage_pct: Optional[float] = None,
        extra_params: Optional[Dict] = None
    ) -> Dict:
        """Add more liquidity to an existing CLMM position"""
        payload = _body(
            ClmmAddRequest(
                connector=connector,
                chainNetwork=chain_network,
                walletAddress=wallet_address,
                positionAddress=position_address,
                baseTokenAmount=base_token_amount,
                quoteTokenAmount=quote_token_amount,
                slippagePct=slippage_pct,
            )
        )

        # Connector-specific parameters, merged after the model — see clmm_open_position.
        if extra_params:
            payload.update(extra_params)

        return await self._request("POST", "trading/clmm/add", json=payload)

    async def clmm_close_position(
        self,
        connector: str,
        chain_network: str,
        wallet_address: str,
        position_address: str,
        slippage_pct: Optional[float] = None,
    ) -> Dict:
        """Close a CLMM position completely.

        `slippage_pct` is the withdrawal's tolerance; None uses the connector's
        configured slippagePct. Enforced by orca, uniswap and pancakeswap — the other
        CLMM connectors close with no minimum-amount check, so it changes nothing there.
        """
        return await self._request("POST", "trading/clmm/close", json=_body(
            ClmmCloseRequest(
                connector=connector,
                chainNetwork=chain_network,
                walletAddress=wallet_address,
                positionAddress=position_address,
                slippagePct=slippage_pct,
            )
        ))

    async def clmm_remove_liquidity(
        self,
        connector: str,
        chain_network: str,
        wallet_address: str,
        position_address: str,
        percentage_to_remove: float,
        slippage_pct: Optional[float] = None
    ) -> Dict:
        """Remove liquidity from a CLMM position (partial).

        slippage_pct is only honored by the Orca connector; others ignore it.
        """
        payload = _body(
            ClmmRemoveRequest(
                connector=connector,
                chainNetwork=chain_network,
                walletAddress=wallet_address,
                positionAddress=position_address,
                percentageToRemove=percentage_to_remove,
                slippagePct=slippage_pct,
            )
        )

        return await self._request("POST", "trading/clmm/remove", json=payload)

    async def clmm_position_info(
        self,
        connector: str,
        chain_network: str,
        position_address: str
    ) -> Dict:
        """
        Get CLMM position information including pending fees.

        Note: Gateway returns 500 instead of 404 when position doesn't exist (is closed).
        Callers should treat 500 errors as "position not found/closed".
        """
        # Validate required parameters
        if not connector:
            raise ValueError("connector is required for clmm_position_info")
        if not chain_network:
            raise ValueError("chain_network is required for clmm_position_info")
        if not position_address:
            raise ValueError("position_address is required for clmm_position_info")

        params = _query(
            ClmmPositionInfoRequest(
                connector=connector,
                chainNetwork=chain_network,
                positionAddress=position_address,
            )
        )
        return await self._request("GET", "trading/clmm/position-info", params=params)

    async def clmm_positions_owned(
        self,
        connector: str,
        chain_network: str,
        wallet_address: str
    ) -> List[Dict]:
        """
        Get ALL CLMM positions owned by a wallet on a connector.

        Gateway's /trading/clmm/positions-owned takes no pool filter (its handler
        reads only connector, chainNetwork and walletAddress); callers that care
        about one pool filter the returned rows by their poolAddress field.

        Args:
            connector: CLMM connector (e.g., 'meteora', 'raydium')
            chain_network: Chain and network in format 'chain-network' (e.g., 'solana-mainnet-beta')
            wallet_address: Wallet address to query

        Returns:
            List of position dictionaries with fields like:
            - address: Position NFT address
            - poolAddress: Pool address
            - baseTokenAddress, quoteTokenAddress
            - baseTokenAmount, quoteTokenAmount
            - baseFeeAmount, quoteFeeAmount
            - lowerBinId, upperBinId
            - lowerPrice, upperPrice, price
        """
        params = _query(
            ClmmPositionsOwnedRequest(
                connector=connector,
                chainNetwork=chain_network,
                walletAddress=wallet_address,
            )
        )
        return await self._request("GET", "trading/clmm/positions-owned", params=params)

    async def clmm_quote_position(
        self,
        connector: str,
        chain_network: str,
        pool_address: str,
        lower_price: float,
        upper_price: float,
        base_token_amount: Optional[float] = None,
        quote_token_amount: Optional[float] = None,
        slippage_pct: Optional[float] = None,
    ) -> Dict:
        """Quote the base/quote split a candidate position would take, without signing anything."""
        params = _query(
            ClmmQuoteLiquidityRequest(
                connector=connector,
                chainNetwork=chain_network,
                poolAddress=pool_address,
                lowerPrice=lower_price,
                upperPrice=upper_price,
                baseTokenAmount=base_token_amount,
                quoteTokenAmount=quote_token_amount,
                slippagePct=slippage_pct,
            )
        )
        return await self._request("GET", "trading/clmm/quote-liquidity", params=params)

    async def clmm_create_pool(
        self,
        connector: str,
        chain_network: str,
        wallet_address: str,
        base_token: str,
        quote_token: str,
        initial_price: Optional[float] = None,
        extra_params: Optional[Dict] = None,
    ) -> Dict:
        """Create a new (empty) CLMM pool.

        extra_params carries the connector-specific create params under Gateway's own
        names (binStep, feeBps, ammConfigIndex) and is spread into the payload — the
        same contract as clmm open's extra_params. The router validates keys before
        this is called.
        """
        payload = _body(
            ClmmCreatePoolRequest(
                connector=connector,
                chainNetwork=chain_network,
                walletAddress=wallet_address,
                baseToken=base_token,
                quoteToken=quote_token,
                initialPrice=initial_price,
            )
        )
        if extra_params:
            payload.update(extra_params)
        return await self._request("POST", "trading/clmm/create-pool", json=payload)

    async def clmm_collect_fees(
        self,
        connector: str,
        chain_network: str,
        wallet_address: str,
        position_address: str
    ) -> Dict:
        """Collect accumulated fees from a CLMM position"""
        return await self._request("POST", "trading/clmm/collect-fees", json=_body(
            ClmmCollectFeesRequest(
                connector=connector,
                chainNetwork=chain_network,
                walletAddress=wallet_address,
                positionAddress=position_address,
            )
        ))

    async def clmm_pool_info(
        self,
        connector: str,
        chain_network: str,
        pool_address: str,
        bin_count: int = 0
    ) -> Dict:
        """Get detailed CLMM pool information by pool address.

        bin_count > 0 asks Gateway for the per-tick liquidity distribution
        (`bins`) around the active tick. Meteora always returns its bins and
        ignores the parameter; orca, raydium, uniswap and pancakeswap honour it.
        """
        params = _query(
            ClmmPoolInfoRequest(
                connector=connector,
                chainNetwork=chain_network,
                poolAddress=pool_address,
                binCount=bin_count or None,
            )
        )
        return await self._request("GET", "trading/clmm/pool-info", params=params)

    async def clmm_fetch_pools(
        self,
        connector: str,
        chain_network: str,
        limit: int = 50,
        query: Optional[str] = None,
        sort_by: Optional[str] = None,
        page: Optional[int] = None,
        include_unverified: Optional[bool] = None,
        sort_direction: Optional[str] = None,
        verified_only: Optional[bool] = None
    ) -> Dict:
        """
        Discover CLMM pools from the connector's own listing API (meteora, orca).

        Proxies the DEX's pool-discovery API rather than Gateway's saved pool list. The
        knobs differ per connector — meteora takes page/includeUnverified and a
        "field:direction" sortBy; orca takes sortDirection/verifiedOnly and does not
        paginate — so only keys the caller sets are sent. Gateway drops a knob the chosen
        connector ignores, meaning the wrong connector's knob is a silent no-op.
        """
        params = _query(
            ClmmFetchPoolsRequest(
                chainNetwork=chain_network,
                connector=connector,
                limit=limit,
                query=query or None,
                sortBy=sort_by or None,
                page=page if page else None,
                includeUnverified=include_unverified,
                sortDirection=sort_direction or None,
                verifiedOnly=verified_only,
            )
        )

        return await self._request("GET", "trading/clmm/fetch-pools", params=params)

    # ============================================
    # AMM Liquidity (Meteora DAMM v2, Raydium CPMM, Uniswap/Pancakeswap V2)
    # ============================================
    # All go through the unified trading/amm/* surface (camelCase body/query with connector +
    # chainNetwork). Meteora DAMM v2 positions are NFTs, so remove requires positionAddress and
    # add takes it optionally; fungible-LP AMMs ignore it. positions-owned is meteora-only.

    async def amm_pool_info(self, connector: str, chain_network: str, pool_address: str) -> Dict:
        """Get AMM pool information (reserves, price, base fee)."""
        return await self._request("GET", "trading/amm/pool-info", params=_query(
            AmmPoolInfoRequest(
                connector=connector,
                chainNetwork=chain_network,
                poolAddress=pool_address,
            )
        ))

    async def amm_position_info(
        self, connector: str, chain_network: str, pool_address: str, wallet_address: str
    ) -> Dict:
        """Get a wallet's aggregate liquidity in an AMM pool plus a per-position breakdown (DAMM v2)."""
        return await self._request("GET", "trading/amm/position-info", params=_query(
            AmmPositionInfoRequest(
                connector=connector,
                chainNetwork=chain_network,
                poolAddress=pool_address,
                walletAddress=wallet_address,
            )
        ))

    async def amm_positions_owned(
        self, connector: str, chain_network: str, wallet_address: str
    ) -> List[Dict]:
        """List all of a wallet's AMM positions across pools (meteora only; fungible-LP → Gateway 400)."""
        return await self._request("GET", "trading/amm/positions-owned", params=_query(
            AmmPositionsOwnedRequest(
                connector=connector,
                chainNetwork=chain_network,
                walletAddress=wallet_address,
            )
        ))

    async def amm_quote_liquidity(
        self,
        connector: str,
        chain_network: str,
        pool_address: str,
        base_token_amount: float,
        quote_token_amount: float,
        slippage_pct: Optional[float] = None,
    ) -> Dict:
        """Quote a two-sided liquidity deposit."""
        payload = _query(
            AmmQuoteLiquidityRequest(
                connector=connector,
                chainNetwork=chain_network,
                poolAddress=pool_address,
                baseTokenAmount=base_token_amount,
                quoteTokenAmount=quote_token_amount,
                slippagePct=slippage_pct,
            )
        )
        return await self._request("GET", "trading/amm/quote-liquidity", params=payload)

    async def amm_add_liquidity(
        self,
        connector: str,
        chain_network: str,
        wallet_address: str,
        pool_address: str,
        base_token_amount: float,
        quote_token_amount: float,
        slippage_pct: Optional[float] = None,
        position_address: Optional[str] = None,
    ) -> Dict:
        """Add two-sided liquidity. For meteora, position_address adds to that NFT position (omit = new)."""
        payload = _body(
            AmmAddRequest(
                connector=connector,
                chainNetwork=chain_network,
                walletAddress=wallet_address,
                poolAddress=pool_address,
                baseTokenAmount=base_token_amount,
                quoteTokenAmount=quote_token_amount,
                slippagePct=slippage_pct,
                positionAddress=position_address,
            )
        )
        return await self._request("POST", "trading/amm/add", json=payload)

    async def amm_remove_liquidity(
        self,
        connector: str,
        chain_network: str,
        wallet_address: str,
        pool_address: str,
        percentage_to_remove: float,
        slippage_pct: Optional[float] = None,
        position_address: Optional[str] = None,
    ) -> Dict:
        """Remove liquidity. Gateway requires position_address for meteora (DAMM v2 NFT positions)."""
        payload = _body(
            AmmRemoveRequest(
                connector=connector,
                chainNetwork=chain_network,
                walletAddress=wallet_address,
                poolAddress=pool_address,
                percentageToRemove=percentage_to_remove,
                slippagePct=slippage_pct,
                positionAddress=position_address,
            )
        )
        return await self._request("POST", "trading/amm/remove", json=payload)

    async def amm_create_pool(
        self,
        connector: str,
        chain_network: str,
        wallet_address: str,
        base_token: str,
        quote_token: str,
        base_token_amount: float,
        quote_token_amount: Optional[float] = None,
        initial_price: Optional[float] = None,
        slippage_pct: Optional[float] = None,
        extra_params: Optional[Dict] = None,
    ) -> Dict:
        """Create and seed a new AMM pool.

        slippage_pct is the seeding slippage (uniswap/pancakeswap only); omitted, the
        connector's configured slippagePct applies. extra_params carries the
        connector-specific create params under Gateway's own names (configAddress,
        ammConfigIndex) and is spread into the payload — the same contract as clmm
        open's extra_params. The router validates keys before this is called.
        """
        # Seed price: at most one of quoteTokenAmount / initialPrice; Gateway falls back
        # to market price when neither is given, which _body expresses by dropping None.
        payload = _body(
            AmmCreatePoolRequest(
                connector=connector,
                chainNetwork=chain_network,
                walletAddress=wallet_address,
                baseToken=base_token,
                quoteToken=quote_token,
                baseTokenAmount=base_token_amount,
                quoteTokenAmount=quote_token_amount,
                initialPrice=initial_price,
                slippagePct=slippage_pct,
            )
        )
        if extra_params:
            payload.update(extra_params)
        return await self._request("POST", "trading/amm/create-pool", json=payload)

    # ============================================
    # Transaction Polling
    # ============================================

    async def poll_transaction(
        self,
        network_id: str,
        tx_hash: str,
    ) -> Optional[Dict]:
        """
        Poll transaction status on blockchain.

        Args:
            network_id: Network ID in format 'chain-network' (e.g., 'solana-mainnet-beta', 'ethereum-mainnet')
            tx_hash: Transaction hash/signature

        Returns:
            Transaction status dict with fields:
            - txStatus: 1 confirmed, 0 pending, -1 failed, -2 not found
              (-2 is terminal on Solana once the blockhash expires — treat it
              as dropped, not merely pending)
            - fee: Transaction fee amount
            - error: Parsed error message if transaction failed (e.g., "SLIPPAGE_EXCEEDED (0x1771): ...")
            - txData: Full transaction data including meta.err
            Returns None if Gateway is unavailable or request fails.
        """
        try:
            # Split network_id into chain and network
            parts = network_id.split('-', 1)
            if len(parts) != 2:
                logger.error(f"Invalid network_id format: {network_id}. Expected 'chain-network'")
                return None

            chain, network = parts

            payload = {
                "network": network,
                "signature": tx_hash
            }

            return await self._request("POST", f"chains/{chain}/poll", json=payload)
        except Exception as e:
            logger.error(f"Error polling transaction {tx_hash}: {e}")
            return None
