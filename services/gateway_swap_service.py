"""Persistence for DEX swaps executed through the ``/gateway/swap*`` routes.

The shape of a ``gateway_swaps`` row, the pagination envelope over a swap search and
the "record it but never fail the swap over it" policy all live here rather than in
the handlers, which only decide what to answer the caller.
"""
import logging
from decimal import Decimal
from typing import Any, Dict, Optional

from database.repositories import GatewaySwapRepository
from services.repository_service import RepositoryService

logger = logging.getLogger(__name__)


class GatewaySwapService(RepositoryService):
    """Swap history persistence for the swap routes."""

    repository_class = GatewaySwapRepository

    async def record_swap(
        self,
        *,
        transaction_hash: str,
        network: str,
        connector: str,
        wallet_address: str,
        trading_pair: str,
        base_token: str,
        quote_token: str,
        side: str,
        input_amount: Decimal,
        output_amount: Decimal,
        price: Decimal,
        slippage_pct: Optional[Decimal],
        gas_fee: Optional[Decimal],
        gas_token: Optional[str],
        status: str,
        pool_address: Optional[str],
    ) -> None:
        """Record a settled swap. Best-effort: the swap already happened on-chain."""
        async def _fn(swap_repo):
            swap_data = {
                "transaction_hash": transaction_hash,
                "network": network,
                # Store the base venue name: a swap on "jupiter" and one on
                # "jupiter/router" are the same venue and must file together.
                "connector": connector.split("/")[0],
                "wallet_address": wallet_address,
                "trading_pair": trading_pair,
                "base_token": base_token,
                "quote_token": quote_token,
                "side": side,
                "input_amount": float(input_amount),
                "output_amount": float(output_amount),
                "price": float(price),
                "slippage_pct": float(slippage_pct) if slippage_pct is not None else None,
                "gas_fee": float(gas_fee) if gas_fee is not None else None,
                "gas_token": gas_token,
                "status": status,
                # Set by the pool-scoped routes, which resolve exactly one pool; a
                # router picks its own path across pools and leaves it unset.
                "pool_address": pool_address
            }

            await swap_repo.create_swap(swap_data)
            logger.info(f"Recorded swap in database: {transaction_hash} (status: {status})")

        await self._in_repo_best_effort(
            _fn, error_message="Error recording swap in database")

    async def get_swap(self, transaction_hash: str) -> Optional[Dict[str, Any]]:
        """One swap by transaction hash, or None when there is no such row.

        None rather than an exception: the caller turns "no row" into its own 404,
        which a swallowed database error must never be mistaken for.
        """
        async def _fn(swap_repo):
            swap = await swap_repo.get_swap_by_tx_hash(transaction_hash)
            return swap_repo.to_dict(swap) if swap else None

        return await self._in_repo(_fn)

    async def search_swaps(
        self,
        *,
        network: Optional[str] = None,
        connector: Optional[str] = None,
        wallet_address: Optional[str] = None,
        trading_pair: Optional[str] = None,
        status: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Filtered swap history, as the endpoint's paginated envelope."""
        # Validate limit
        if limit > 1000:
            limit = 1000

        async def _fn(swap_repo):
            swaps = await swap_repo.get_swaps(
                network=network,
                connector=connector,
                wallet_address=wallet_address,
                trading_pair=trading_pair,
                status=status,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                offset=offset
            )

            # Get total count for pagination (simplified - actual count would need separate query)
            has_more = len(swaps) == limit

            return {
                "data": [swap_repo.to_dict(swap) for swap in swaps],
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "has_more": has_more,
                    "total_count": len(swaps) + offset if not has_more else None
                }
            }

        return await self._in_repo(_fn)

    async def get_swaps_summary(
        self,
        *,
        network: Optional[str] = None,
        wallet_address: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Aggregate swap statistics (volume, fees, success rate)."""
        async def _fn(swap_repo):
            return await swap_repo.get_swaps_summary(
                network=network,
                wallet_address=wallet_address,
                start_time=start_time,
                end_time=end_time
            )

        return await self._in_repo(_fn)
