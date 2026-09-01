"""Persistence for CLMM positions and their lifecycle events.

Everything the ``/gateway/clmm/*`` routes know about the database lives here: the
shape of a position row, the shape of each event row (OPEN, ADD_LIQUIDITY,
REMOVE_LIQUIDITY, CLOSE, COLLECT_FEES), and the bookkeeping each one applies to the
position it belongs to. The handlers used to carry a copy of all three per endpoint,
which is how the poller's auto-discovery path ended up deriving a different key set
for the same table.

Reads propagate their errors; the after-the-fact recording of an operation that is
already on-chain is best-effort, per ``RepositoryService``.
"""
import asyncio
import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from database.repositories import GatewayCLMMRepository
from services.gateway_client import check_gateway_error
from services.repository_service import RepositoryService

logger = logging.getLogger(__name__)


async def refresh_position_data(position, gateway_client, clmm_repo: GatewayCLMMRepository):
    """
    Refresh position data from Gateway and update database.

    This updates:
    - in_range status
    - liquidity amounts
    - pending fees
    - position status (if closed externally)
    """
    try:
        # Get wallet address for the position
        wallet_address = position.wallet_address

        # Get all positions for this pool and find our specific position
        try:
            # check_gateway_error is critical here: a Gateway HTTP error must raise (and skip
            # the refresh) rather than flow onward and mark the position CLOSED below.
            positions_list = check_gateway_error(await gateway_client.clmm_positions_owned(
                connector=position.connector,
                chain_network=position.network,  # position.network is already in 'chain-network' format
                wallet_address=wallet_address
            ))

            # Find our specific position in the list
            result = None
            if isinstance(positions_list, list):
                for pos in positions_list:
                    if pos.get("address") == position.position_address:
                        result = pos
                        break

            # Absent from a single positions-owned read: could be closed externally,
            # could be a lagging RPC node. Closing is owned by the poller's
            # consecutive-miss gate (and the zero-liquidity check below) so one
            # refresh can never close a live position.
            if result is None:
                logger.info(f"Position {position.position_address} absent from positions-owned; "
                            "skipping update (poller's miss-gate owns close detection)")
                return

        except Exception as e:
            # If we can't fetch positions, log error but don't mark as closed
            logger.error(f"Error fetching position from Gateway: {e}")
            return

        # Extract current state
        current_price = Decimal(str(result.get("price", 0)))
        lower_price = Decimal(str(result.get("lowerPrice", 0))) if result.get("lowerPrice") else Decimal("0")
        upper_price = Decimal(str(result.get("upperPrice", 0))) if result.get("upperPrice") else Decimal("0")

        # Calculate in_range status
        in_range = "UNKNOWN"
        if current_price > 0 and lower_price > 0 and upper_price > 0:
            if lower_price <= current_price <= upper_price:
                in_range = "IN_RANGE"
            else:
                in_range = "OUT_OF_RANGE"

        # Extract token amounts
        base_token_amount = Decimal(str(result.get("baseTokenAmount", 0)))
        quote_token_amount = Decimal(str(result.get("quoteTokenAmount", 0)))

        # Check if position has been closed (zero liquidity)
        if base_token_amount == 0 and quote_token_amount == 0:
            logger.info(f"Position {position.position_address} has zero liquidity, marking as CLOSED")
            await clmm_repo.close_position(position.position_address)
            return

        # Update liquidity amounts, in_range status, and current price
        await clmm_repo.update_position_liquidity(
            position_address=position.position_address,
            base_token_amount=base_token_amount,
            quote_token_amount=quote_token_amount,
            in_range=in_range,
            current_price=current_price
        )

        # Always write pending fees — 0 is a real value (e.g. right after an
        # external collect); the old non-zero guard left stale pendings forever.
        base_fee_pending = Decimal(str(result.get("baseFeeAmount", 0)))
        quote_fee_pending = Decimal(str(result.get("quoteFeeAmount", 0)))

        await clmm_repo.update_position_fees(
            position_address=position.position_address,
            base_fee_pending=base_fee_pending,
            quote_fee_pending=quote_fee_pending
        )

        logger.debug(f"Refreshed position {position.position_address}: price={current_price}, in_range={in_range}, "
                     f"base={base_token_amount}, quote={quote_token_amount}")

    except Exception as e:
        logger.error(f"Error refreshing position {position.position_address}: {e}", exc_info=True)
        raise


class GatewayCLMMService(RepositoryService):
    """Position and event persistence for the CLMM routes."""

    repository_class = GatewayCLMMRepository

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_position_wallet(self, position_address: str) -> Optional[str]:
        """Wallet recorded for a position, or None if hapi has no row for it.

        Used by close/collect to resolve the signer: an explicit request value wins,
        then this, then the chain's default wallet.
        """
        async def _fn(clmm_repo):
            position = await clmm_repo.get_position_by_address(position_address)
            return position.wallet_address if position else None

        return await self._in_repo(_fn)

    async def get_position_pool_address(self, position_address: str) -> Optional[str]:
        """Pool a position sits in, or None if hapi has no row for it."""
        async def _fn(clmm_repo):
            position = await clmm_repo.get_position_by_address(position_address)
            return position.pool_address if position else None

        return await self._in_repo(_fn)

    async def get_position_events(
        self,
        position_address: str,
        event_type: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """Event history for a position, as the endpoint's envelope."""
        async def _fn(clmm_repo):
            events = await clmm_repo.get_position_events(
                position_address=position_address,
                event_type=event_type,
                limit=limit
            )

            return {
                "data": [clmm_repo.event_to_dict(event) for event in events],
                "total_count": len(events)
            }

        return await self._in_repo(_fn)

    async def search_positions(
        self,
        *,
        network: Optional[str] = None,
        connector: Optional[str] = None,
        wallet_address: Optional[str] = None,
        trading_pair: Optional[str] = None,
        status: Optional[str] = None,
        position_addresses: Optional[List[str]] = None,
        limit: int = 50,
        offset: int = 0,
        refresh: bool = False,
        gateway_client=None,
    ) -> Dict[str, Any]:
        """Search stored positions, optionally refreshing them from Gateway first.

        Args:
            refresh: Re-read each matched position from Gateway and write back what
                it says before answering. Requires ``gateway_client``.
        """
        # Validate limit
        if limit > 1000:
            limit = 1000

        filters = dict(
            network=network,
            connector=connector,
            wallet_address=wallet_address,
            trading_pair=trading_pair,
            status=status,
            position_addresses=position_addresses,
            limit=limit,
            offset=offset,
        )

        # Optionally refresh position data from Gateway first
        if refresh and gateway_client is not None and await gateway_client.ping():
            await self._refresh_positions(filters, gateway_client)

        # Get final results after refresh
        async def _fn(clmm_repo):
            positions = await clmm_repo.get_positions(**filters)

            # Get total count for pagination
            has_more = len(positions) == limit

            return {
                "data": [clmm_repo.position_to_dict(pos) for pos in positions],
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "has_more": has_more,
                    "total_count": len(positions) + offset if not has_more else None
                }
            }

        return await self._in_repo(_fn)

    async def _refresh_positions(self, filters: Dict[str, Any], gateway_client) -> None:
        """Re-read every position matching ``filters`` from Gateway, one session each."""
        # Get positions to refresh
        async def _fn(clmm_repo):
            positions_to_refresh = await clmm_repo.get_positions(**filters)

            # Extract position addresses and details before closing session
            return [
                {
                    "position_address": pos.position_address,
                    "pool_address": pos.pool_address,
                    "connector": pos.connector,
                    "network": pos.network,
                    "wallet_address": pos.wallet_address
                }
                for pos in positions_to_refresh
            ]

        position_details = await self._in_repo(_fn)

        # Refresh each position in a separate session
        logger.info(f"Refreshing {len(position_details)} positions from Gateway")
        for pos_detail in position_details:
            async def _refresh(clmm_repo, address=pos_detail["position_address"]):
                # Get position again in this session
                position = await clmm_repo.get_position_by_address(address)
                if position:
                    await refresh_position_data(position, gateway_client, clmm_repo)

            try:
                await self._in_repo(_refresh)
            except Exception as e:
                logger.warning(f"Failed to refresh position {pos_detail['position_address']}: {e}")
                # Continue with other positions even if one fails

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def record_open_position(
        self,
        *,
        position_address: str,
        pool_address: str,
        network: str,
        connector: str,
        wallet_address: str,
        trading_pair: str,
        base_token: str,
        quote_token: str,
        lower_price: Decimal,
        upper_price: Decimal,
        entry_price: Optional[float],
        base_amount_added: Any,
        quote_amount_added: Any,
        position_rent: Any,
        transaction_hash: str,
        gas_fee: Any,
        gas_token: Optional[str],
        tx_status: str,
    ) -> None:
        """Record a newly opened position and its OPEN event."""
        # Calculate percentage: (upper_price - lower_price) / lower_price
        percentage = None
        if lower_price and upper_price and lower_price > 0:
            percentage = float((upper_price - lower_price) / lower_price)
            logger.info(f"Position price range percentage: {percentage:.4f} ({percentage*100:.2f}%)")

        async def _fn(clmm_repo):
            # Create position record
            position_data = {
                "position_address": position_address,
                "pool_address": pool_address,
                "network": network,
                "connector": connector,
                "wallet_address": wallet_address,
                "trading_pair": trading_pair,
                "base_token": base_token,
                "quote_token": quote_token,
                "status": "OPEN",
                "lower_price": float(lower_price),
                "upper_price": float(upper_price),
                "percentage": percentage,
                "entry_price": entry_price,  # Pool price when position opened
                "current_price": entry_price,  # Same as entry at open time, updated by poller
                "initial_base_token_amount": float(base_amount_added),
                "initial_quote_token_amount": float(quote_amount_added),
                "position_rent": float(position_rent) if position_rent else None,
                "base_token_amount": float(base_amount_added),
                "quote_token_amount": float(quote_amount_added),
                "in_range": "UNKNOWN"  # Will be updated by poller
            }

            position = await clmm_repo.create_position(position_data)
            logger.info(f"Recorded CLMM position in database: {position_address}")

            # Create OPEN event with polled status
            event_data = {
                "position_id": position.id,
                "transaction_hash": transaction_hash,
                "event_type": "OPEN",
                "base_token_amount": float(base_amount_added) if base_amount_added is not None else None,
                "quote_token_amount": float(quote_amount_added) if quote_amount_added is not None else None,
                "gas_fee": float(gas_fee) if gas_fee is not None else None,
                "gas_token": gas_token,
                "status": tx_status
            }

            await clmm_repo.create_event(event_data)
            logger.info(f"Recorded CLMM OPEN event in database: {transaction_hash} "
                        f"(status: {tx_status}, gas: {gas_fee} {gas_token})")

        await self._in_repo_best_effort(
            _fn, error_message="Error recording CLMM position in database")

    async def record_add_liquidity(
        self,
        *,
        position_address: str,
        transaction_hash: str,
        tx_status: str,
        base_amount_added: Any,
        quote_amount_added: Any,
        gas_fee: Any,
        gas_token: Optional[str],
        add_price: Optional[float],
    ) -> None:
        """Record an ADD_LIQUIDITY event and book the added capital."""
        async def _fn(clmm_repo):
            # Get position to link event
            position = await clmm_repo.get_position_by_address(position_address)
            if not position:
                logger.warning(f"ADD_LIQUIDITY {transaction_hash} executed for position "
                               f"{position_address} with no database record — "
                               "no event recorded (position may be a pending open "
                               "not yet discovered)")
                return

            event_data = {
                "position_id": position.id,
                "transaction_hash": transaction_hash,
                "event_type": "ADD_LIQUIDITY",
                # `is not None`: 0 is a real amount on single-sided adds
                "base_token_amount": float(base_amount_added) if base_amount_added is not None else None,
                "quote_token_amount": float(quote_amount_added) if quote_amount_added is not None else None,
                "gas_fee": float(gas_fee) if gas_fee is not None else None,
                "gas_token": gas_token,
                "status": tx_status
            }
            await clmm_repo.create_event(event_data)
            logger.info(f"Recorded CLMM ADD_LIQUIDITY event: {transaction_hash} "
                        f"(status: {tx_status}, gas: {gas_fee} {gas_token})")

            # Added capital raises both the PnL baseline and the held amounts.
            # Book here only when the tx confirmed inline (the event is created
            # CONFIRMED and the poller never re-processes it); SUBMITTED events
            # are booked by the poller's confirm path.
            if tx_status == "CONFIRMED":
                await clmm_repo.add_to_position_amounts(
                    position_address=position_address,
                    base_delta=Decimal(str(base_amount_added or 0)),
                    quote_delta=Decimal(str(quote_amount_added or 0)),
                    entry_price=Decimal(str(add_price)) if add_price else None,
                )

        await self._in_repo_best_effort(
            _fn, error_message="Error recording ADD_LIQUIDITY event")

    async def record_remove_liquidity(
        self,
        *,
        position_address: str,
        transaction_hash: str,
        tx_status: str,
        base_amount_removed: Any,
        quote_amount_removed: Any,
        gas_fee: Any,
        gas_token: Optional[str],
    ) -> None:
        """Record a REMOVE_LIQUIDITY event and book the withdrawn capital."""
        async def _fn(clmm_repo):
            # Get position to link event
            position = await clmm_repo.get_position_by_address(position_address)
            if not position:
                logger.warning(f"REMOVE_LIQUIDITY {transaction_hash} executed for position "
                               f"{position_address} with no database record — "
                               "no event recorded (position may be a pending open "
                               "not yet discovered)")
                return

            # No "percentage" key: GatewayCLMMEvent has no such column, and the
            # stray kwarg made create_event raise — silently losing every
            # REMOVE_LIQUIDITY event to the log-and-continue handler below.
            event_data = {
                "position_id": position.id,
                "transaction_hash": transaction_hash,
                "event_type": "REMOVE_LIQUIDITY",
                "base_token_amount": float(base_amount_removed) if base_amount_removed is not None else None,
                "quote_token_amount": float(quote_amount_removed) if quote_amount_removed is not None else None,
                "gas_fee": float(gas_fee) if gas_fee is not None else None,
                "gas_token": gas_token,
                "status": tx_status
            }
            await clmm_repo.create_event(event_data)
            logger.info(f"Recorded CLMM REMOVE_LIQUIDITY event: {transaction_hash} "
                        f"(status: {tx_status}, gas: {gas_fee} {gas_token})")

            # Withdrawn capital lowers both the held amounts and the PnL
            # baseline. Book only on inline confirmation (the event is created
            # CONFIRMED and the poller never re-processes it); SUBMITTED events
            # are booked by the poller's confirm path.
            if tx_status == "CONFIRMED":
                await clmm_repo.subtract_from_position_amounts(
                    position_address=position_address,
                    base_delta=Decimal(str(base_amount_removed or 0)),
                    quote_delta=Decimal(str(quote_amount_removed or 0)),
                )

        await self._in_repo_best_effort(
            _fn, error_message="Error recording REMOVE_LIQUIDITY event")

    async def record_close(
        self,
        *,
        position_address: str,
        connector: str,
        network: str,
        transaction_hash: str,
        tx_status: str,
        base_amount_removed: Any,
        quote_amount_removed: Any,
        base_fee_collected: Decimal,
        quote_fee_collected: Decimal,
        position_rent_refunded: Any,
        close_price: Optional[float],
        gas_fee: Any,
        gas_token: Optional[str],
        gateway_client,
    ) -> None:
        """Record a CLOSE event, book the final fees, and mark the position closed.

        ``gateway_client`` is re-read after the write to confirm the position really
        is gone before the row is marked CLOSED.
        """
        async def _fn(clmm_repo):
            # Get position to link event
            position = await clmm_repo.get_position_by_address(position_address)
            if not position:
                # H8 window: a close on a position hapi has no row for (e.g. a
                # pending open awaiting the discovery sweep) leaves no event —
                # say so loudly instead of silently skipping.
                logger.warning(f"CLOSE {transaction_hash} executed for position "
                               f"{position_address} with no database record — "
                               "no CLOSE event recorded (position may be a pending open "
                               "not yet discovered)")
                return

            # Create event record
            event_data = {
                "position_id": position.id,
                "transaction_hash": transaction_hash,
                "event_type": "CLOSE",
                "base_token_amount": float(base_amount_removed) if base_amount_removed is not None else None,
                "quote_token_amount": float(quote_amount_removed) if quote_amount_removed is not None else None,
                "base_fee_collected": float(base_fee_collected) if base_fee_collected is not None else None,
                "quote_fee_collected": float(quote_fee_collected) if quote_fee_collected is not None else None,
                "gas_fee": float(gas_fee) if gas_fee is not None else None,
                "gas_token": gas_token,
                "status": tx_status
            }
            await clmm_repo.create_event(event_data)
            logger.info(f"Recorded CLMM CLOSE event: {transaction_hash} "
                        f"(status: {tx_status}, gas: {gas_fee} {gas_token})")

            # Position bookkeeping happens exactly once, when the tx is known
            # good: CONFIRMED here (the event is created CONFIRMED, so the
            # poller never touches it), or in the poller's confirm path for
            # SUBMITTED events. A FAILED tx mutates nothing — the old
            # unconditional booking permanently inflated *_fee_collected on
            # failed closes.
            if tx_status != "CONFIRMED":
                return False

            new_base_collected = Decimal(str(position.base_fee_collected)) + base_fee_collected
            new_quote_collected = Decimal(str(position.quote_fee_collected)) + quote_fee_collected

            await clmm_repo.update_position_fees(
                position_address=position_address,
                base_fee_collected=new_base_collected,
                quote_fee_collected=new_quote_collected,
                base_fee_pending=Decimal("0"),
                quote_fee_pending=Decimal("0")
            )

            # Update current_price with close price
            if close_price:
                await clmm_repo.update_position_liquidity(
                    position_address=position_address,
                    base_token_amount=Decimal(str(position.base_token_amount)),
                    quote_token_amount=Decimal(str(position.quote_token_amount)),
                    current_price=Decimal(str(close_price))
                )

            return True

        booked = await self._in_repo_best_effort(
            _fn, error_message="Error recording CLOSE event", default=False
        )
        if not booked:
            return

        # The propagation wait happens with no session held: the writes above are
        # committed and the pooled connection is back before we sit idle for two
        # seconds, so a fleet closing positions at once cannot drain the pool
        # waiting on the chain.
        #
        # Verify position is actually gone on Gateway before marking CLOSED (some
        # connectors 500 instead of 404 for a nonexistent position — right after our
        # own close, either means gone).
        try:
            await asyncio.sleep(2)  # Wait for transaction to propagate

            verify_result = await gateway_client.clmm_position_info(
                connector=connector,
                chain_network=network,
                position_address=position_address
            )

            if verify_result and isinstance(verify_result, dict) and "error" in verify_result:
                status_code = verify_result.get("status")
                if status_code in (404, 500):
                    async def _close(clmm_repo):
                        await clmm_repo.close_position(
                            position_address,
                            position_rent_refunded=(Decimal(str(position_rent_refunded))
                                                    if position_rent_refunded is not None else None)
                        )

                    await self._in_repo(_close)
                    logger.info(f"Position {position_address} verified as closed "
                                f"(Gateway returned {status_code})")
                else:
                    logger.warning(f"Unexpected error verifying position close: {verify_result}")
            elif verify_result and "address" in verify_result:
                # Position still exists - might be a failed close or delayed propagation
                logger.warning(f"Position {position_address} still exists after close "
                               "transaction. Will be handled by poller.")
            else:
                logger.debug("Could not verify position close status, will be handled by poller")

        except Exception as verify_error:
            logger.warning(f"Error verifying position close: {verify_error}. Will be handled by poller.")

        logger.info(f"Updated position {position_address}: "
                    "collected fees updated, pending fees reset to 0.")

    async def record_collect_fees(
        self,
        *,
        position_address: str,
        transaction_hash: str,
        tx_status: str,
        base_fee_collected: Decimal,
        quote_fee_collected: Decimal,
        gas_fee: Any,
        gas_token: Optional[str],
    ) -> None:
        """Record a COLLECT_FEES event and book the collected fees."""
        async def _fn(clmm_repo):
            # Get position to link event
            position = await clmm_repo.get_position_by_address(position_address)
            if not position:
                logger.warning(f"COLLECT_FEES {transaction_hash} executed for position "
                               f"{position_address} with no database record — "
                               "no event recorded (position may be a pending open "
                               "not yet discovered)")
                return

            # Create event record
            event_data = {
                "position_id": position.id,
                "transaction_hash": transaction_hash,
                "event_type": "COLLECT_FEES",
                "base_fee_collected": float(base_fee_collected) if base_fee_collected is not None else None,
                "quote_fee_collected": float(quote_fee_collected) if quote_fee_collected is not None else None,
                "gas_fee": float(gas_fee) if gas_fee is not None else None,
                "gas_token": gas_token,
                "status": tx_status
            }
            await clmm_repo.create_event(event_data)
            logger.info(f"Recorded CLMM COLLECT_FEES event: {transaction_hash} "
                        f"(status: {tx_status}, gas: {gas_fee} {gas_token})")

            # Book fees exactly once: CONFIRMED here (event created CONFIRMED,
            # never re-processed), SUBMITTED in the poller's confirm path.
            # The old unconditional booking double-counted every pending
            # collect (endpoint + poller) and kept phantom fees on failures.
            if tx_status == "CONFIRMED":
                new_base_collected = Decimal(str(position.base_fee_collected)) + base_fee_collected
                new_quote_collected = Decimal(str(position.quote_fee_collected)) + quote_fee_collected

                await clmm_repo.update_position_fees(
                    position_address=position_address,
                    base_fee_collected=new_base_collected,
                    quote_fee_collected=new_quote_collected,
                    base_fee_pending=Decimal("0"),
                    quote_fee_pending=Decimal("0")
                )
                logger.info(f"Updated position {position_address}: "
                            "collected fees updated, pending fees reset to 0")

        await self._in_repo_best_effort(
            _fn, error_message="Error recording COLLECT_FEES event")

    async def record_failed_write(
        self,
        *,
        transaction_hash: Optional[str],
        error: Exception,
        event_type: str,
        position_address: Optional[str],
    ) -> None:
        """Record a write that reached the chain and reverted, before the error is re-raised.

        The recording methods above only run when Gateway *returns*. A transaction that
        landed and reverted does not return: Gateway raises, the client turns it into a
        GatewayError, and control skips every ``create_event`` call to land in an
        ``except`` that persists nothing. So the database said every operation ever
        attempted had succeeded, while a close that reverted at slot 440494812 — costing
        0.000011772 SOL — left no row at all.

        Only failures carrying a transaction id are recorded (the caller parses it out of
        the error). A pre-flight simulation failure never got one and cost nothing, and
        inventing an identifier for it would put a row in the table that no lookup by hash
        could ever match.

        Recording never masks the original failure: the caller still gets Gateway's error.
        """
        if not transaction_hash or not position_address:
            return

        async def _fn(repo):
            position = await repo.get_position_by_address(position_address)
            if position is None:
                logger.warning(
                    f"CLMM {event_type} {transaction_hash} reverted on-chain for position "
                    f"{position_address}, which has no database record — no event written."
                )
                return False
            await repo.create_event({
                "position_id": position.id,
                "transaction_hash": transaction_hash,
                "event_type": event_type,
                "status": "FAILED",
                "error_message": str(error),
            })
            return True

        recorded = await self._in_repo_best_effort(
            _fn, error_message=f"Error recording failed CLMM {event_type}", default=False)
        if recorded:
            logger.error(
                f"CLMM {event_type} {transaction_hash} landed on-chain and FAILED for position "
                f"{position_address}; recorded. {error}"
            )
