"""Persistence for AMM liquidity writes and the DAMM v2 positions that carry identity.

Everything the ``/gateway/amm/*`` routes know about the database lives here: the shape
of a ``gateway_amm_events`` row, the shape of a ``gateway_amm_positions`` row and the
bookkeeping an add or a remove applies to it, and the pagination envelopes over both
searches. The handlers keep only what is genuinely theirs — reading Gateway's response
and deciding what to answer the caller.

Reads propagate their errors; the after-the-fact recording of an operation that is
already on-chain is best-effort, per ``RepositoryService``.
"""
import logging
from decimal import Decimal
from typing import Any, Dict, Optional

from database.repositories import GatewayAMMRepository
from services.gateway_client import get_native_gas_token
from services.repository_service import RepositoryService

logger = logging.getLogger(__name__)


class GatewayAMMService(RepositoryService):
    """AMM event and position persistence for the AMM routes."""

    repository_class = GatewayAMMRepository

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def record_position_add(
        self,
        *,
        gateway_client,
        position_address: str,
        pool_address: str,
        connector: str,
        network: str,
        wallet_address: str,
        base_amount_added: Any,
        quote_amount_added: Any,
        position_rent: Any,
        price: Optional[float],
        base_token_address: str,
        quote_token_address: str,
    ) -> None:
        """Create or top up the DAMM v2 position row for a confirmed add."""
        base_added = base_amount_added or 0
        quote_added = quote_amount_added or 0

        async def _fn(repo):
            existing = await repo.get_position_by_address(position_address)
            if existing:
                await repo.add_to_position_amounts(
                    position_address=position_address,
                    base_delta=Decimal(str(base_added)),
                    quote_delta=Decimal(str(quote_added)),
                    entry_price=Decimal(str(price)) if price else None,
                )
                if existing.status == "CLOSED":
                    existing.status = "OPEN"
                    existing.closed_at = None
            else:
                chain, network_name = network.split("-", 1)
                base_symbol = await gateway_client.resolve_token_symbol(
                    chain, network_name, base_token_address)
                quote_symbol = await gateway_client.resolve_token_symbol(
                    chain, network_name, quote_token_address)
                await repo.create_position({
                    "position_address": position_address,
                    "pool_address": pool_address,
                    "connector": connector.split("/")[0],
                    "network": network,
                    "wallet_address": wallet_address,
                    "base_token": base_symbol,
                    "quote_token": quote_symbol,
                    "trading_pair": f"{base_symbol}-{quote_symbol}",
                    "initial_base_token_amount": base_added,
                    "initial_quote_token_amount": quote_added,
                    "base_token_amount": base_added,
                    "quote_token_amount": quote_added,
                    # Rent is locked, not spent: the chain returns it when the position
                    # account closes, and Gateway reports it separately for exactly that
                    # reason. Recorded here so the close can be checked against it — a
                    # refund smaller than what was locked means an account was left
                    # behind. Present only when this add opened the position; adding to
                    # one that already exists locks no further rent.
                    "position_rent": Decimal(str(position_rent)) if position_rent is not None else None,
                    "entry_price": price,
                    "current_price": price,
                })
            logger.info(f"Booked AMM position {position_address}: +{base_added} base, "
                        f"+{quote_added} quote")

        await self._in_repo_best_effort(
            _fn, error_message=f"Error booking AMM position {position_address}")

    async def record_position_remove(
        self,
        *,
        position_address: str,
        base_amount_removed: Any,
        quote_amount_removed: Any,
        percentage_to_remove: float,
        position_rent_refunded: Any,
    ) -> None:
        """Unbook a confirmed removal, closing the row when the whole position went out."""
        async def _fn(repo):
            position = await repo.subtract_from_position_amounts(
                position_address=position_address,
                base_delta=Decimal(str(base_amount_removed or 0)),
                quote_delta=Decimal(str(quote_amount_removed or 0)),
            )
            # A 100% remove is the close: Gateway closes the position account in
            # the same transaction, which is what returns its rent. There is no
            # separate close route, and positionRentRefunded arrives only on this
            # path — a partial removal leaves the account open and refunds
            # nothing, so its absence there is a fact rather than a gap.
            if position and percentage_to_remove >= 100:
                await repo.close_position(
                    position_address,
                    position_rent_refunded=(Decimal(str(position_rent_refunded))
                                            if position_rent_refunded is not None else None),
                )

        await self._in_repo_best_effort(
            _fn, error_message=f"Error booking AMM removal for {position_address}")

    async def record_event(
        self,
        *,
        transaction_hash: str,
        event_type: str,
        connector: str,
        network: str,
        wallet_address: str,
        pool_address: str,
        position_address: Optional[str],
        base_token_amount: Any,
        quote_token_amount: Any,
        price: Optional[float],
        gas_fee: Any,
        tx_status: str,
    ) -> None:
        """Persist one AMM write.

        Best-effort: the liquidity has already moved by the time this is called, so a
        database problem must not surface as a failed write to the caller. Amounts come
        from Gateway's ``data``, present only once it confirmed the tx; a
        submitted-not-confirmed write records the status with null amounts rather than
        inventing figures.
        """
        chain, _ = network.split("-", 1) if "-" in network else (network, "")

        async def _fn(repo):
            await repo.create_event({
                "transaction_hash": transaction_hash,
                "connector": connector,
                "network": network,
                "wallet_address": wallet_address,
                "pool_address": pool_address,
                "position_address": position_address,
                "event_type": event_type,
                "base_token_amount": base_token_amount,
                "quote_token_amount": quote_token_amount,
                "price": price,
                "gas_fee": gas_fee,
                "gas_token": get_native_gas_token(chain) if gas_fee is not None else None,
                "status": tx_status,
            })
            logger.info(f"Recorded AMM {event_type}: {transaction_hash} (status: {tx_status})")

        await self._in_repo_best_effort(
            _fn, error_message=f"Error recording AMM {event_type} event")

    async def record_failed_event(
        self,
        *,
        transaction_hash: Optional[str],
        error: Exception,
        event_type: str,
        connector: str,
        network: str,
        wallet_address: str,
        pool_address: str,
        position_address: Optional[str] = None,
    ) -> None:
        """Record a write that reached the chain and reverted, before the error is re-raised.

        :meth:`record_event` above only runs when Gateway *returns*. A transaction that
        landed and reverted does not return: Gateway raises, the client turns it into a
        GatewayError, and control skips the whole recording block. That is why every row
        in both event tables read CONFIRMED with no error_message — not because nothing
        had ever failed, but because a failure could not be written.

        Only failures carrying a transaction id are recorded (the caller parses it out of
        the error): a pre-flight simulation failure never got one and cost nothing, while
        a landed revert has one and paid gas. Recording never masks the original failure.
        """
        if not transaction_hash:
            return

        async def _fn(repo):
            await repo.create_event({
                "transaction_hash": transaction_hash,
                "connector": connector,
                "network": network,
                "wallet_address": wallet_address,
                "pool_address": pool_address,
                "position_address": position_address,
                "event_type": event_type,
                "status": "FAILED",
                "error_message": str(error),
            })
            logger.error(
                f"AMM {event_type} {transaction_hash} landed on-chain and FAILED on {connector}/"
                f"{network}; recorded. {error}"
            )

        await self._in_repo_best_effort(
            _fn, error_message=f"Error recording failed AMM {event_type}")

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def search_events(
        self,
        *,
        connector: Optional[str] = None,
        network: Optional[str] = None,
        wallet_address: Optional[str] = None,
        pool_address: Optional[str] = None,
        event_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Recorded AMM liquidity writes, newest first, as the endpoint's envelope."""
        async def _fn(repo):
            events = await repo.search_events(
                connector=connector, network=network, wallet_address=wallet_address,
                pool_address=pool_address, event_type=event_type, status=status,
                limit=min(limit, 1000), offset=offset,
            )
            return {
                "data": [repo.event_to_dict(event) for event in events],
                "total_count": len(events),
                "limit": limit,
                "offset": offset,
            }

        return await self._in_repo(_fn)

    async def search_positions(
        self,
        *,
        connector: Optional[str] = None,
        network: Optional[str] = None,
        wallet_address: Optional[str] = None,
        pool_address: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Tracked AMM positions (Meteora DAMM v2 NFTs), newest first, as the envelope."""
        async def _fn(repo):
            positions = await repo.search_positions(
                connector=connector, network=network, wallet_address=wallet_address,
                pool_address=pool_address, status=status, limit=min(limit, 1000), offset=offset,
            )
            return {
                "data": [repo.position_to_dict(position) for position in positions],
                "total_count": len(positions),
                "limit": limit,
                "offset": offset,
            }

        return await self._in_repo(_fn)
