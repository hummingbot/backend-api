import re
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from deps import get_accounts_service, get_gateway_service, require_gateway_online
from models import AddPoolRequest, AddTokenRequest, GatewayConfig, GatewayStatus, UpdateApiKeysRequest
from services.accounts_service import AccountsService
from services.gateway_client import GatewayError, check_gateway_error
from services.gateway_service import GatewayService

router = APIRouter(tags=["Gateway"], prefix="/gateway")


def camel_to_snake(name: str) -> str:
    """Convert camelCase to snake_case"""
    name = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', name).lower()


def snake_to_camel(name: str) -> str:
    """
    Convert snake_case to camelCase, handling common acronyms.

    Special cases:
    - url -> URL
    - cu -> CU (compute units)
    - id -> ID
    - api -> API
    - rpc -> RPC
    """
    # Map of acronyms that should be uppercase
    acronyms = {'url', 'cu', 'id', 'api', 'rpc', 'uri'}

    components = name.split('_')

    # Process each component
    result_parts = [components[0]]  # First component stays lowercase

    for component in components[1:]:
        if component.lower() in acronyms:
            # Uppercase acronyms
            result_parts.append(component.upper())
        else:
            # Title case for normal words
            result_parts.append(component.title())

    return ''.join(result_parts)


def normalize_gateway_response(data: Dict) -> Dict:
    """
    Normalize Gateway response data to Python conventions.
    - Converts camelCase to snake_case
    - Maps baseSymbol -> base, quoteSymbol -> quote
    - Creates trading_pair field
    """
    if isinstance(data, dict):
        normalized = {}
        for key, value in data.items():
            # Handle special mappings
            if key == "baseSymbol":
                normalized["base"] = value
            elif key == "quoteSymbol":
                normalized["quote"] = value
            else:
                # Convert to snake_case
                new_key = camel_to_snake(key)
                # Recursively normalize nested dicts/lists
                if isinstance(value, dict):
                    normalized[new_key] = normalize_gateway_response(value)
                elif isinstance(value, list):
                    normalized[new_key] = [normalize_gateway_response(item) if isinstance(item, dict) else item for item in value]
                else:
                    normalized[new_key] = value

        # Create trading_pair if we have base and quote
        if "base" in normalized and "quote" in normalized:
            normalized["trading_pair"] = f"{normalized['base']}-{normalized['quote']}"

        return normalized
    return data


# ============================================
# Container Management
# ============================================

@router.get("/status", response_model=GatewayStatus)
async def get_gateway_status(gateway_service: GatewayService = Depends(get_gateway_service)):
    """Get Gateway container status."""
    return gateway_service.get_status()


@router.post("/start")
async def start_gateway(
    config: GatewayConfig,
    gateway_service: GatewayService = Depends(get_gateway_service)
):
    """Start Gateway container."""
    result = gateway_service.start(config)
    if not result["success"]:
        if "already running" in result["message"]:
            raise HTTPException(status_code=400, detail=result["message"])
        raise HTTPException(status_code=500, detail=result["message"])
    return result


@router.post("/stop")
async def stop_gateway(gateway_service: GatewayService = Depends(get_gateway_service)):
    """Stop Gateway container."""
    result = gateway_service.stop()
    if not result["success"]:
        if "not found" in result["message"]:
            raise HTTPException(status_code=404, detail=result["message"])
        raise HTTPException(status_code=500, detail=result["message"])
    return result


@router.post("/restart")
async def restart_gateway(
    config: Optional[GatewayConfig] = None,
    gateway_service: GatewayService = Depends(get_gateway_service)
):
    """
    Restart Gateway container.

    If config is provided, the container will be removed and recreated with new configuration.
    If no config is provided, the container will be stopped and started with existing configuration.
    """
    result = gateway_service.restart(config)
    if not result["success"]:
        if "not found" in result["message"]:
            raise HTTPException(status_code=404, detail=result["message"])
        raise HTTPException(status_code=500, detail=result["message"])
    return result


@router.get("/logs")
async def get_gateway_logs(
    tail: int = Query(default=100, ge=1, le=10000),
    gateway_service: GatewayService = Depends(get_gateway_service)
):
    """Get Gateway container logs."""
    result = gateway_service.get_logs(tail)
    if not result["success"]:
        if "not found" in result["message"]:
            raise HTTPException(status_code=404, detail=result["message"])
        raise HTTPException(status_code=500, detail=result["message"])
    return result


# ============================================
# Connectors
# ============================================

@router.get("/connectors", dependencies=[Depends(require_gateway_online)])
async def list_connectors(accounts_service: AccountsService = Depends(get_accounts_service)) -> Dict:
    """
    List all available DEX connectors with their configurations.

    Returns connector details including name, trading types, chain, and networks.
    All fields normalized to snake_case.
    """
    try:
        result = check_gateway_error(await accounts_service.gateway_client.get_connectors())
        return normalize_gateway_response(result)

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error listing connectors: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error listing connectors: {str(e)}")


@router.get("/connectors/{connector_name}", dependencies=[Depends(require_gateway_online)])
async def get_connector_config(
    connector_name: str,
    accounts_service: AccountsService = Depends(get_accounts_service)
) -> Dict:
    """
    Get configuration for a specific DEX connector.

    Args:
        connector_name: Connector name (e.g., 'meteora', 'raydium')
    """
    try:
        result = check_gateway_error(await accounts_service.gateway_client.get_config(connector_name))
        return normalize_gateway_response(result)

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error getting connector config: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting connector config: {str(e)}")


@router.post("/connectors/{connector_name}", dependencies=[Depends(require_gateway_online)])
async def update_connector_config(
    connector_name: str,
    config_updates: Dict,
    accounts_service: AccountsService = Depends(get_accounts_service)
) -> Dict:
    """
    Update configuration for a DEX connector.

    Args:
        connector_name: Connector name (e.g., 'meteora', 'raydium')
        config_updates: Dict with path-value pairs to update.
                       Keys can be in snake_case (e.g., {"slippage_pct": 0.5})
                       or camelCase (e.g., {"slippagePct": 0.5})
    """
    try:
        results = []
        for path, value in config_updates.items():
            # Convert snake_case to camelCase if needed
            camel_path = snake_to_camel(path) if '_' in path else path
            result = check_gateway_error(
                await accounts_service.gateway_client.update_config(connector_name, camel_path, value)
            )
            results.append(result)

        return {
            "success": True,
            "message": f"Updated {len(results)} config param(s) for {connector_name}. Restart Gateway.",
            "restart_required": True,
            "restart_endpoint": "POST /gateway/restart",
            "results": results
        }

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error updating connector config: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating connector config: {str(e)}")


# ============================================
# API Keys
# ============================================

@router.get("/apiKeys", dependencies=[Depends(require_gateway_online)])
async def get_api_keys(accounts_service: AccountsService = Depends(get_accounts_service)) -> Dict:
    """
    Get all configured API keys from Gateway.

    Returns a dict mapping provider name to API key value.
    Example response:
    {
        "helius": "46951ec2-16af-4fc0-a5df-970b0eb925b7",
        "infura": "920646320ec3463fa1b5235be9fa48d3",
        "coingecko": "CG-Rw786jTpNmV1MvRrqpDAHR6r",
        "etherscan": ""
    }
    """
    try:
        return check_gateway_error(await accounts_service.gateway_client.get_api_keys())

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error getting API keys: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting API keys: {str(e)}")


@router.post("/apiKeys", dependencies=[Depends(require_gateway_online)])
async def update_api_keys(
    request: UpdateApiKeysRequest,
    accounts_service: AccountsService = Depends(get_accounts_service)
) -> Dict:
    """
    Update API keys in Gateway configuration.

    Args:
        request: Contains api_keys dict mapping provider name to API key value

    Example request:
    {
        "api_keys": {
            "helius": "new-api-key-value",
            "infura": "another-api-key"
        }
    }

    Note: After updating API keys, restart Gateway for changes to take effect.
    """
    try:
        results = await accounts_service.gateway_client.update_api_keys(request.api_keys)

        # A None result means the request never reached Gateway (connection
        # error mid-batch) — that is a failure, not a success to filter out.
        if any(r is None for r in results):
            raise HTTPException(
                status_code=503,
                detail="Gateway became unreachable while updating API keys; not all keys were applied",
            )
        errors = [r for r in results if "error" in r]
        if errors:
            raise HTTPException(status_code=400, detail=f"Failed to update some API keys: {errors}")

        return {
            "success": True,
            "message": f"Updated {len(request.api_keys)} API key(s). Restart Gateway for changes to take effect.",
            "restart_required": True,
            "restart_endpoint": "POST /gateway/restart",
            "updated_keys": list(request.api_keys.keys())
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating API keys: {str(e)}")


# ============================================
# Chains (Networks) and Tokens
# ============================================

@router.get("/chains", dependencies=[Depends(require_gateway_online)])
async def list_chains(accounts_service: AccountsService = Depends(get_accounts_service)) -> Dict:
    """
    List all available blockchain chains and their networks.

    This also serves as the networks list endpoint.
    """
    try:
        return check_gateway_error(await accounts_service.gateway_client.get_chains())

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error listing chains: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error listing chains: {str(e)}")


# ============================================
# Pools (Legacy - use /networks/{network_id}/pools instead)
# ============================================

@router.get("/pools", deprecated=True, dependencies=[Depends(require_gateway_online)])
async def list_pools_legacy(
    connector_name: str = Query(description="DEX connector (e.g., 'meteora', 'raydium')"),
    network: str = Query(description="Network (e.g., 'mainnet-beta')"),
    accounts_service: AccountsService = Depends(get_accounts_service)
) -> List[Dict]:
    """
    [DEPRECATED] Use GET /gateway/networks/{network_id}/pools instead.

    List all liquidity pools for a connector and network.
    """
    try:
        # Determine chain from connector (legacy behavior). Solana routers
        # (jupiter/dflow/okx/titan) must map to solana too, or this returns the
        # (empty) ethereum pool list for them.
        solana_connectors = {"raydium", "meteora", "orca", "pancakeswap-sol", "jupiter", "dflow", "okx", "titan"}
        chain = "solana" if connector_name in solana_connectors else "ethereum"

        pools = check_gateway_error(
            await accounts_service.gateway_client.get_pools(chain, network, connector=connector_name)
        )

        if not pools:
            return []

        # Normalize each pool
        normalized_pools = [normalize_gateway_response(pool) for pool in pools]
        return normalized_pools

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error getting pools: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting pools: {str(e)}")


# ============================================
# Networks (Primary Endpoints)
# ============================================

@router.get("/networks", dependencies=[Depends(require_gateway_online)])
async def list_networks(accounts_service: AccountsService = Depends(get_accounts_service)) -> Dict:
    """
    List all available networks across all chains.

    Returns a flattened list of network IDs in the format 'chain-network'.
    This is the primary interface for network discovery.
    """
    try:
        chains_result = check_gateway_error(await accounts_service.gateway_client.get_chains())

        # Flatten chain-network combinations into network IDs
        networks = []
        if "chains" in chains_result and isinstance(chains_result["chains"], list):
            for chain_item in chains_result["chains"]:
                chain = chain_item.get("chain")
                chain_networks = chain_item.get("networks", [])
                for network in chain_networks:
                    network_id = f"{chain}-{network}"
                    networks.append({
                        "network_id": network_id,
                        "chain": chain,
                        "network": network
                    })

        return {
            "networks": networks,
            "count": len(networks)
        }

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error listing networks: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error listing networks: {str(e)}")


@router.get("/networks/{network_id}", dependencies=[Depends(require_gateway_online)])
async def get_network_config(
    network_id: str,
    accounts_service: AccountsService = Depends(get_accounts_service)
) -> Dict:
    """
    Get configuration for a specific network.

    Args:
        network_id: Network ID in format 'chain-network' (e.g., 'solana-mainnet-beta', 'ethereum-mainnet')

    Example: GET /gateway/networks/solana-mainnet-beta
    """
    try:
        result = check_gateway_error(await accounts_service.gateway_client.get_config(network_id))
        return normalize_gateway_response(result)

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error getting network config: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting network config: {str(e)}")


@router.post("/networks/{network_id}", dependencies=[Depends(require_gateway_online)])
async def update_network_config(
    network_id: str,
    config_updates: Dict,
    accounts_service: AccountsService = Depends(get_accounts_service)
) -> Dict:
    """
    Update configuration for a specific network.

    Args:
        network_id: Network ID in format 'chain-network' (e.g., 'solana-mainnet-beta')
        config_updates: Dict with path-value pairs to update.
                       Keys can be in snake_case (e.g., {"node_url": "https://..."})
                       or camelCase (e.g., {"nodeURL": "https://..."})

    Example: POST /gateway/networks/solana-mainnet-beta
    """
    try:
        results = []
        for path, value in config_updates.items():
            # Convert snake_case to camelCase if needed
            camel_path = snake_to_camel(path) if '_' in path else path
            result = check_gateway_error(
                await accounts_service.gateway_client.update_config(network_id, camel_path, value)
            )
            results.append(result)

        return {
            "success": True,
            "message": f"Updated {len(results)} config parameter(s) for {network_id}. Restart Gateway.",
            "restart_required": True,
            "restart_endpoint": "POST /gateway/restart",
            "results": results
        }

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error updating network config: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating network config: {str(e)}")


@router.get("/networks/{network_id}/tokens", dependencies=[Depends(require_gateway_online)])
async def get_network_tokens(
    network_id: str,
    search: Optional[str] = Query(default=None),
    accounts_service: AccountsService = Depends(get_accounts_service)
) -> Dict:
    """
    Get available tokens for a network.

    Args:
        network_id: Network ID in format 'chain-network' (e.g., 'solana-mainnet-beta')
        search: Filter tokens by symbol, name, or address (case-insensitive substring)

    Example: GET /gateway/networks/solana-mainnet-beta/tokens?search=USDC
    """
    try:
        # Parse network_id into chain and network
        parts = network_id.split('-', 1)
        if len(parts) != 2:
            raise HTTPException(status_code=400, detail=f"Invalid network_id format: '{network_id}'. Use 'chain-network'")

        chain, network = parts
        result = check_gateway_error(await accounts_service.gateway_client.get_tokens(chain, network))

        # Apply search filter. Address is matched as well as symbol and name, because
        # the address is what a caller usually has: a user pastes a mint, and a search
        # that only knows symbols answers "no such token" for one that is registered.
        # That false negative reads as "not in Gateway" and invites a duplicate add.
        # Gateway's own /tokens filter already matches all three (token-service.ts
        # listTokens); this re-implements it here because the search term is not
        # forwarded, so the two have to agree by hand.
        if search and "tokens" in result:
            search_lower = search.lower()
            result["tokens"] = [
                token for token in result["tokens"]
                if (search_lower in token.get("symbol", "").lower() or
                    search_lower in token.get("name", "").lower() or
                    search_lower in token.get("address", "").lower())
            ]

        return result

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error getting network tokens: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting network tokens: {str(e)}")


@router.post("/networks/{network_id}/tokens", dependencies=[Depends(require_gateway_online)])
async def add_network_token(
    network_id: str,
    token_request: AddTokenRequest,
    accounts_service: AccountsService = Depends(get_accounts_service)
) -> Dict:
    """
    Add a custom token to Gateway's token list for a specific network.

    Args:
        network_id: Network ID in format 'chain-network' (e.g., 'solana-mainnet-beta', 'ethereum-mainnet')
        token_request: Token details (address, symbol, name, decimals)

    Example: POST /gateway/networks/ethereum-mainnet/tokens
    {
        "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "symbol": "USDC",
        "name": "USD Coin",
        "decimals": 6
    }

    No Gateway restart is needed: the token list is read off disk per request, so
    the token is live as soon as this returns.
    """
    try:
        # Parse network_id into chain and network
        parts = network_id.split('-', 1)
        if len(parts) != 2:
            raise HTTPException(status_code=400, detail=f"Invalid network_id format: '{network_id}'. Use 'chain-network'")

        chain, network = parts

        # Use symbol as name if name is not provided
        token_name = token_request.name if token_request.name else token_request.symbol

        check_gateway_error(await accounts_service.gateway_client.add_token(
            chain=chain,
            network=network,
            address=token_request.address,
            symbol=token_request.symbol,
            name=token_name,
            decimals=token_request.decimals
        ))

        return {
            "success": True,
            "message": f"Token {token_request.symbol} added to {network_id}.",
            "token": {
                "symbol": token_request.symbol,
                "address": token_request.address,
                "network_id": network_id
            }
        }

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Failed to add token: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error adding token: {str(e)}")


@router.post("/networks/{network_id}/tokens/save/{token_address}", dependencies=[Depends(require_gateway_online)])
async def save_network_token(
    network_id: str,
    token_address: str,
    accounts_service: AccountsService = Depends(get_accounts_service)
) -> Dict:
    """
    Save a token by address - auto-fetches token info from GeckoTerminal.

    This is the simplest way to add a token. Just provide the address and
    the API will fetch symbol, name, and decimals automatically.

    Args:
        network_id: Network ID in format 'chain-network' (e.g., 'solana-mainnet-beta', 'ethereum-mainnet')
        token_address: Token contract address

    Example: POST /gateway/networks/solana-mainnet-beta/tokens/save/9QFfgxdSqH5zT7j6rZb1y6SZhw2aFtcQu2r6BuYpump
    """
    try:
        # Parse network_id into chain and network
        parts = network_id.split('-', 1)
        if len(parts) != 2:
            raise HTTPException(status_code=400, detail=f"Invalid network_id format: '{network_id}'. Use 'chain-network'")

        chain, network = parts

        result = check_gateway_error(await accounts_service.gateway_client.save_token(
            chain=chain,
            network=network,
            token_address=token_address
        ))

        token_info = result.get("token", {})
        return {
            "success": True,
            "message": result.get("message", f"Token saved to {network_id}"),
            "token": {
                "symbol": token_info.get("symbol"),
                "address": token_info.get("address", token_address),
                "decimals": token_info.get("decimals"),
                "name": token_info.get("name"),
                "network_id": network_id
            }
        }

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Failed to save token: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error saving token: {str(e)}")


@router.delete("/networks/{network_id}/tokens/{token_address}", dependencies=[Depends(require_gateway_online)])
async def delete_network_token(
    network_id: str,
    token_address: str,
    accounts_service: AccountsService = Depends(get_accounts_service)
) -> Dict:
    """
    Delete a custom token from Gateway's token list for a specific network.

    Args:
        network_id: Network ID in format 'chain-network' (e.g., 'solana-mainnet-beta', 'ethereum-mainnet')
        token_address: Token contract address to delete

    Example: DELETE /gateway/networks/solana-mainnet-beta/tokens/9QFfgxdSqH5zT7j6rZb1y6SZhw2aFtcQu2r6BuYpump

    No Gateway restart is needed: the token list is read off disk per request, so
    the deletion is live as soon as this returns.
    """
    try:
        # Parse network_id into chain and network
        parts = network_id.split('-', 1)
        if len(parts) != 2:
            raise HTTPException(status_code=400, detail=f"Invalid network_id format: '{network_id}'. Use 'chain-network'")

        chain, network = parts

        check_gateway_error(await accounts_service.gateway_client.delete_token(
            chain=chain,
            network=network,
            token_address=token_address
        ))

        return {
            "success": True,
            "message": f"Token {token_address} deleted from {network_id}.",
            "token_address": token_address,
            "network_id": network_id
        }

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Failed to delete token: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting token: {str(e)}")


# ============================================
# Network Pools
# ============================================

@router.get("/networks/{network_id}/pools", dependencies=[Depends(require_gateway_online)])
async def get_network_pools(
    network_id: str,
    connector: Optional[str] = Query(default=None, description="Filter by connector (e.g., 'raydium', 'meteora')"),
    pool_type: Optional[str] = Query(default=None, description="Filter by type ('amm' or 'clmm')"),
    search: Optional[str] = Query(default=None, description="Search by trading pair or address"),
    accounts_service: AccountsService = Depends(get_accounts_service)
) -> Dict:
    """
    Get available pools for a network.

    Args:
        network_id: Network ID in format 'chain-network' (e.g., 'solana-mainnet-beta')
        connector: Optional filter by connector (e.g., 'raydium', 'meteora', 'uniswap')
        pool_type: Optional filter by type ('amm' or 'clmm')
        search: Optional search by trading pair (e.g., 'SOL-USDC') or pool address

    Example: GET /gateway/networks/solana-mainnet-beta/pools?connector=raydium&type=clmm
    """
    try:
        # Parse network_id into chain and network
        parts = network_id.split('-', 1)
        if len(parts) != 2:
            raise HTTPException(status_code=400, detail=f"Invalid network_id format: '{network_id}'. Use 'chain-network'")

        chain, network = parts
        pools = check_gateway_error(await accounts_service.gateway_client.get_pools(
            chain=chain,
            network=network,
            connector=connector,
            pool_type=pool_type,
            search=search
        ))

        # Normalize each pool
        normalized_pools = [normalize_gateway_response(pool) for pool in pools] if pools else []

        return {
            "pools": normalized_pools,
            "count": len(normalized_pools),
            "network_id": network_id
        }

    except HTTPException:
        raise
    except GatewayError as e:
        raise HTTPException(status_code=e.status, detail=f"Gateway error getting network pools: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting network pools: {str(e)}")


@router.post("/networks/{network_id}/pools", dependencies=[Depends(require_gateway_online)])
async def add_network_pool(
    network_id: str,
    pool_request: AddPoolRequest,
    accounts_service: AccountsService = Depends(get_accounts_service)
) -> Dict:
    """
    Add a custom pool to Gateway's pool list for a specific network.

    Args:
        network_id: Network ID in format 'chain-network' (e.g., 'solana-mainnet-beta', 'ethereum-mainnet')
        pool_request: Pool details (connector, type, base, quote, address, etc.)

    Example: POST /gateway/networks/solana-mainnet-beta/pools
    {
        "connector_name": "raydium",
        "type": "clmm",
        "base": "SOL",
        "quote": "USDC",
        "address": "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2",
        "base_address": "So11111111111111111111111111111111111111112",
        "quote_address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "fee_pct": 0.25
    }

    No Gateway restart is needed: the pool list is read off disk per request, so the
    pool is listed and priced as soon as this returns.
    """
    try:
        # Parse network_id into chain and network
        parts = network_id.split('-', 1)
        if len(parts) != 2:
            raise HTTPException(status_code=400, detail=f"Invalid network_id format: '{network_id}'. Use 'chain-network'")

        chain, network = parts

        result = await accounts_service.gateway_client.add_pool(
            chain=chain,
            network=network,
            connector=pool_request.connector_name,
            pool_type=pool_request.type,
            address=pool_request.address,
            base_symbol=pool_request.base,
            quote_symbol=pool_request.quote,
            base_token_address=pool_request.base_address,
            quote_token_address=pool_request.quote_address,
            fee_pct=pool_request.fee_pct
        )

        if result is None:
            raise HTTPException(status_code=502, detail="Failed to add pool: Gateway returned no response")

        if "error" in result:
            status = result.get("status", 400)
            raise HTTPException(status_code=status, detail=f"Failed to add pool: {result.get('error')}")

        trading_pair = f"{pool_request.base}-{pool_request.quote}"
        return {
            "success": True,
            "message": f"Pool {trading_pair} added to {network_id}.",
            "pool": {
                "trading_pair": trading_pair,
                "connector": pool_request.connector_name,
                "type": pool_request.type,
                "address": pool_request.address,
                "network_id": network_id
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error adding pool: {str(e)}")


@router.post("/networks/{network_id}/pools/save/{pool_address}", dependencies=[Depends(require_gateway_online)])
async def save_network_pool(
    network_id: str,
    pool_address: str,
    connector: Optional[str] = Query(default=None),
    type: Optional[str] = Query(default=None),
    accounts_service: AccountsService = Depends(get_accounts_service)
) -> Dict:
    """
    Save a pool by address, auto-adding any missing tokens.

    Gateway only needs GeckoTerminal to answer one question: which DEX does this
    address belong to, and is it amm or clmm. The pool's base, quote and fee always
    come from the connector. Pass connector and type to answer that directly and skip
    the lookup — which is what a caller holding an LP provider config like
    'meteora/clmm' can always do, and what makes this work for a token or pool
    GeckoTerminal has not indexed.

    Args:
        network_id: Network ID in format 'chain-network' (e.g., 'solana-mainnet-beta')
        pool_address: Pool contract address
        connector: DEX connector ('meteora', 'raydium', 'orca', 'uniswap'). With type.
        type: Pool type, 'amm' or 'clmm'. With connector.

    Example: POST /gateway/networks/solana-mainnet-beta/pools/save/2sf5NYcY...?connector=meteora&type=clmm
    """
    try:
        if (connector is None) != (type is None):
            raise HTTPException(
                status_code=400,
                detail="connector and type must be given together, or both omitted",
            )
        if type is not None and type not in ("amm", "clmm"):
            raise HTTPException(status_code=400, detail=f"Invalid type '{type}': use 'amm' or 'clmm'")

        result = await accounts_service.gateway_client.save_pool(
            chain_network=network_id,
            address=pool_address,
            connector=connector,
            pool_type=type,
        )

        if result is None:
            raise HTTPException(status_code=502, detail="Failed to save pool: Gateway returned no response")

        if "error" in result:
            raise HTTPException(status_code=400, detail=f"Failed to save pool: {result.get('error')}")

        pool = result.get("pool", {})
        tokens_added = result.get("tokensAdded", [])

        return {
            "success": True,
            "message": result.get("message", f"Pool saved to {network_id}"),
            "pool": normalize_gateway_response(pool),
            "tokens_added": tokens_added,
            "network_id": network_id
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error saving pool: {str(e)}")


@router.delete("/networks/{network_id}/pools/{pool_address}", dependencies=[Depends(require_gateway_online)])
async def delete_network_pool(
    network_id: str,
    pool_address: str,
    accounts_service: AccountsService = Depends(get_accounts_service)
) -> Dict:
    """
    Delete a pool from Gateway's pool list for a specific network.

    Args:
        network_id: Network ID in format 'chain-network' (e.g., 'solana-mainnet-beta', 'ethereum-mainnet')
        pool_address: Pool contract address to delete

    Example: DELETE /gateway/networks/solana-mainnet-beta/pools/58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2

    No Gateway restart is needed: the pool list is read off disk per request, so the
    deletion is live as soon as this returns.
    """
    try:
        # Parse network_id into chain and network
        parts = network_id.split('-', 1)
        if len(parts) != 2:
            raise HTTPException(status_code=400, detail=f"Invalid network_id format: '{network_id}'. Use 'chain-network'")

        chain, network = parts

        result = await accounts_service.gateway_client.delete_pool(
            chain=chain,
            network=network,
            address=pool_address
        )

        if result is None:
            raise HTTPException(status_code=400, detail="Failed to delete pool - no response from Gateway")

        if "error" in result:
            raise HTTPException(status_code=400, detail=f"Failed to delete pool: {result.get('error')}")

        return {
            "success": True,
            "message": f"Pool {pool_address} deleted from {network_id}.",
            "pool_address": pool_address,
            "network_id": network_id
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting pool: {str(e)}")
