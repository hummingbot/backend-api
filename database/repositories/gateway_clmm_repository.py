from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Set

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import GatewayCLMMEvent, GatewayCLMMPosition

# The event types whose gas_token the pre-ARCH-054 write paths damaged.
LIQUIDITY_EVENT_TYPES = ("ADD_LIQUIDITY", "REMOVE_LIQUIDITY")


class GatewayCLMMRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    # ============================================
    # Position Management
    # ============================================

    async def create_position(self, position_data: Dict) -> GatewayCLMMPosition:
        """Create a new CLMM position record."""
        position = GatewayCLMMPosition(**position_data)
        self.session.add(position)
        await self.session.flush()
        return position

    async def get_position_by_address(self, position_address: str) -> Optional[GatewayCLMMPosition]:
        """Get a position by its address."""
        result = await self.session.execute(
            select(GatewayCLMMPosition).where(GatewayCLMMPosition.position_address == position_address)
        )
        return result.scalar_one_or_none()

    async def get_position_by_id(self, position_id: int) -> Optional[GatewayCLMMPosition]:
        """Get a position by its primary key id."""
        result = await self.session.execute(
            select(GatewayCLMMPosition).where(GatewayCLMMPosition.id == position_id)
        )
        return result.scalar_one_or_none()

    async def update_position_liquidity(
        self,
        position_address: str,
        base_token_amount: Decimal,
        quote_token_amount: Decimal,
        in_range: Optional[str] = None,
        current_price: Optional[Decimal] = None
    ) -> Optional[GatewayCLMMPosition]:
        """Update position liquidity amounts and current price."""
        result = await self.session.execute(
            select(GatewayCLMMPosition).where(GatewayCLMMPosition.position_address == position_address)
        )
        position = result.scalar_one_or_none()
        if position:
            position.base_token_amount = float(base_token_amount)
            position.quote_token_amount = float(quote_token_amount)
            if in_range is not None:
                position.in_range = in_range
            if current_price is not None:
                position.current_price = float(current_price)
            await self.session.flush()
        return position

    async def update_position_fees(
        self,
        position_address: str,
        base_fee_pending: Optional[Decimal] = None,
        quote_fee_pending: Optional[Decimal] = None,
        base_fee_collected: Optional[Decimal] = None,
        quote_fee_collected: Optional[Decimal] = None
    ) -> Optional[GatewayCLMMPosition]:
        """Update position fee amounts."""
        result = await self.session.execute(
            select(GatewayCLMMPosition).where(GatewayCLMMPosition.position_address == position_address)
        )
        position = result.scalar_one_or_none()
        if position:
            if base_fee_pending is not None:
                position.base_fee_pending = float(base_fee_pending)
            if quote_fee_pending is not None:
                position.quote_fee_pending = float(quote_fee_pending)
            if base_fee_collected is not None:
                position.base_fee_collected = float(base_fee_collected)
            if quote_fee_collected is not None:
                position.quote_fee_collected = float(quote_fee_collected)
            await self.session.flush()
        return position

    async def close_position(
        self,
        position_address: str,
        position_rent_refunded: Optional[Decimal] = None
    ) -> Optional[GatewayCLMMPosition]:
        """Mark position as closed, recording the rent the chain gave back."""
        result = await self.session.execute(
            select(GatewayCLMMPosition).where(GatewayCLMMPosition.position_address == position_address)
        )
        position = result.scalar_one_or_none()
        if position:
            position.status = "CLOSED"
            position.closed_at = datetime.now(timezone.utc)
            if position_rent_refunded is not None:
                position.position_rent_refunded = position_rent_refunded
            await self.session.flush()
        return position

    async def record_position_rent(
        self,
        position_address: str,
        position_rent: Optional[Decimal] = None,
        position_rent_refunded: Optional[Decimal] = None,
    ) -> Optional[GatewayCLMMPosition]:
        """Fill in rent figures the write routes never saw, without disturbing ones they did.

        Both columns are written by hummingbot-api's own routes: `position_rent` by OPEN,
        `position_rent_refunded` by CLOSE. A position an executor opened talks to Gateway
        directly through the wheel, so neither route ran and both columns stay NULL — on
        the workflow that is actually recommended for holding a position. Rent is
        ~0.0100572 SOL on Orca, larger than the liquidity on a small position.

        Only NULL columns are filled. A figure already recorded came from the transaction
        that produced it and is not replaced by one carried in from elsewhere, and a
        second call cannot rewrite what the first stored.

        Callers must not pass zero for an unmeasured figure. NULL means "no rent was
        observed", which is the truth both for a reading that never happened and for a
        chain without rent at all; a stored 0.0 claims a measurement was taken and came
        back empty, which nothing downstream can tell from the real thing (see GW-18).
        """
        result = await self.session.execute(
            select(GatewayCLMMPosition).where(GatewayCLMMPosition.position_address == position_address)
        )
        position = result.scalar_one_or_none()
        if position is None:
            return None

        if position_rent is not None and position.position_rent is None:
            position.position_rent = position_rent
        if position_rent_refunded is not None and position.position_rent_refunded is None:
            position.position_rent_refunded = position_rent_refunded
        await self.session.flush()
        return position

    async def reopen_position(self, position_address: str) -> Optional[GatewayCLMMPosition]:
        """
        Reopen a position that was incorrectly marked as closed.

        This is used when autodiscover finds a position that exists on-chain
        but was marked as CLOSED in the database (e.g., due to a failed close transaction).
        """
        result = await self.session.execute(
            select(GatewayCLMMPosition).where(GatewayCLMMPosition.position_address == position_address)
        )
        position = result.scalar_one_or_none()
        if position and position.status == "CLOSED":
            position.status = "OPEN"
            position.closed_at = None
            await self.session.flush()
        return position

    async def subtract_from_position_amounts(
        self,
        position_address: str,
        base_delta: Decimal,
        quote_delta: Decimal
    ) -> Optional[GatewayCLMMPosition]:
        """Book removed liquidity: lower both the PnL baseline and the held amounts.

        The mirror of add_to_position_amounts, and the same invariant. Lowering only
        the held amounts leaves withdrawn capital counted as a loss of exactly the
        amount withdrawn. entry_price is deliberately untouched: a pro-rata removal
        changes how much remains, not the average price it was entered at.
        """
        result = await self.session.execute(
            select(GatewayCLMMPosition).where(GatewayCLMMPosition.position_address == position_address)
        )
        position = result.scalar_one_or_none()
        if position:
            # Floor at 0: a remove reported larger than the recorded holding (stale
            # cache, or liquidity added outside this API) must not drive it negative.
            position.initial_base_token_amount = max(
                0.0, float(position.initial_base_token_amount or 0) - float(base_delta))
            position.initial_quote_token_amount = max(
                0.0, float(position.initial_quote_token_amount or 0) - float(quote_delta))
            position.base_token_amount = max(0.0, float(position.base_token_amount or 0) - float(base_delta))
            position.quote_token_amount = max(0.0, float(position.quote_token_amount or 0) - float(quote_delta))
            await self.session.flush()
        return position

    async def add_to_position_amounts(
        self,
        position_address: str,
        base_delta: Decimal,
        quote_delta: Decimal,
        entry_price: Optional[Decimal] = None
    ) -> Optional[GatewayCLMMPosition]:
        """Book added liquidity: raise both the PnL baseline and the held amounts.

        Both move together or the row contradicts itself. Raising only the baseline
        leaves a position recorded as holding less than it was given, with no removal
        or fee collection to explain the gap — pnl_summary then reports a loss of
        exactly the added capital. Raising only the held amounts overstates PnL by
        the same figure. Nothing else refreshes these: _refresh_position_data runs
        only when a caller passes search_positions(refresh=True).

        `entry_price` is the pool price at the moment of the add. Given it, the stored
        entry price becomes the base-weighted average of the old basis and the new
        capital — without it, capital deposited today is valued at the price the
        position opened at, and the cost basis is wrong by the drift between them.
        """
        result = await self.session.execute(
            select(GatewayCLMMPosition).where(GatewayCLMMPosition.position_address == position_address)
        )
        position = result.scalar_one_or_none()
        if position:
            old_base = float(position.initial_base_token_amount or 0)
            new_base = old_base + float(base_delta)

            if entry_price is not None and float(base_delta) > 0 and new_base > 0:
                old_entry = float(position.entry_price) if position.entry_price is not None else None
                if old_entry is not None:
                    position.entry_price = (old_entry * old_base + float(entry_price) * float(base_delta)) / new_base
                else:
                    # No basis to weight against (e.g. a discovered position): the
                    # add's own price is the best available.
                    position.entry_price = float(entry_price)

            position.initial_base_token_amount = new_base
            position.initial_quote_token_amount = float(position.initial_quote_token_amount or 0) + float(quote_delta)
            position.base_token_amount = float(position.base_token_amount or 0) + float(base_delta)
            position.quote_token_amount = float(position.quote_token_amount or 0) + float(quote_delta)
            await self.session.flush()
        return position

    async def get_recently_closed_addresses(self, within_seconds: int) -> Set[str]:
        """Addresses of positions closed within the last `within_seconds`.

        Used by the discovery sweep's reopen logic: a lagging positions-owned RPC
        read can still show a position the user just closed, and reopening it would
        flap the record CLOSED -> OPEN -> CLOSED.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=within_seconds)
        result = await self.session.execute(
            select(GatewayCLMMPosition.position_address).where(
                GatewayCLMMPosition.status == "CLOSED",
                GatewayCLMMPosition.closed_at.isnot(None),
                GatewayCLMMPosition.closed_at >= cutoff,
            )
        )
        return set(result.scalars().all())

    async def get_positions(
        self,
        network: Optional[str] = None,
        connector: Optional[str] = None,
        wallet_address: Optional[str] = None,
        trading_pair: Optional[str] = None,
        status: Optional[str] = None,
        position_addresses: Optional[List[str]] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[GatewayCLMMPosition]:
        """Get positions with filtering and pagination."""
        query = select(GatewayCLMMPosition)

        # Apply filters
        if network:
            query = query.where(GatewayCLMMPosition.network == network)
        if connector:
            query = query.where(GatewayCLMMPosition.connector == connector)
        if wallet_address:
            query = query.where(GatewayCLMMPosition.wallet_address == wallet_address)
        if trading_pair:
            query = query.where(GatewayCLMMPosition.trading_pair == trading_pair)
        if status:
            query = query.where(GatewayCLMMPosition.status == status)
        if position_addresses:
            query = query.where(GatewayCLMMPosition.position_address.in_(position_addresses))

        # Apply ordering and pagination
        query = query.order_by(GatewayCLMMPosition.created_at.desc())
        query = query.limit(limit).offset(offset)

        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_open_positions(
        self,
        network: Optional[str] = None,
        wallet_address: Optional[str] = None
    ) -> List[GatewayCLMMPosition]:
        """Get all open positions."""
        return await self.get_positions(
            network=network,
            wallet_address=wallet_address,
            status="OPEN",
            limit=1000
        )

    async def get_position_addresses_set(self, status: Optional[str] = None) -> Set[str]:
        """
        Get a set of position addresses in the database.

        Args:
            status: Optional filter by status ("OPEN" or "CLOSED").
                    If None, returns all positions.

        Returns:
            Set of position addresses (useful for quick existence checks)
        """
        query = select(GatewayCLMMPosition.position_address)
        if status:
            query = query.where(GatewayCLMMPosition.status == status)
        result = await self.session.execute(query)
        return {row[0] for row in result.all()}

    # ============================================
    # Event Management
    # ============================================

    async def create_event(self, event_data: Dict) -> GatewayCLMMEvent:
        """Create a new CLMM event record."""
        event = GatewayCLMMEvent(**event_data)
        self.session.add(event)
        await self.session.flush()
        return event

    async def update_event_status(
        self,
        transaction_hash: str,
        status: str,
        error_message: Optional[str] = None,
        gas_fee: Optional[Decimal] = None,
        gas_token: Optional[str] = None
    ) -> Optional[GatewayCLMMEvent]:
        """Update event status after transaction confirmation."""
        result = await self.session.execute(
            select(GatewayCLMMEvent).where(GatewayCLMMEvent.transaction_hash == transaction_hash)
        )
        event = result.scalar_one_or_none()
        if event:
            event.status = status
            if error_message:
                event.error_message = error_message
            if gas_fee is not None:
                event.gas_fee = float(gas_fee)
            if gas_token:
                event.gas_token = gas_token
            await self.session.flush()
        return event

    async def get_position_events(
        self,
        position_address: str,
        event_type: Optional[str] = None,
        limit: int = 100
    ) -> List[GatewayCLMMEvent]:
        """Get all events for a position."""
        # First get the position
        position = await self.get_position_by_address(position_address)
        if not position:
            return []

        # Then get its events
        query = select(GatewayCLMMEvent).where(GatewayCLMMEvent.position_id == position.id)

        if event_type:
            query = query.where(GatewayCLMMEvent.event_type == event_type)

        query = query.order_by(GatewayCLMMEvent.timestamp.desc()).limit(limit)

        result = await self.session.execute(query)
        return result.scalars().all()

    async def backfill_liquidity_gas_tokens(self) -> Dict:
        """Repair gas_token on liquidity events written before ARCH-054 unified the chain map.

        Two write paths damaged these rows and neither will revisit them: the add/remove
        handlers' two-branch ternary wrote gas_token NULL off solana/ethereum, and a row
        inserted CONFIRMED is never re-polled, so that NULL is permanent; the poller's old
        6-entry dict wrote "UNKNOWN" for base, arbitrum and polygon. The write paths are
        fixed, the historical rows are not, and every cost report reading them is wrong.

        Only rows that actually recorded a gas fee are repaired: with no fee there is no
        currency to name, which is exactly what the routes persist today
        (``get_native_gas_token(chain) if gas_fee is not None else None``). The chain comes
        from the position's ``network`` ("solana-mainnet-beta" -> "solana") and is resolved
        through ``get_native_gas_token`` — the single source ARCH-054 established, never a
        second copy of the map. A chain that still resolves to "UNKNOWN" is left untouched
        and counted, so an unmapped chain surfaces instead of being papered over.

        Idempotent: a repaired row no longer matches the NULL/"UNKNOWN" filter, so a second
        run fixes nothing and changes nothing.

        Returns:
            {"fixed": int, "unresolved": int, "unresolved_networks": List[str]}
        """
        # Imported here, not at module scope: `services/__init__` imports back into
        # `database`, so a top-level import of it from a repository is a cycle.
        from services.gateway_client import get_native_gas_token

        query = (
            select(GatewayCLMMEvent, GatewayCLMMPosition.network)
            .join(GatewayCLMMPosition, GatewayCLMMEvent.position_id == GatewayCLMMPosition.id)
            .where(
                GatewayCLMMEvent.event_type.in_(LIQUIDITY_EVENT_TYPES),
                GatewayCLMMEvent.gas_fee.isnot(None),
                or_(GatewayCLMMEvent.gas_token.is_(None), GatewayCLMMEvent.gas_token == "UNKNOWN"),
            )
        )
        result = await self.session.execute(query)

        fixed = 0
        unresolved = 0
        unresolved_networks: Set[str] = set()
        for event, network in result.all():
            chain = (network or "").split("-", 1)[0]
            gas_token = get_native_gas_token(chain)
            if gas_token == "UNKNOWN":
                unresolved += 1
                unresolved_networks.add(network)
                continue
            event.gas_token = gas_token
            fixed += 1

        if fixed:
            await self.session.flush()

        return {
            "fixed": fixed,
            "unresolved": unresolved,
            "unresolved_networks": sorted(unresolved_networks),
        }

    async def get_pending_events(self, limit: int = 100) -> List[GatewayCLMMEvent]:
        """Get events that are still pending confirmation."""
        query = select(GatewayCLMMEvent).where(
            GatewayCLMMEvent.status == "SUBMITTED"
        ).order_by(GatewayCLMMEvent.timestamp.desc()).limit(limit)

        result = await self.session.execute(query)
        return result.scalars().all()

    # ============================================
    # Utilities
    # ============================================

    def position_to_dict(self, position: GatewayCLMMPosition) -> Dict:
        """Convert GatewayCLMMPosition model to dictionary format with enhanced PnL calculation."""
        pnl_summary = None

        # Get prices for PnL calculation
        entry_price = float(position.entry_price) if position.entry_price is not None else None
        current_price = float(position.current_price) if position.current_price is not None else None

        # Calculate PnL if we have initial amounts and prices
        if (position.initial_base_token_amount is not None
                and position.initial_quote_token_amount is not None
                and entry_price and entry_price > 0
                and current_price and current_price > 0):

            # Initial amounts
            initial_base = float(position.initial_base_token_amount)
            initial_quote = float(position.initial_quote_token_amount)

            # Current liquidity amounts
            current_base = float(position.base_token_amount)
            current_quote = float(position.quote_token_amount)

            # Total fees (collected + pending)
            total_fees_base = float(position.base_fee_collected) + float(position.base_fee_pending)
            total_fees_quote = float(position.quote_fee_collected) + float(position.quote_fee_pending)

            # Value calculations (all normalized to quote currency)
            initial_value_quote = initial_base * entry_price + initial_quote
            current_lp_value_quote = current_base * current_price + current_quote
            total_fees_value_quote = total_fees_base * current_price + total_fees_quote
            current_total_value_quote = current_lp_value_quote + total_fees_value_quote

            # HODL comparison: what if user just held initial tokens without LP
            hodl_value_quote = initial_base * current_price + initial_quote

            # Impermanent loss (negative = loss due to LP vs holding)
            impermanent_loss_quote = current_lp_value_quote - hodl_value_quote

            # Total P&L
            total_pnl_quote = current_total_value_quote - initial_value_quote
            total_pnl_pct = (total_pnl_quote / initial_value_quote * 100) if initial_value_quote > 0 else 0

            # Price change
            price_change_pct = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0

            # Duration and APR estimate
            duration_hours = 0
            fee_apr_estimate = None
            if position.created_at:
                # Use closed_at if closed, otherwise current time
                end_time = position.closed_at if position.closed_at else datetime.now(timezone.utc)
                # Handle timezone-naive datetimes
                if position.created_at.tzinfo is None:
                    created_at = position.created_at.replace(tzinfo=timezone.utc)
                else:
                    created_at = position.created_at
                if end_time.tzinfo is None:
                    end_time = end_time.replace(tzinfo=timezone.utc)

                duration_seconds = (end_time - created_at).total_seconds()
                duration_hours = duration_seconds / 3600

                # Calculate fee APR if we have meaningful duration
                if duration_seconds > 0 and initial_value_quote > 0:
                    duration_years = duration_seconds / (365.25 * 24 * 3600)
                    if duration_years > 0:
                        fee_apr_estimate = (total_fees_value_quote / initial_value_quote / duration_years * 100)

            pnl_summary = {
                # Prices
                "entry_price": round(entry_price, 8),
                "current_price": round(current_price, 8),
                "price_change_pct": round(price_change_pct, 4),

                # Initial state
                "initial_base": round(initial_base, 8),
                "initial_quote": round(initial_quote, 8),
                "initial_value_quote": round(initial_value_quote, 8),

                # Current position (liquidity only, no fees)
                "current_base": round(current_base, 8),
                "current_quote": round(current_quote, 8),
                "current_lp_value_quote": round(current_lp_value_quote, 8),

                # Fees earned
                "total_fees_base": round(total_fees_base, 8),
                "total_fees_quote": round(total_fees_quote, 8),
                "total_fees_value_quote": round(total_fees_value_quote, 8),

                # HODL comparison
                "hodl_value_quote": round(hodl_value_quote, 8),

                # Key metrics
                "impermanent_loss_quote": round(impermanent_loss_quote, 8),
                "current_total_value_quote": round(current_total_value_quote, 8),
                "total_pnl_quote": round(total_pnl_quote, 8),
                "total_pnl_pct": round(total_pnl_pct, 4),

                # Time metrics
                "duration_hours": round(duration_hours, 2),
                "fee_apr_estimate": round(fee_apr_estimate, 2) if fee_apr_estimate else None
            }

        return {
            "position_address": position.position_address,
            "pool_address": position.pool_address,
            "network": position.network,
            "connector": position.connector,
            "wallet_address": position.wallet_address,
            "trading_pair": position.trading_pair,
            "base_token": position.base_token,
            "quote_token": position.quote_token,
            "created_at": position.created_at.isoformat(),
            "closed_at": position.closed_at.isoformat() if position.closed_at else None,
            "status": position.status,
            "lower_price": float(position.lower_price),
            "upper_price": float(position.upper_price),
            "lower_bin_id": position.lower_bin_id,
            "upper_bin_id": position.upper_bin_id,
            "entry_price": entry_price,
            "current_price": current_price,
            "percentage": float(position.percentage) if position.percentage is not None else None,
            "initial_base_token_amount": (float(position.initial_base_token_amount)
                                          if position.initial_base_token_amount is not None else None),
            "initial_quote_token_amount": (float(position.initial_quote_token_amount)
                                           if position.initial_quote_token_amount is not None else None),
            "position_rent": float(position.position_rent) if position.position_rent is not None else None,
            "position_rent_refunded": (float(position.position_rent_refunded)
                                       if position.position_rent_refunded is not None else None),
            "base_token_amount": float(position.base_token_amount),
            "quote_token_amount": float(position.quote_token_amount),
            "in_range": position.in_range,
            "base_fee_collected": float(position.base_fee_collected),
            "quote_fee_collected": float(position.quote_fee_collected),
            "base_fee_pending": float(position.base_fee_pending),
            "quote_fee_pending": float(position.quote_fee_pending),
            "pnl_summary": pnl_summary,
            "last_updated": position.last_updated.isoformat(),
        }

    def event_to_dict(self, event: GatewayCLMMEvent) -> Dict:
        """Convert GatewayCLMMEvent model to dictionary format."""
        return {
            "transaction_hash": event.transaction_hash,
            "timestamp": event.timestamp.isoformat(),
            "event_type": event.event_type,
            "base_token_amount": float(event.base_token_amount) if event.base_token_amount is not None else None,
            "quote_token_amount": float(event.quote_token_amount) if event.quote_token_amount is not None else None,
            "base_fee_collected": float(event.base_fee_collected) if event.base_fee_collected is not None else None,
            "quote_fee_collected": float(event.quote_fee_collected) if event.quote_fee_collected is not None else None,
            "gas_fee": float(event.gas_fee) if event.gas_fee is not None else None,
            "gas_token": event.gas_token,
            "status": event.status,
            "error_message": event.error_message,
        }
