"""Persistence for CLMM positions and their lifecycle events.

Everything the ``/gateway/clmm/*`` routes and the transaction poller know about the
database lives here: the shape of a position row, the shape of each event row (OPEN,
ADD_LIQUIDITY, REMOVE_LIQUIDITY, CLOSE, COLLECT_FEES), and the bookkeeping each one
applies to the position it belongs to. The handlers used to carry a copy of all three
per endpoint, which is how the poller's auto-discovery path ended up deriving a
different key set for the same table — see :func:`build_position_row`.

Reads propagate their errors; the after-the-fact recording of an operation that is
already on-chain is best-effort, per ``RepositoryService``.
"""
import asyncio
import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

from database.repositories import GatewayCLMMRepository
from services.gateway_client import check_gateway_error
from services.repository_service import RepositoryService

logger = logging.getLogger(__name__)


def build_position_row(
    *,
    position_address: str,
    pool_address: str,
    network: str,
    connector: str,
    wallet_address: str,
    trading_pair: str,
    base_token: str,
    quote_token: str,
    lower_price: Any,
    upper_price: Any,
    entry_price: Optional[Any],
    current_price: Optional[Any],
    initial_base_token_amount: Any,
    initial_quote_token_amount: Any,
    base_token_amount: Any,
    quote_token_amount: Any,
    lower_bin_id: Optional[int] = None,
    upper_bin_id: Optional[int] = None,
    position_rent: Optional[Any] = None,
    in_range: str = "UNKNOWN",
    base_fee_pending: Any = 0,
    quote_fee_pending: Any = 0,
) -> Dict[str, Any]:
    """The one description of what a new ``gateway_clmm_positions`` row contains.

    A position reaches the table by two routes — ``/gateway/clmm/open`` opens one, and
    the poller's discovery sweep finds one that was opened elsewhere — and each used to
    assemble the row itself. The key sets drifted apart: discovery wrote ``lower_bin_id``,
    ``upper_bin_id``, ``base_fee_pending`` and ``quote_fee_pending``, the route wrote
    ``position_rent``, and neither wrote the other's columns. The same logical position
    therefore had two different row shapes depending on which path recorded it, and
    anything reading the table back had to know which one to expect.

    So the key set is fixed here, and it is the union: every caller yields the same
    columns, and a caller that cannot know a value passes nothing rather than omitting
    the column. Absent is expressed as NULL (bin ids the route never learns, rent no
    discovered position ever locked through us) and zero only where zero is the fact —
    a position that has just come into the table has collected no fees.

    ``lower_price``/``upper_price``/the amounts accept anything ``float()`` takes, so
    both a route's ``Decimal`` and Gateway's parsed JSON floats arrive the same way.
    """
    # (upper - lower) / lower, computed on Decimals so a route's exact request values
    # and the poller's floats round identically.
    percentage = None
    if lower_price and upper_price and Decimal(str(lower_price)) > 0:
        percentage = float(
            (Decimal(str(upper_price)) - Decimal(str(lower_price))) / Decimal(str(lower_price))
        )
        logger.info(f"Position price range percentage: {percentage:.4f} ({percentage * 100:.2f}%)")

    return {
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
        # Bin-based CLMMs (Meteora) identify a range by bin; Gateway reports them on a
        # position it lists, not on the response to opening one.
        "lower_bin_id": lower_bin_id,
        "upper_bin_id": upper_bin_id,
        "entry_price": float(entry_price) if entry_price is not None else None,
        "current_price": float(current_price) if current_price is not None else None,
        "percentage": percentage,
        "initial_base_token_amount": float(initial_base_token_amount),
        "initial_quote_token_amount": float(initial_quote_token_amount),
        # Rent is locked, not spent, and only the open route observes the figure. NULL
        # rather than 0: a stored 0.0 claims a measurement that came back empty, which
        # nothing downstream can tell from rent that was never read.
        "position_rent": float(position_rent) if position_rent else None,
        "base_token_amount": float(base_token_amount),
        "quote_token_amount": float(quote_token_amount),
        "in_range": in_range,
        # Fees already accrued on-chain but not yet collected. Zero on a fresh open;
        # a discovered position may have been earning for days before we saw it.
        "base_fee_pending": float(base_fee_pending),
        "quote_fee_pending": float(quote_fee_pending),
        # Nothing has been collected through us yet on either path, by definition.
        "base_fee_collected": 0.0,
        "quote_fee_collected": 0.0,
    }


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
        position_data = build_position_row(
            position_address=position_address,
            pool_address=pool_address,
            network=network,
            connector=connector,
            wallet_address=wallet_address,
            trading_pair=trading_pair,
            base_token=base_token,
            quote_token=quote_token,
            lower_price=lower_price,
            upper_price=upper_price,
            entry_price=entry_price,  # Pool price when position opened
            current_price=entry_price,  # Same as entry at open time, updated by poller
            initial_base_token_amount=base_amount_added,
            initial_quote_token_amount=quote_amount_added,
            base_token_amount=base_amount_added,
            quote_token_amount=quote_amount_added,
            position_rent=position_rent,
            # in_range is left UNKNOWN rather than derived from the entry price: the
            # poller re-reads the position from the chain within the minute and is the
            # only thing that has ever set this column from an observation.
        )

        async def _fn(clmm_repo):
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

    # ------------------------------------------------------------------
    # Transaction poller
    #
    # The poller used to construct GatewayCLMMRepository itself in five places and
    # hold one session open across a whole poll cycle's worth of Gateway calls. It
    # now decides *what the chain says* — status classification, the dropped grace
    # window, the consecutive-miss gate — and everything about *what gets written*
    # lives here with the routes' writes. Each operation takes its own short session,
    # so a failure part-way through a cycle no longer discards the statuses already
    # confirmed in it.
    # ------------------------------------------------------------------

    async def get_pending_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Events still awaiting confirmation, with the network their position sits on.

        Returned as plain dicts: the poller does Gateway I/O between reading these and
        writing the result, and ORM instances must not outlive their session.
        ``network`` is None when the event's position row is missing, which the caller
        reports rather than guessing a chain from.
        """
        async def _fn(clmm_repo):
            events = await clmm_repo.get_pending_events(limit=limit)
            pending = []
            for event in events:
                position = await clmm_repo.get_position_by_id(event.position_id)
                pending.append({
                    "transaction_hash": event.transaction_hash,
                    "timestamp": event.timestamp,
                    "network": position.network if position else None,
                    "position_address": position.position_address if position else None,
                })
            return pending

        return await self._in_repo(_fn)

    async def update_event_status(
        self,
        *,
        transaction_hash: str,
        status: str,
        error_message: Optional[str] = None,
        gas_fee: Optional[Decimal] = None,
        gas_token: Optional[str] = None,
    ) -> None:
        """Record what a poll found for an event that did not confirm."""
        async def _fn(clmm_repo):
            await clmm_repo.update_event_status(
                transaction_hash=transaction_hash,
                status=status,
                error_message=error_message,
                gas_fee=gas_fee,
                gas_token=gas_token,
            )

        await self._in_repo_best_effort(
            _fn, error_message=f"Error recording {status} status for CLMM event {transaction_hash}")

    async def record_event_confirmed(
        self,
        *,
        transaction_hash: str,
        gas_fee: Optional[Decimal] = None,
        gas_token: Optional[str] = None,
    ) -> None:
        """Mark an event CONFIRMED and apply the bookkeeping it owes its position.

        Both halves share one session, as they always have. The bookkeeping is guarded
        separately: a position that cannot be booked must not roll back the confirmed
        status, or the next cycle would poll the same transaction and book it twice.
        """
        async def _fn(clmm_repo):
            event = await clmm_repo.update_event_status(
                transaction_hash=transaction_hash,
                status="CONFIRMED",
                gas_fee=gas_fee,
                gas_token=gas_token,
            )
            if event is None:
                logger.warning(f"CLMM event {transaction_hash} confirmed on-chain but has no row to update")
                return

            try:
                await self._book_confirmed_event(clmm_repo, event)
            except Exception as e:
                logger.error(f"Error updating position from event {event.id}: {e}", exc_info=True)

        await self._in_repo_best_effort(
            _fn, error_message=f"Error confirming CLMM event {transaction_hash}")

    @staticmethod
    async def _book_confirmed_event(clmm_repo, event) -> None:
        """Apply a newly confirmed event's effect to its position.

        Fee and capital booking happens exactly once, here: the routes only mutate the
        position when Gateway confirmed the transaction inline (those events are created
        CONFIRMED and never reach this path), and leave submitted-not-confirmed booking
        to the poller.
        """
        position = await clmm_repo.get_position_by_id(event.position_id)
        if not position:
            logger.error(f"Position not found for event {event.id}")
            return

        if event.event_type == "CLOSE":
            if event.base_fee_collected is not None or event.quote_fee_collected is not None:
                new_base = float(position.base_fee_collected or 0) + float(event.base_fee_collected or 0)
                new_quote = float(position.quote_fee_collected or 0) + float(event.quote_fee_collected or 0)
                await clmm_repo.update_position_fees(
                    position_address=position.position_address,
                    base_fee_collected=Decimal(str(new_base)),
                    quote_fee_collected=Decimal(str(new_quote)),
                    base_fee_pending=Decimal("0"),
                    quote_fee_pending=Decimal("0")
                )
            await clmm_repo.close_position(position.position_address)

        elif event.event_type == "ADD_LIQUIDITY":
            # Added capital raises both the PnL baseline and the held amounts. Event
            # amounts may be the requested figures (recorded at submit time) rather
            # than on-chain actuals — the accepted residual is that pending-tx amounts
            # are not backfilled from txData; requested amounts are the best available.
            if event.base_token_amount or event.quote_token_amount:
                await clmm_repo.add_to_position_amounts(
                    position_address=position.position_address,
                    base_delta=Decimal(str(event.base_token_amount or 0)),
                    quote_delta=Decimal(str(event.quote_token_amount or 0)),
                )

        elif event.event_type == "REMOVE_LIQUIDITY":
            # The mirror of ADD_LIQUIDITY: withdrawn capital lowers both the held
            # amounts and the PnL baseline.
            if event.base_token_amount or event.quote_token_amount:
                await clmm_repo.subtract_from_position_amounts(
                    position_address=position.position_address,
                    base_delta=Decimal(str(event.base_token_amount or 0)),
                    quote_delta=Decimal(str(event.quote_token_amount or 0)),
                )

        elif event.event_type == "COLLECT_FEES":
            if event.base_fee_collected or event.quote_fee_collected:
                new_base_collected = float(position.base_fee_collected or 0) + float(event.base_fee_collected or 0)
                new_quote_collected = float(position.quote_fee_collected or 0) + float(event.quote_fee_collected or 0)
                await clmm_repo.update_position_fees(
                    position_address=position.position_address,
                    base_fee_collected=Decimal(str(new_base_collected)),
                    quote_fee_collected=Decimal(str(new_quote_collected)),
                    base_fee_pending=Decimal("0"),
                    quote_fee_pending=Decimal("0")
                )

    async def get_tracked_position_addresses(self, recently_closed_seconds: int) -> Dict[str, Set[str]]:
        """The three address sets the discovery sweep compares Gateway's listing against.

        One session for all three: they are read together and used together, and a
        position that changed status between them would make the sweep contradict itself.
        """
        async def _fn(clmm_repo):
            return {
                "open": await clmm_repo.get_position_addresses_set(status="OPEN"),
                "closed": await clmm_repo.get_position_addresses_set(status="CLOSED"),
                "recently_closed": await clmm_repo.get_recently_closed_addresses(recently_closed_seconds),
            }

        return await self._in_repo(_fn)

    async def reopen_position(self, position_address: str) -> bool:
        """Undo a close for a position the chain still reports as live. True if reopened."""
        async def _fn(clmm_repo):
            return await clmm_repo.reopen_position(position_address) is not None

        return await self._in_repo_best_effort(
            _fn, error_message=f"Error reopening position {position_address}", default=False)

    async def record_discovered_position(
        self,
        *,
        pos_data: Dict[str, Any],
        connector: str,
        network: str,
        wallet_address: str,
    ) -> bool:
        """Record a position the poller found on-chain that hapi has no row for.

        These were opened elsewhere (the UI, an executor talking to Gateway directly),
        so the entry price and the initial deposit are unknowable: what the chain holds
        right now is the best available estimate for both, and is recorded as such.

        The row itself is :func:`build_position_row`'s — the same columns the open route
        writes — and it is paired with a synthetic DISCOVERED event so the history says
        where the row came from.
        """
        position_address = pos_data.get("address")
        if not position_address:
            return False

        # Full token addresses are used as the token identity here, as the open route does.
        base_token = pos_data.get("baseTokenAddress") or "UNKNOWN"
        quote_token = pos_data.get("quoteTokenAddress") or "UNKNOWN"

        current_price = float(pos_data.get("price", 0))
        lower_price = float(pos_data.get("lowerPrice", 0))
        upper_price = float(pos_data.get("upperPrice", 0))

        base_token_amount = float(pos_data.get("baseTokenAmount", 0))
        quote_token_amount = float(pos_data.get("quoteTokenAmount", 0))

        in_range = "UNKNOWN"
        if current_price > 0 and lower_price > 0 and upper_price > 0:
            in_range = "IN_RANGE" if lower_price <= current_price <= upper_price else "OUT_OF_RANGE"

        position_data = build_position_row(
            position_address=position_address,
            pool_address=pos_data.get("poolAddress", ""),
            network=network,
            connector=connector,
            wallet_address=wallet_address,
            trading_pair=f"{base_token}-{quote_token}",
            base_token=base_token,
            quote_token=quote_token,
            lower_price=lower_price,
            upper_price=upper_price,
            lower_bin_id=pos_data.get("lowerBinId"),
            upper_bin_id=pos_data.get("upperBinId"),
            entry_price=current_price,  # Best available estimate
            current_price=current_price,
            # Nothing records what was originally deposited, so what is held now stands
            # in for it — the PnL baseline starts at the moment of discovery.
            initial_base_token_amount=base_token_amount,
            initial_quote_token_amount=quote_token_amount,
            base_token_amount=base_token_amount,
            quote_token_amount=quote_token_amount,
            in_range=in_range,
            base_fee_pending=float(pos_data.get("baseFeeAmount", 0)),
            quote_fee_pending=float(pos_data.get("quoteFeeAmount", 0)),
        )

        async def _fn(clmm_repo):
            position = await clmm_repo.create_position(position_data)
            await clmm_repo.create_event({
                "position_id": position.id,
                # Synthetic: there is no transaction of ours to confirm.
                "transaction_hash": f"discovered_{position_address[:16]}",
                "event_type": "DISCOVERED",
                "base_token_amount": base_token_amount,
                "quote_token_amount": quote_token_amount,
                "status": "CONFIRMED",
            })
            return True

        return await self._in_repo_best_effort(
            _fn, error_message=f"Error creating discovered position {position_address}", default=False)

    async def get_open_positions(self) -> List[Dict[str, Any]]:
        """The open positions the poller refreshes, as plain dicts.

        Only the fields needed to re-read a position from Gateway: the refresh does
        network I/O per position and must not hold a session while it does.
        """
        async def _fn(clmm_repo):
            positions = await clmm_repo.get_open_positions()
            return [
                {
                    "id": position.id,
                    "position_address": position.position_address,
                    "wallet_address": position.wallet_address,
                    "connector": position.connector,
                    "network": position.network,
                }
                for position in positions
            ]

        return await self._in_repo(_fn)

    async def mark_position_closed(self, position_address: str) -> None:
        """Close a position the chain no longer reports. Never fails the poll cycle."""
        async def _fn(clmm_repo):
            await clmm_repo.close_position(position_address)

        await self._in_repo_best_effort(
            _fn, error_message=f"Error closing position {position_address}")

    async def record_position_state(
        self,
        *,
        position_address: str,
        base_token_amount: Decimal,
        quote_token_amount: Decimal,
        in_range: str,
        current_price: Decimal,
        base_fee_pending: Decimal,
        quote_fee_pending: Decimal,
    ) -> None:
        """Write back everything one on-chain read of a position says, in one session.

        Pending fees are always written: 0 is a real value right after an external
        collect, and a non-zero guard would leave the old figure standing forever.
        """
        async def _fn(clmm_repo):
            await clmm_repo.update_position_liquidity(
                position_address=position_address,
                base_token_amount=base_token_amount,
                quote_token_amount=quote_token_amount,
                in_range=in_range,
                current_price=current_price,
            )
            await clmm_repo.update_position_fees(
                position_address=position_address,
                base_fee_pending=base_fee_pending,
                quote_fee_pending=quote_fee_pending,
            )

        await self._in_repo_best_effort(
            _fn, error_message=f"Error updating state for position {position_address}")
