from sqlalchemy import TIMESTAMP, Boolean, Column, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship

Base = declarative_base()


class AccountState(Base):
    __tablename__ = "account_states"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False, index=True)
    account_name = Column(String, nullable=False, index=True)
    connector_name = Column(String, nullable=False, index=True)

    token_states = relationship("TokenState", back_populates="account_state", cascade="all, delete-orphan")


class TokenState(Base):
    __tablename__ = "token_states"

    id = Column(Integer, primary_key=True, index=True)
    account_state_id = Column(Integer, ForeignKey("account_states.id"), nullable=False)
    token = Column(String, nullable=False, index=True)
    units = Column(Numeric(precision=30, scale=18), nullable=False)
    price = Column(Numeric(precision=30, scale=18), nullable=False)
    value = Column(Numeric(precision=30, scale=18), nullable=False)
    available_units = Column(Numeric(precision=30, scale=18), nullable=False)

    account_state = relationship("AccountState", back_populates="token_states")


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    # Order identification
    client_order_id = Column(String, nullable=False, unique=True, index=True)
    exchange_order_id = Column(String, nullable=True, index=True)

    # Timestamps
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Account and connector info
    account_name = Column(String, nullable=False, index=True)
    connector_name = Column(String, nullable=False, index=True)

    # Order details
    trading_pair = Column(String, nullable=False, index=True)
    trade_type = Column(String, nullable=False)  # BUY, SELL
    order_type = Column(String, nullable=False)  # LIMIT, MARKET, LIMIT_MAKER
    amount = Column(Numeric(precision=30, scale=18), nullable=False)
    price = Column(Numeric(precision=30, scale=18), nullable=True)  # Null for market orders

    # Order status and execution
    status = Column(String, nullable=False, default="SUBMITTED",
                    index=True)  # SUBMITTED, OPEN, FILLED, CANCELLED, FAILED
    filled_amount = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    average_fill_price = Column(Numeric(precision=30, scale=18), nullable=True)

    # Fee information
    fee_paid = Column(Numeric(precision=30, scale=18), default=0, nullable=True)
    fee_currency = Column(String, nullable=True)

    # Additional metadata
    error_message = Column(Text, nullable=True)

    # Relationships for future enhancements
    trades = relationship("Trade", back_populates="order", cascade="all, delete-orphan")


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)

    # Trade identification
    trade_id = Column(String, nullable=False, unique=True, index=True)

    # Timestamps
    timestamp = Column(TIMESTAMP(timezone=True), nullable=False, index=True)

    # Trade details
    trading_pair = Column(String, nullable=False, index=True)
    trade_type = Column(String, nullable=False)  # BUY, SELL
    amount = Column(Numeric(precision=30, scale=18), nullable=False)
    price = Column(Numeric(precision=30, scale=18), nullable=False)

    # Fee information
    fee_paid = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    fee_currency = Column(String, nullable=True)

    # Relationship
    order = relationship("Order", back_populates="trades")


class PositionSnapshot(Base):
    __tablename__ = "position_snapshots"

    id = Column(Integer, primary_key=True, index=True)

    # Position identification
    account_name = Column(String, nullable=False, index=True)
    connector_name = Column(String, nullable=False, index=True)
    trading_pair = Column(String, nullable=False, index=True)

    # Timestamps
    timestamp = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False, index=True)

    # Real-time exchange data (from connector.account_positions)
    side = Column(String, nullable=False)  # LONG, SHORT
    exchange_size = Column(Numeric(precision=30, scale=18), nullable=False)  # Size from exchange
    entry_price = Column(Numeric(precision=30, scale=18), nullable=True)  # Average entry price
    mark_price = Column(Numeric(precision=30, scale=18), nullable=True)  # Current mark price

    # Real-time PnL data (can't be derived from trades alone)
    unrealized_pnl = Column(Numeric(precision=30, scale=18), nullable=True)  # From exchange
    percentage_pnl = Column(Numeric(precision=10, scale=6), nullable=True)  # PnL percentage

    # Leverage and margin info
    leverage = Column(Numeric(precision=10, scale=2), nullable=True)  # Position leverage
    initial_margin = Column(Numeric(precision=30, scale=18), nullable=True)  # Initial margin
    maintenance_margin = Column(Numeric(precision=30, scale=18), nullable=True)  # Maintenance margin

    # Fee tracking (exchange provides cumulative data)
    cumulative_funding_fees = Column(Numeric(precision=30, scale=18), nullable=False, default=0)  # Funding fees
    fee_currency = Column(String, nullable=True)  # Fee currency (usually USDT)

    # Reconciliation fields (calculated from our trade data)
    calculated_size = Column(Numeric(precision=30, scale=18), nullable=True)  # Size from our trades
    calculated_entry_price = Column(Numeric(precision=30, scale=18), nullable=True)  # Entry from our trades
    size_difference = Column(Numeric(precision=30, scale=18), nullable=True)  # Difference for reconciliation

    # Additional metadata
    exchange_position_id = Column(String, nullable=True, index=True)  # Exchange position ID
    is_reconciled = Column(String, nullable=False, default="PENDING")  # RECONCILED, MISMATCH, PENDING


class FundingPayment(Base):
    __tablename__ = "funding_payments"

    id = Column(Integer, primary_key=True, index=True)

    # Payment identification
    funding_payment_id = Column(String, nullable=False, unique=True, index=True)

    # Timestamps
    timestamp = Column(TIMESTAMP(timezone=True), nullable=False, index=True)

    # Account and connector info
    account_name = Column(String, nullable=False, index=True)
    connector_name = Column(String, nullable=False, index=True)

    # Funding details
    trading_pair = Column(String, nullable=False, index=True)
    funding_rate = Column(Numeric(precision=20, scale=18), nullable=False)  # Funding rate
    funding_payment = Column(Numeric(precision=30, scale=18), nullable=False)  # Payment amount
    fee_currency = Column(String, nullable=False)  # Payment currency (usually USDT)

    # Position association
    position_size = Column(Numeric(precision=30, scale=18), nullable=True)  # Position size at time of payment
    position_side = Column(String, nullable=True)  # LONG, SHORT

    # Additional metadata
    exchange_funding_id = Column(String, nullable=True, index=True)  # Exchange funding ID


class BotRun(Base):
    __tablename__ = "bot_runs"

    id = Column(Integer, primary_key=True, index=True)

    # Bot identification
    bot_name = Column(String, nullable=False, index=True)
    instance_name = Column(String, nullable=False, index=True)

    # Deployment info
    deployed_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False, index=True)
    strategy_type = Column(String, nullable=False, index=True)  # 'script' or 'controller'
    strategy_name = Column(String, nullable=False, index=True)
    config_name = Column(String, nullable=True, index=True)

    # Runtime tracking
    stopped_at = Column(TIMESTAMP(timezone=True), nullable=True, index=True)

    # Status tracking
    deployment_status = Column(String, nullable=False, default="DEPLOYED", index=True)  # DEPLOYED, FAILED, ARCHIVED
    run_status = Column(String, nullable=False, default="CREATED", index=True)  # CREATED, RUNNING, STOPPED, ERROR

    # Configuration and final state
    deployment_config = Column(Text, nullable=True)  # JSON of full deployment config
    final_status = Column(Text, nullable=True)  # JSON of final bot state, performance, etc.

    # Account info
    account_name = Column(String, nullable=False, index=True)

    # Metadata
    image_version = Column(String, nullable=True, index=True)
    error_message = Column(Text, nullable=True)


class GatewaySwap(Base):
    __tablename__ = "gateway_swaps"

    id = Column(Integer, primary_key=True, index=True)

    # Transaction identification
    transaction_hash = Column(String, nullable=False, unique=True, index=True)

    # Timestamps
    timestamp = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False, index=True)

    # Network and connector info (unified format)
    network = Column(String, nullable=False, index=True)  # chain-network format: solana-mainnet-beta, ethereum-mainnet
    connector = Column(String, nullable=False, index=True)  # jupiter, 0x, etc.
    wallet_address = Column(String, nullable=False, index=True)

    # Swap details
    trading_pair = Column(String, nullable=False, index=True)
    base_token = Column(String, nullable=False, index=True)
    quote_token = Column(String, nullable=False, index=True)
    side = Column(String, nullable=False)  # BUY, SELL

    # Amounts
    input_amount = Column(Numeric(precision=30, scale=18), nullable=False)
    output_amount = Column(Numeric(precision=30, scale=18), nullable=False)
    price = Column(Numeric(precision=30, scale=18), nullable=False)

    # Slippage and fees
    slippage_pct = Column(Numeric(precision=10, scale=6), nullable=True)
    gas_fee = Column(Numeric(precision=30, scale=18), nullable=True)
    gas_token = Column(String, nullable=True)  # SOL, ETH, etc.

    # Status
    status = Column(String, nullable=False, default="SUBMITTED", index=True)  # SUBMITTED, CONFIRMED, FAILED

    # Pool information (optional)
    pool_address = Column(String, nullable=True, index=True)

    # Additional metadata
    quote_id = Column(String, nullable=True)  # If swap was from a quote
    error_message = Column(Text, nullable=True)


class GatewayCLMMPosition(Base):
    __tablename__ = "gateway_clmm_positions"

    id = Column(Integer, primary_key=True, index=True)

    # Position identification
    position_address = Column(String, nullable=False, unique=True, index=True)  # CLMM position NFT address
    pool_address = Column(String, nullable=False, index=True)

    # Network and connector info (unified format)
    network = Column(String, nullable=False, index=True)  # chain-network format: solana-mainnet-beta, ethereum-mainnet
    connector = Column(String, nullable=False, index=True)  # meteora, raydium, uniswap
    wallet_address = Column(String, nullable=False, index=True)

    # Position pair
    trading_pair = Column(String, nullable=False, index=True)
    base_token = Column(String, nullable=False, index=True)
    quote_token = Column(String, nullable=False, index=True)

    # Timestamps
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False, index=True)
    closed_at = Column(TIMESTAMP(timezone=True), nullable=True, index=True)

    # Status
    status = Column(String, nullable=False, default="OPEN", index=True)  # OPEN, CLOSED

    # Price range (CLMM)
    lower_price = Column(Numeric(precision=30, scale=18), nullable=False)
    upper_price = Column(Numeric(precision=30, scale=18), nullable=False)
    lower_bin_id = Column(Integer, nullable=True)  # For bin-based CLMM (Meteora)
    upper_bin_id = Column(Integer, nullable=True)

    # Price tracking for PnL calculation
    entry_price = Column(Numeric(precision=30, scale=18), nullable=True)  # Pool price when position opened
    current_price = Column(Numeric(precision=30, scale=18),
                           nullable=True)  # Latest price (becomes close price when closed)

    # Initial deposit amounts (for PnL calculation)
    initial_base_token_amount = Column(Numeric(precision=30, scale=18), nullable=True)
    initial_quote_token_amount = Column(Numeric(precision=30, scale=18), nullable=True)

    # Position rent (SOL locked for position NFT, returned on close)
    position_rent = Column(Numeric(precision=30, scale=18), nullable=True)
    # What the chain actually refunded when the position account was closed. Kept
    # alongside position_rent rather than replacing it so the two can be compared —
    # a close refunding less than was locked means an account was left behind.
    position_rent_refunded = Column(Numeric(precision=30, scale=18), nullable=True)

    # Current liquidity amounts
    base_token_amount = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    quote_token_amount = Column(Numeric(precision=30, scale=18), nullable=False, default=0)

    # In range status
    in_range = Column(String, nullable=False, default="UNKNOWN")  # IN_RANGE, OUT_OF_RANGE, UNKNOWN

    # Price range percentage: (upper_price - lower_price) / lower_price
    percentage = Column(Numeric(precision=10, scale=6), nullable=True)

    # Accumulated fees (CLMM)
    base_fee_collected = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    quote_fee_collected = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    base_fee_pending = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    quote_fee_pending = Column(Numeric(precision=30, scale=18), nullable=False, default=0)

    # Last update timestamp
    last_updated = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    events = relationship("GatewayCLMMEvent", back_populates="position", cascade="all, delete-orphan")


class GatewayCLMMEvent(Base):
    __tablename__ = "gateway_clmm_events"

    id = Column(Integer, primary_key=True, index=True)
    position_id = Column(Integer, ForeignKey("gateway_clmm_positions.id"), nullable=False)

    # Event identification
    transaction_hash = Column(String, nullable=False, index=True)

    # Timestamps
    timestamp = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False, index=True)

    # Event type
    event_type = Column(String, nullable=False,
                        index=True)  # OPEN, ADD_LIQUIDITY, REMOVE_LIQUIDITY, COLLECT_FEES, CLOSE,
    # DISCOVERED (written by the poller with a synthetic tx hash)

    # Event amounts
    base_token_amount = Column(Numeric(precision=30, scale=18), nullable=True)
    quote_token_amount = Column(Numeric(precision=30, scale=18), nullable=True)

    # For fee collection
    base_fee_collected = Column(Numeric(precision=30, scale=18), nullable=True)
    quote_fee_collected = Column(Numeric(precision=30, scale=18), nullable=True)

    # Gas fee
    gas_fee = Column(Numeric(precision=30, scale=18), nullable=True)
    gas_token = Column(String, nullable=True)

    # Status
    status = Column(String, nullable=False, default="SUBMITTED", index=True)  # SUBMITTED, CONFIRMED, FAILED
    error_message = Column(Text, nullable=True)

    # Relationship
    position = relationship("GatewayCLMMPosition", back_populates="events")


class GatewayAMMPosition(Base):
    """A Meteora DAMM v2 position — an NFT with its own identity, tracked like a CLMM one.

    Only connectors whose positions are NFTs get rows here. Fungible-LP AMMs (Raydium CPMM,
    Uniswap/PancakeSwap V2) have no per-position identity to key on: their holdings are the
    LP token balance, read live, and their history is gateway_amm_events alone.
    """
    __tablename__ = "gateway_amm_positions"

    id = Column(Integer, primary_key=True, index=True)

    position_address = Column(String, nullable=False, unique=True, index=True)  # position NFT
    pool_address = Column(String, nullable=False, index=True)

    network = Column(String, nullable=False, index=True)  # chain-network format
    connector = Column(String, nullable=False, index=True)
    wallet_address = Column(String, nullable=False, index=True)

    base_token = Column(String, nullable=False, index=True)
    quote_token = Column(String, nullable=False, index=True)
    trading_pair = Column(String, nullable=False, index=True)

    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False, index=True)
    closed_at = Column(TIMESTAMP(timezone=True), nullable=True, index=True)
    status = Column(String, nullable=False, default="OPEN", index=True)  # OPEN, CLOSED

    # Deposited capital (the PnL baseline) and what the position currently holds. These
    # move together on every add and remove — see the CLMM repository for why letting
    # them drift reports a loss or gain of exactly the amount transacted.
    initial_base_token_amount = Column(Numeric(precision=30, scale=18), nullable=True)
    initial_quote_token_amount = Column(Numeric(precision=30, scale=18), nullable=True)
    base_token_amount = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    quote_token_amount = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    lp_token_amount = Column(Numeric(precision=30, scale=18), nullable=True)

    # Rent for the position account, locked on open and returned by the chain on close.
    # DAMM v2 positions are NFTs with their own account, so they carry rent exactly as
    # CLMM positions do — it is not liquidity, and a close that refunds less than was
    # locked means an account was left behind.
    position_rent = Column(Numeric(precision=30, scale=18), nullable=True)
    position_rent_refunded = Column(Numeric(precision=30, scale=18), nullable=True)

    entry_price = Column(Numeric(precision=30, scale=18), nullable=True)  # base-weighted across adds
    current_price = Column(Numeric(precision=30, scale=18), nullable=True)

    last_updated = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class GatewayAMMEvent(Base):
    """One AMM liquidity write — the AMM history, for every connector.

    Fungible-LP AMMs have only this: no position identity to key a row on, so holdings
    come from Gateway live and cost basis reconstructs from the log. Meteora DAMM v2
    positions are NFTs and additionally get a GatewayAMMPosition row, which these events
    reference by position_address.
    """
    __tablename__ = "gateway_amm_events"

    id = Column(Integer, primary_key=True, index=True)

    transaction_hash = Column(String, nullable=False, index=True)
    timestamp = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False, index=True)

    # Venue. No foreign key: there is no AMM positions table to point at.
    connector = Column(String, nullable=False, index=True)
    network = Column(String, nullable=False, index=True)  # chain-network format
    wallet_address = Column(String, nullable=False, index=True)
    pool_address = Column(String, nullable=False, index=True)
    # Meteora DAMM v2 positions are NFTs; fungible-LP AMMs leave this null.
    position_address = Column(String, nullable=True, index=True)

    event_type = Column(String, nullable=False, index=True)  # ADD_LIQUIDITY, REMOVE_LIQUIDITY, CREATE_POOL

    base_token_amount = Column(Numeric(precision=30, scale=18), nullable=True)
    quote_token_amount = Column(Numeric(precision=30, scale=18), nullable=True)

    # Pool price when the write landed (quote per base). Without it a fungible-LP AMM has
    # no cost basis anywhere: those connectors get no position row, so this column is the
    # only record of what price the capital went in or out at.
    price = Column(Numeric(precision=30, scale=18), nullable=True)

    gas_fee = Column(Numeric(precision=30, scale=18), nullable=True)
    gas_token = Column(String, nullable=True)

    status = Column(String, nullable=False, default="SUBMITTED", index=True)  # SUBMITTED, CONFIRMED, FAILED
    error_message = Column(Text, nullable=True)


class ControllerPerformanceSnapshot(Base):
    """Periodic snapshot of controller performance and custom_info from bots."""
    __tablename__ = "controller_performance_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False, index=True)
    bot_name = Column(String, nullable=False, index=True)
    controller_id = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False)  # running, error, stopped
    performance = Column(Text, nullable=True)  # JSON dict of performance metrics
    custom_info = Column(Text, nullable=True)  # JSON dict of custom info


class ExecutorPerformanceSnapshot(Base):
    """Periodic snapshot of a live executor's performance, plus one terminal row.

    Written by ExecutorService: on the snapshot tick for everything in
    _active_executors, and once more at completion with is_terminal=True. The terminal
    row is what makes a closed executor's series answerable from this table alone --
    no join to ExecutorRecord, and no exposure to the two paths that leave that record's
    metrics at their creation-time zeros (the startup reap, and the except branch in
    _persist_executor_completed).

    Deliberately narrow and typed, unlike controller_performance_snapshots: ExecutorInfo
    is named field by field all over this repo, while the controller's PerformanceReport
    is versioned by the core and has to be blobbed. Typing it also keeps the heavy
    custom_info payloads (fill_events, levels_by_state, ...) out of a per-minute row.
    """
    __tablename__ = "executor_performance_snapshots"
    __table_args__ = (
        # The hot query is WHERE executor_id = ? ORDER BY timestamp DESC -- one executor's
        # series, which is what both the history route and the reap lookup ask for.
        Index("ix_exec_perf_executor_timestamp", "executor_id", "timestamp"),
    )

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False, index=True)

    # Identity, denormalized from ExecutorRecord so a series needs no join.
    executor_id = Column(String, nullable=False, index=True)
    executor_type = Column(String, nullable=False, index=True)
    account_name = Column(String, nullable=False, index=True)
    connector_name = Column(String, nullable=False)
    trading_pair = Column(String, nullable=False)
    controller_id = Column(String, nullable=False, default="main", index=True)

    status = Column(String, nullable=False)  # RunnableStatus name
    close_type = Column(String, nullable=True)  # only ever set on the terminal row
    is_terminal = Column(Boolean, nullable=False, default=False, index=True)

    # The four ExecutorInfo metrics, same precision as ExecutorRecord. There is NO
    # separate volume column: filled_amount_quote IS the volume traded, on every executor
    # type including LP -- see test_executor_volume_is_the_filled_amount.py.
    net_pnl_quote = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    net_pnl_pct = Column(Numeric(precision=10, scale=6), nullable=False, default=0)
    cum_fees_quote = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    filled_amount_quote = Column(Numeric(precision=30, scale=18), nullable=False, default=0)


class ExecutorRecord(Base):
    """Database model for executor state persistence."""
    __tablename__ = "executors"

    id = Column(Integer, primary_key=True, index=True)

    # Executor identification
    executor_id = Column(String, nullable=False, unique=True, index=True)
    executor_type = Column(String, nullable=False, index=True)

    # Account and connector info
    account_name = Column(String, nullable=False, index=True)
    connector_name = Column(String, nullable=False, index=True)
    trading_pair = Column(String, nullable=False, index=True)
    controller_id = Column(String, nullable=False, default="main", index=True)

    # Timestamps
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False, index=True)
    closed_at = Column(TIMESTAMP(timezone=True), nullable=True, index=True)

    # Status
    status = Column(String, nullable=False, default="RUNNING", index=True)
    close_type = Column(String, nullable=True)

    # Performance metrics
    net_pnl_quote = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    net_pnl_pct = Column(Numeric(precision=10, scale=6), nullable=False, default=0)
    cum_fees_quote = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    filled_amount_quote = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    # Trading volume generated. The same number as filled_amount_quote for any executor
    # that places orders, and deliberately not for an LP executor, whose filled amount is
    # the capital it deposited — depositing capital trades nothing.

    # Error tracking
    error_log = Column(Text, nullable=True)  # JSON: last errors captured during execution

    # Configuration (JSON)
    config = Column(Text, nullable=True)

    # Final state (JSON)
    final_state = Column(Text, nullable=True)

    # Relationships
    orders = relationship("ExecutorOrder", back_populates="executor", cascade="all, delete-orphan")


class PositionHoldRecord(Base):
    """Database model for position hold tracking (separate from executor lifecycle)."""
    __tablename__ = "position_holds"
    __table_args__ = (
        UniqueConstraint(
            "account_name", "connector_name", "trading_pair", "controller_id",
            name="uq_position_hold_key"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)

    # Position identification
    account_name = Column(String, nullable=False, index=True)
    connector_name = Column(String, nullable=False, index=True)
    trading_pair = Column(String, nullable=False, index=True)
    controller_id = Column(String, nullable=False, default="main", index=True)

    # Aggregated amounts
    buy_amount_base = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    buy_amount_quote = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    sell_amount_base = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    sell_amount_quote = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    realized_pnl_quote = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    cum_fees_quote = Column(Numeric(precision=30, scale=18), nullable=False, default=0)

    # Tracking
    executor_ids = Column(Text, nullable=True)  # JSON array of executor IDs
    status = Column(String, nullable=False, default="ACTIVE", index=True)  # ACTIVE, CLEARED

    # Timestamps
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False, index=True)
    last_updated = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    cleared_at = Column(TIMESTAMP(timezone=True), nullable=True)


class ExecutorOrder(Base):
    """Database model for orders created by executors."""
    __tablename__ = "executor_orders"

    id = Column(Integer, primary_key=True, index=True)

    # Executor reference
    executor_id = Column(String, ForeignKey("executors.executor_id"), nullable=False, index=True)

    # Order identification
    client_order_id = Column(String, nullable=False, index=True)
    exchange_order_id = Column(String, nullable=True)

    # Order details
    order_type = Column(String, nullable=False)  # open, close, take_profit, stop_loss
    trade_type = Column(String, nullable=False)  # BUY, SELL
    amount = Column(Numeric(precision=30, scale=18), nullable=False)
    price = Column(Numeric(precision=30, scale=18), nullable=True)

    # Execution
    status = Column(String, nullable=False, default="SUBMITTED")
    filled_amount = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    average_fill_price = Column(Numeric(precision=30, scale=18), nullable=True)

    # Timestamps
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), onupdate=func.now(), nullable=True)

    # Relationship
    executor = relationship("ExecutorRecord", back_populates="orders")
