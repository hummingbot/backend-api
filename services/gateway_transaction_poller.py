"""
Gateway Transaction Poller

This service polls blockchain transactions to confirm Gateway swap and CLMM operations.
Unlike CEX connectors that emit events, DEX transactions require active polling until confirmation.

Additionally polls CLMM position state to keep database in sync with on-chain state.
"""
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Optional

from database import AsyncDatabaseManager
from services.gateway_client import GatewayClient, get_native_gas_token
from services.gateway_clmm_service import GatewayCLMMService
from services.gateway_swap_service import GatewaySwapService

logger = logging.getLogger(__name__)


class GatewayTransactionPoller:
    """
    Polls Gateway for transaction status updates and position state.

    - Transaction polling: Confirms pending swap/CLMM transactions
    - Position polling: Updates CLMM position state (in_range, liquidity, fees)

    Unlike CEX connectors that emit events when orders fill, DEX transactions
    need to be polled until they are confirmed on-chain or fail.
    """

    def __init__(
        self,
        db_manager: AsyncDatabaseManager,
        gateway_client: GatewayClient,
        poll_interval: int = 10,  # Poll every 10 seconds for transactions
        position_poll_interval: int = 300,  # Poll every 5 minutes for positions
        max_retry_age: int = 3600  # Stop retrying after 1 hour
    ):
        self.db_manager = db_manager
        self.gateway_client = gateway_client
        # Every row this poller reads or writes goes through the same services the
        # /gateway/clmm and /gateway/swap routes persist through, so a position it
        # discovers has the same shape as one a route opened. Constructed here rather
        # than injected: a RepositoryService holds nothing but the shared db_manager,
        # so there is no instance worth threading down from main.py.
        self.clmm_service = GatewayCLMMService(db_manager=db_manager)
        self.swap_service = GatewaySwapService(db_manager=db_manager)
        self.poll_interval = poll_interval
        self.position_poll_interval = position_poll_interval
        self.max_retry_age = max_retry_age
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._position_poll_task: Optional[asyncio.Task] = None
        self._last_position_poll: Optional[datetime] = None
        # Consecutive position-info misses per position. A single 404/500 can be a
        # transient RPC problem (Gateway 500s on upstream hiccups), so a position is
        # only marked CLOSED after MISSING_STRIKES_TO_CLOSE consecutive misses.
        self._position_missing_strikes: Dict[str, int] = {}

    async def start(self):
        """Start the polling service."""
        if self._running:
            logger.warning("GatewayTransactionPoller already running")
            return

        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._position_poll_task = asyncio.create_task(self._position_poll_loop())
        logger.info(f"GatewayTransactionPoller started (tx_poll={self.poll_interval}s, pos_poll={self.position_poll_interval}s)")

    async def stop(self):
        """Stop the polling service."""
        if not self._running:
            return

        self._running = False

        # Cancel transaction polling task
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        # Cancel position polling task
        if self._position_poll_task:
            self._position_poll_task.cancel()
            try:
                await self._position_poll_task
            except asyncio.CancelledError:
                pass

        logger.info("GatewayTransactionPoller stopped")

    async def _poll_loop(self):
        """Main polling loop."""
        while self._running:
            try:
                await self._poll_pending_transactions()
            except Exception as e:
                logger.error(f"Error in poll loop: {e}", exc_info=True)

            # Wait before next poll
            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break

    # A tx reported NOT_FOUND (-2) is only terminal once its blockhash can no longer
    # be valid (~90s on Solana); before that, -2 can just mean "not visible yet".
    DROPPED_GRACE_SECONDS = 180

    # Consecutive position-info misses before a position is marked CLOSED
    # (mirrors the lp_executor's external-close gate).
    MISSING_STRIKES_TO_CLOSE = 3

    # Don't reopen a DB-CLOSED position seen in positions-owned if it was closed
    # within this window: the listing RPC node may simply lag the close.
    REOPEN_GRACE_SECONDS = 300

    async def _poll_pending_transactions(self):
        """Poll all pending transactions and update their status."""
        try:
            # One availability gate per cycle. When Gateway is unreachable nothing is
            # polled AND nothing is aged out: the age timeout must never fire on a
            # transaction we could not actually check (it may have confirmed on-chain).
            if not await self.gateway_client.ping():
                logger.warning("Gateway not available; skipping transaction poll cycle")
                return

            # The pending book is read up front and the session released: what follows
            # is one Gateway call per transaction, and holding a pooled connection
            # across all of them is what used to drain the pool under a busy fleet.
            pending_swaps = await self.swap_service.get_pending_swaps(limit=100)
            logger.debug(f"Found {len(pending_swaps)} pending swaps")

            for swap in pending_swaps:
                await self._poll_swap_transaction(swap)

            pending_events = await self.clmm_service.get_pending_events(limit=100)
            logger.debug(f"Found {len(pending_events)} pending CLMM events")

            for event in pending_events:
                await self._poll_clmm_event_transaction(event)

        except Exception as e:
            logger.error(f"Error polling pending transactions: {e}", exc_info=True)

    async def _poll_swap_transaction(self, swap: Dict):
        """Poll a specific swap transaction status."""
        transaction_hash = swap["transaction_hash"]
        try:
            # Parse network into chain and network
            parts = swap["network"].split('-', 1)
            if len(parts) != 2:
                logger.error(f"Invalid network format for swap {transaction_hash}: {swap['network']}")
                return

            chain, network = parts

            status_result = await self._check_transaction_status(
                chain=chain,
                network=network,
                tx_hash=transaction_hash
            )
            if status_result is None:
                # Transient (Gateway/RPC hiccup): no information, no state change.
                return

            age = (datetime.now(timezone.utc) - swap["timestamp"]).total_seconds()
            status = status_result["status"]
            gas_fee_raw = status_result.get("gas_fee")
            gas_fee = Decimal(str(gas_fee_raw)) if gas_fee_raw is not None else None

            if status == "CONFIRMED":
                # Accepted residual: amounts/price are NOT backfilled from the poll's
                # txData — a swap recorded while pending keeps its request-side leg
                # and 0 placeholders after confirmation. Backfilling requires parsing
                # balance changes from txData (deferred by design).
                logger.info(f"Swap transaction confirmed: {transaction_hash}")
                await self.swap_service.update_swap_status(
                    transaction_hash=transaction_hash,
                    status="CONFIRMED",
                    gas_fee=gas_fee,
                    gas_token=status_result.get("gas_token")
                )
            elif status == "FAILED":
                # A landed-but-failed tx still paid gas — record it.
                logger.warning(f"Swap transaction failed: {transaction_hash}")
                await self.swap_service.update_swap_status(
                    transaction_hash=transaction_hash,
                    status="FAILED",
                    error_message=status_result.get("error_message", "Transaction failed on-chain"),
                    gas_fee=gas_fee,
                    gas_token=status_result.get("gas_token")
                )
            elif status == "DROPPED" and age > self.DROPPED_GRACE_SECONDS:
                logger.warning(f"Swap transaction dropped (not found on-chain): {transaction_hash}")
                await self.swap_service.update_swap_status(
                    transaction_hash=transaction_hash,
                    status="FAILED",
                    error_message="Transaction not found on-chain (dropped after blockhash expiry)"
                )
            elif status == "PENDING" and age > self.max_retry_age:
                # Genuinely still unconfirmed after a successful poll — only now may
                # the age timeout fire.
                logger.warning(f"Swap {transaction_hash} exceeded max retry age, marking as FAILED")
                await self.swap_service.update_swap_status(
                    transaction_hash=transaction_hash,
                    status="FAILED",
                    error_message="Transaction confirmation timeout"
                )
            # PENDING within age / DROPPED within grace: retry next cycle.

        except Exception as e:
            logger.error(f"Error polling swap transaction {transaction_hash}: {e}")

    async def _poll_clmm_event_transaction(self, event: Dict):
        """Poll a specific CLMM event transaction status."""
        transaction_hash = event["transaction_hash"]
        try:
            # The network comes from the position the event belongs to; None means
            # that row is missing, so there is no chain to ask about this event.
            if not event["network"]:
                logger.error(f"Position not found for CLMM event {transaction_hash}")
                return

            # Parse network
            parts = event["network"].split('-', 1)
            if len(parts) != 2:
                logger.error(f"Invalid network format for CLMM event {transaction_hash}: {event['network']}")
                return

            chain, network = parts

            status_result = await self._check_transaction_status(
                chain=chain,
                network=network,
                tx_hash=transaction_hash
            )
            if status_result is None:
                # Transient (Gateway/RPC hiccup): no information, no state change.
                return

            age = (datetime.now(timezone.utc) - event["timestamp"]).total_seconds()
            status = status_result["status"]
            gas_fee_raw = status_result.get("gas_fee")
            gas_fee = Decimal(str(gas_fee_raw)) if gas_fee_raw is not None else None

            if status == "CONFIRMED":
                logger.info(f"CLMM event transaction confirmed: {transaction_hash}")
                # Marks the event CONFIRMED and applies what it owes its position
                # (fees booked, capital added or withdrawn, a close finalised).
                await self.clmm_service.record_event_confirmed(
                    transaction_hash=transaction_hash,
                    gas_fee=gas_fee,
                    gas_token=status_result.get("gas_token")
                )
            elif status == "FAILED":
                logger.warning(f"CLMM event transaction failed: {transaction_hash}")
                await self.clmm_service.update_event_status(
                    transaction_hash=transaction_hash,
                    status="FAILED",
                    error_message=status_result.get("error_message", "Transaction failed on-chain"),
                    gas_fee=gas_fee,
                    gas_token=status_result.get("gas_token")
                )
            elif status == "DROPPED" and age > self.DROPPED_GRACE_SECONDS:
                logger.warning(f"CLMM event transaction dropped (not found on-chain): {transaction_hash}")
                await self.clmm_service.update_event_status(
                    transaction_hash=transaction_hash,
                    status="FAILED",
                    error_message="Transaction not found on-chain (dropped after blockhash expiry)"
                )
            elif status == "PENDING" and age > self.max_retry_age:
                logger.warning(f"CLMM event {transaction_hash} exceeded max retry age, marking as FAILED")
                await self.clmm_service.update_event_status(
                    transaction_hash=transaction_hash,
                    status="FAILED",
                    error_message="Transaction confirmation timeout"
                )
            # PENDING within age / DROPPED within grace: retry next cycle.

        except Exception as e:
            logger.error(f"Error polling CLMM event transaction {transaction_hash}: {e}")

    async def _check_transaction_status(
        self,
        chain: str,
        network: str,
        tx_hash: str
    ) -> Optional[Dict]:
        """
        Check transaction status on blockchain via Gateway.

        Returns:
            Dict with status ("CONFIRMED" | "FAILED" | "DROPPED" | "PENDING"),
            gas_fee, gas_token, and error_message.
            None only when no information could be obtained (Gateway/RPC hiccup) —
            callers must treat that as "no state change", never as pending-with-age.
        """
        try:
            # Reconstruct network_id from chain and network
            # (Gateway availability is gated once per cycle by the caller.)
            network_id = f"{chain}-{network}"

            # Poll transaction status from Gateway
            result = await self.gateway_client.poll_transaction(
                network_id=network_id,
                tx_hash=tx_hash
            )

            # Check if we got a valid response
            if result is None or not isinstance(result, dict):
                logger.warning(f"Invalid response from Gateway for transaction {tx_hash} on {network_id}: {result}")
                return None

            # A Gateway HTTP error (the client's {"error", "status"} shape) is transient — treat it
            # like "no response" rather than letting its 'error' key mark the transaction FAILED below.
            if set(result.keys()) == {"error", "status"}:
                logger.warning(
                    f"Gateway HTTP error polling transaction {tx_hash} on {network_id} "
                    f"(status {result['status']}): {result['error']}"
                )
                return None

            logger.debug(f"Polled transaction {tx_hash} on {network_id}: txStatus={result.get('txStatus')}")

            # Classify on txStatus ALONE. Gateway deliberately returns txStatus 0
            # (pending) WITH a non-null `error` for transient poll failures — "the
            # caller should poll again, not give up" — so the error field must never
            # promote a pending transaction to FAILED.
            tx_status = result.get("txStatus")
            gas_token = get_native_gas_token(chain)
            gas_fee = result.get("fee")

            if tx_status == 1:
                return {
                    "status": "CONFIRMED",
                    "gas_fee": gas_fee,
                    "gas_token": gas_token,
                    "error_message": None
                }

            if tx_status == -1:
                # Landed on-chain but failed. Gateway returns parsed error messages
                # like "SLIPPAGE_EXCEEDED (0x1771): ..."; fall back to meta.err.
                error_msg = result.get("error")
                if not error_msg:
                    tx_data = result.get("txData") or {}
                    meta = tx_data.get("meta") if isinstance(tx_data, dict) else {}
                    raw_error = meta.get("err") if isinstance(meta, dict) else None
                    error_msg = str(raw_error) if raw_error else "Transaction failed on-chain"
                return {
                    "status": "FAILED",
                    "gas_fee": gas_fee,
                    "gas_token": gas_token,
                    "error_message": error_msg
                }

            if tx_status == -2:
                # NOT_FOUND — terminal on Solana once the blockhash expires; can also
                # appear briefly right after submission. The caller applies the grace
                # window before treating it as dropped.
                return {
                    "status": "DROPPED",
                    "gas_fee": None,
                    "gas_token": gas_token,
                    "error_message": "Transaction not found on-chain"
                }

            # txStatus 0 (or anything unrecognized): a successful poll that says the
            # transaction is still unconfirmed — distinct from None (no information).
            return {
                "status": "PENDING",
                "gas_fee": None,
                "gas_token": gas_token,
                "error_message": None
            }

        except Exception as e:
            logger.error(f"Error checking transaction status for {tx_hash}: {e}")
            return None

    # ============================================
    # Position State Polling & Discovery
    # ============================================

    # Supported CLMM connectors and their default networks. The discovery sweep is
    # also the reconciliation path for opens that returned submitted-not-confirmed
    # (position_address unknown at open time), so every Solana connector whose open
    # can pend (meteora, raydium, pancakeswap-sol) must be listed; orca rides along
    # to reconcile externally-created positions. All four speak the unified
    # /trading/clmm/positions-owned schema.
    SUPPORTED_CLMM_CONFIGS = [
        {"connector": "meteora", "chain": "solana", "network": "mainnet-beta"},
        {"connector": "raydium", "chain": "solana", "network": "mainnet-beta"},
        {"connector": "pancakeswap-sol", "chain": "solana", "network": "mainnet-beta"},
        {"connector": "orca", "chain": "solana", "network": "mainnet-beta"},
        # EVM CLMM opens never return the pending shape (data always present), so
        # discovery is not load-bearing there:
        # {"connector": "uniswap", "chain": "ethereum", "network": "mainnet"},
    ]

    async def _position_poll_loop(self):
        """Position state polling loop (runs less frequently)."""
        while self._running:
            try:
                # Check if it's time to poll positions
                now = datetime.now(timezone.utc)
                if self._last_position_poll is None or \
                   (now - self._last_position_poll).total_seconds() >= self.position_poll_interval:
                    await self._poll_and_discover_positions()
                    self._last_position_poll = now

                # Sleep for a short time to avoid busy waiting
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in position poll loop: {e}", exc_info=True)
                await asyncio.sleep(10)

    async def _poll_and_discover_positions(self):
        """
        Main position polling method that:
        1. Discovers new positions from Gateway (created via UI or other means)
        2. Updates all open positions with latest state
        """
        try:
            # Check if Gateway is available
            if not await self.gateway_client.ping():
                logger.debug("Gateway not available, skipping position polling")
                return

            # Step 1: Discover new positions from Gateway
            discovered_count = await self._discover_positions_from_gateway()
            if discovered_count > 0:
                logger.info(f"Discovered {discovered_count} new positions from Gateway")

            # Step 2: Update all open positions
            await self._update_all_open_positions()

        except Exception as e:
            logger.error(f"Error in position poll and discovery: {e}", exc_info=True)

    async def _discover_positions_from_gateway(self) -> int:
        """
        Discover positions from Gateway that aren't tracked in the database,
        and reopen positions that were incorrectly marked as closed.

        This allows tracking positions created directly via UI or other means,
        not just those created through the API.

        Also corrects data inconsistencies where a position was marked CLOSED
        in the database but is still OPEN on-chain (e.g., due to a failed close
        transaction).

        Returns:
            Number of newly discovered + reopened positions
        """
        discovered_count = 0
        reopened_count = 0

        try:
            # Get all wallet addresses for supported chains
            wallet_addresses_by_chain = await self.gateway_client.get_all_wallet_addresses()
            if not wallet_addresses_by_chain:
                logger.debug("No wallets configured in Gateway, skipping position discovery")
                return 0

            # Existing position addresses (for quick existence checks): OPEN ones are
            # already tracked correctly, CLOSED ones may need reopening if still
            # on-chain, and ones closed moments ago are exempt from that (lag guard).
            tracked = await self.clmm_service.get_tracked_position_addresses(self.REOPEN_GRACE_SECONDS)
            open_positions = tracked["open"]
            closed_positions = tracked["closed"]
            recently_closed = tracked["recently_closed"]

            # Poll each supported connector/chain/wallet combination
            for config in self.SUPPORTED_CLMM_CONFIGS:
                connector = config["connector"]
                chain = config["chain"]
                network = config["network"]

                # Get wallet addresses for this chain
                wallet_addresses = wallet_addresses_by_chain.get(chain, [])
                if not wallet_addresses:
                    continue

                for wallet_address in wallet_addresses:
                    try:
                        # Fetch ALL positions for this wallet (no pool filter)
                        chain_network = f"{chain}-{network}"
                        gateway_positions = await self.gateway_client.clmm_positions_owned(
                            connector=connector,
                            chain_network=chain_network,
                            wallet_address=wallet_address
                        )

                        if not gateway_positions or not isinstance(gateway_positions, list):
                            continue

                        # Process each position
                        for pos_data in gateway_positions:
                            position_address = pos_data.get("address")
                            if not position_address:
                                continue

                            # Skip if already tracked as OPEN
                            if position_address in open_positions:
                                continue

                            # Check if position was incorrectly marked as CLOSED
                            if position_address in closed_positions:
                                if position_address in recently_closed:
                                    # Just closed — the positions-owned RPC node may
                                    # lag the close confirmation; reopening now would
                                    # flap the record CLOSED -> OPEN -> CLOSED.
                                    logger.debug(f"Position {position_address} closed recently; "
                                                 "skipping reopen (listing may lag the close)")
                                    continue
                                # Position exists on-chain but is CLOSED in DB → reopen it
                                if await self.clmm_service.reopen_position(position_address):
                                    reopened_count += 1
                                    # Move from closed to open set for this run
                                    closed_positions.discard(position_address)
                                    open_positions.add(position_address)
                                    logger.warning(f"Reopened position {position_address} - "
                                                   f"was CLOSED in DB but still exists on-chain")
                                continue

                            # Create new position in database, in the same shape the
                            # /gateway/clmm/open route writes.
                            created = await self.clmm_service.record_discovered_position(
                                pos_data=pos_data,
                                connector=connector,
                                network=chain_network,
                                wallet_address=wallet_address
                            )

                            if created:
                                discovered_count += 1
                                open_positions.add(position_address)
                                logger.info(f"Discovered new position: {position_address} "
                                            f"(pool: {pos_data.get('poolAddress', 'unknown')[:16]}...)")

                    except Exception as e:
                        logger.warning(f"Error discovering positions for {connector}/{chain}/{wallet_address}: {e}")
                        continue

        except Exception as e:
            logger.error(f"Error in position discovery: {e}", exc_info=True)

        if reopened_count > 0:
            logger.info(f"Position discovery complete: {discovered_count} new, {reopened_count} reopened")

        return discovered_count + reopened_count

    async def _update_all_open_positions(self):
        """Update state for all open positions from Gateway."""
        try:
            open_positions = await self.clmm_service.get_open_positions()
            if not open_positions:
                logger.debug("No open CLMM positions to update")
                return

            logger.info(f"Updating {len(open_positions)} open CLMM positions")

            # One Gateway read and one short write per position: the list above is
            # already detached, so no session is held across the network calls.
            for position in open_positions:
                try:
                    await self._refresh_position_state(position)
                except Exception as e:
                    logger.warning(f"Failed to update position {position['position_address']}: {e}")
                    continue

        except Exception as e:
            logger.error(f"Error updating open positions: {e}", exc_info=True)

    async def _refresh_position_state(self, position: Dict):
        """
        Refresh a single position's state from Gateway.

        Updates:
        - in_range status
        - liquidity amounts
        - pending fees
        - position status (if closed externally)
        """
        position_address = position["position_address"]
        try:
            # Validate position has required fields
            if not position_address:
                logger.error(f"Position ID {position['id']} has no position_address, skipping refresh")
                return
            if not position["wallet_address"]:
                logger.error(f"Position {position_address} has no wallet_address, skipping refresh")
                return
            if not position["connector"]:
                logger.error(f"Position {position_address} has no connector, skipping refresh")
                return
            if not position["network"]:
                logger.error(f"Position {position_address} has no network, skipping refresh")
                return

            # Get individual position info from Gateway (includes pending fees)
            try:
                result = await self.gateway_client.clmm_position_info(
                    connector=position["connector"],
                    chain_network=position["network"],  # already in 'chain-network' format
                    position_address=position_address
                )

                # Check for Gateway errors
                if result is None:
                    logger.debug(f"Gateway connection error for position {position_address}, skipping update")
                    return

                if not isinstance(result, dict):
                    logger.warning(f"Unexpected response type for position {position_address}: {type(result)}")
                    return

                # Check if Gateway returned an error response
                if "error" in result:
                    status_code = result.get("status")

                    # Some connectors 500 instead of 404 for a nonexistent position,
                    # but Gateway ALSO 500s on transient RPC problems — so a miss only
                    # counts as a strike, and the position closes after
                    # MISSING_STRIKES_TO_CLOSE consecutive misses, never on one.
                    if status_code in (404, 500):
                        strikes = self._position_missing_strikes.get(position_address, 0) + 1
                        self._position_missing_strikes[position_address] = strikes
                        if strikes >= self.MISSING_STRIKES_TO_CLOSE:
                            logger.info(f"Position {position_address} missing from Gateway "
                                        f"{strikes} consecutive times (last status: {status_code}), "
                                        "marking as CLOSED")
                            await self.clmm_service.mark_position_closed(position_address)
                            self._position_missing_strikes.pop(position_address, None)
                        else:
                            logger.debug(f"Position {position_address} miss "
                                         f"{strikes}/{self.MISSING_STRIKES_TO_CLOSE} "
                                         f"(status: {status_code}), not closing yet")
                        return
                    # Other errors → skip update, don't close
                    logger.debug(f"Gateway error for position {position_address}: "
                                 f"{result.get('error')} (status: {status_code})")
                    return

                # Validate response has required fields
                if "address" not in result:
                    logger.warning(f"Invalid response for position {position_address}, missing 'address' field")
                    return

                # Successful read: the position exists — reset the miss counter.
                self._position_missing_strikes.pop(position_address, None)

            except Exception as e:
                logger.warning(f"Error fetching position {position_address} from Gateway: {e}")
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

            # Extract token amounts - validate they exist in response
            base_amount_raw = result.get("baseTokenAmount")
            quote_amount_raw = result.get("quoteTokenAmount")

            # If amounts are missing or None, skip update (don't assume zero)
            if base_amount_raw is None or quote_amount_raw is None:
                logger.warning(f"Position {position_address} missing token amounts in response, skipping update")
                return

            base_token_amount = Decimal(str(base_amount_raw))
            quote_token_amount = Decimal(str(quote_amount_raw))

            # If Gateway confirms zero liquidity, position was closed externally
            if base_token_amount == 0 and quote_token_amount == 0:
                logger.info(f"Position {position_address} has zero liquidity, marking as CLOSED")
                await self.clmm_service.mark_position_closed(position_address)
                return

            # Liquidity, in_range, current price and the pending fees are one reading
            # of the position and are written back together.
            base_fee_pending = Decimal(str(result.get("baseFeeAmount", 0)))
            quote_fee_pending = Decimal(str(result.get("quoteFeeAmount", 0)))

            await self.clmm_service.record_position_state(
                position_address=position_address,
                base_token_amount=base_token_amount,
                quote_token_amount=quote_token_amount,
                in_range=in_range,
                current_price=current_price,
                base_fee_pending=base_fee_pending,
                quote_fee_pending=quote_fee_pending,
            )

            logger.debug(f"Refreshed position {position_address}: price={current_price}, in_range={in_range}, "
                         f"base={base_token_amount}, quote={quote_token_amount}, "
                         f"base_fee={base_fee_pending}, quote_fee={quote_fee_pending}")

        except Exception as e:
            logger.error(f"Error refreshing position state {position_address}: {e}", exc_info=True)
