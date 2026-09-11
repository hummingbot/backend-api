"""An archived bot that never filled a trade must not 500 its performance routes.

A bot whose order size sits below the exchange's minimum notional has every order
rejected and archives with an empty TradeFill table. `pd.read_sql_query` gives a
zero-row table `object` columns -- there is nothing to infer a dtype from -- and
`.cumsum()` refuses object dtype however empty the frame is, so
/archived-bots/{db}/performance and /summary both answered 500 with
"cumsum is not supported for object dtype" while /executors and /orders on the same
archive answered fine.

Run with: pytest test/test_archived_bot_zero_fill.py -v
"""
import sqlite3

import pytest

pytest.importorskip("pandas")

from utils.hummingbot_database_reader import HummingbotDatabase  # noqa: E402

# The columns each reader touches, in the types the real hummingbot schema declares.
SCHEMA = """
CREATE TABLE TradeFill (
    config_file_path VARCHAR, strategy VARCHAR, market VARCHAR, symbol VARCHAR,
    base_asset VARCHAR, quote_asset VARCHAR, timestamp BIGINT, order_id VARCHAR,
    trade_type VARCHAR, order_type VARCHAR, price BIGINT, amount BIGINT,
    leverage INTEGER, trade_fee VARCHAR, trade_fee_in_quote BIGINT,
    exchange_trade_id VARCHAR, position VARCHAR
);
CREATE TABLE "Order" (
    id VARCHAR, config_file_path VARCHAR, strategy VARCHAR, market VARCHAR,
    symbol VARCHAR, base_asset VARCHAR, quote_asset VARCHAR,
    creation_timestamp BIGINT, order_type VARCHAR, amount BIGINT, leverage INTEGER,
    price BIGINT, last_status VARCHAR, last_update_timestamp BIGINT,
    exchange_order_id VARCHAR, position VARCHAR
);
CREATE TABLE OrderStatus (id INTEGER, order_id VARCHAR, timestamp BIGINT, status VARCHAR);
CREATE TABLE Executors (
    id VARCHAR, timestamp BIGINT, type VARCHAR, close_timestamp BIGINT,
    close_type INTEGER, status INTEGER, config VARCHAR, net_pnl_pct FLOAT,
    net_pnl_quote FLOAT, cum_fees_quote FLOAT, filled_amount_quote FLOAT,
    is_active BOOLEAN, is_trading BOOLEAN, custom_info VARCHAR, controller_id VARCHAR
);
CREATE TABLE Controllers (id VARCHAR, controller_id VARCHAR, timestamp BIGINT, config VARCHAR);
CREATE TABLE Position (
    id INTEGER, controller_id VARCHAR, connector_name VARCHAR, trading_pair VARCHAR,
    side VARCHAR, timestamp BIGINT, volume_traded_quote BIGINT, amount BIGINT,
    breakeven_price BIGINT, unrealized_pnl_quote BIGINT, cum_fees_quote BIGINT
);
"""


@pytest.fixture
def zero_fill_db(tmp_path):
    """An archive of a bot whose every order was rejected: rows in Order, none in TradeFill."""
    path = tmp_path / "zero_fill_repro.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute(
        'INSERT INTO "Order" (id, config_file_path, strategy, market, symbol, base_asset, '
        'quote_asset, creation_timestamp, order_type, amount, leverage, price, last_status, '
        'last_update_timestamp, exchange_order_id, position) VALUES '
        "('o-1', 'conf.yml', 'v2_with_controllers', 'binance_perpetual_testnet', 'BTC-USDT', "
        "'BTC', 'USDT', 1757000000000, 'LIMIT', 235000, 5, 100000000000, 'FAILED', "
        "1757000000000, NULL, 'NIL')"
    )
    connection.commit()
    connection.close()
    return str(path)


class TestTheReaderSurvivesAnEmptyTable:
    def test_trade_fills_of_an_empty_table_do_not_raise(self, zero_fill_db):
        """The reported crash: cumsum on the object-dtype columns of a zero-row read."""
        trade_fills = HummingbotDatabase(zero_fill_db).get_trade_fills()

        assert len(trade_fills) == 0
        assert "cum_fees_in_quote" in trade_fills.columns
        assert "trade_fee" in trade_fills.columns

    def test_the_scaled_columns_are_numeric_whatever_the_row_count(self, zero_fill_db):
        """Object dtype is the defect, not the symptom: it breaks any later arithmetic."""
        db = HummingbotDatabase(zero_fill_db)

        trade_fills = db.get_trade_fills()
        for column in ["amount", "price", "trade_fee_in_quote"]:
            assert trade_fills[column].dtype != object, column

        positions = db.get_positions()
        for column in ["volume_traded_quote", "amount", "breakeven_price",
                       "unrealized_pnl_quote", "cum_fees_quote"]:
            assert positions[column].dtype != object, column

    def test_performance_of_a_zero_fill_archive_is_an_empty_frame_not_a_raise(self, zero_fill_db):
        performance = HummingbotDatabase(zero_fill_db).calculate_trade_based_performance()

        assert len(performance) == 0

    def test_a_filled_archive_still_scales_and_accumulates(self, zero_fill_db):
        """The coercion must not change what a table with rows in it reports."""
        connection = sqlite3.connect(zero_fill_db)
        connection.execute(
            "INSERT INTO TradeFill (config_file_path, strategy, market, symbol, base_asset, "
            "quote_asset, timestamp, order_id, trade_type, order_type, price, amount, "
            "leverage, trade_fee, trade_fee_in_quote, exchange_trade_id, position) VALUES "
            "('conf.yml', 'v2', 'binance_perpetual_testnet', 'BTC-USDT', 'BTC', 'USDT', "
            "1757000000000, 'o-1', 'BUY', 'LIMIT', 100000000000, 2000000, 5, '{}', 500000, "
            "'t-1', 'NIL'), "
            "('conf.yml', 'v2', 'binance_perpetual_testnet', 'BTC-USDT', 'BTC', 'USDT', "
            "1757000060000, 'o-2', 'SELL', 'LIMIT', 101000000000, 2000000, 5, '{}', 700000, "
            "'t-2', 'NIL')"
        )
        connection.commit()
        connection.close()

        trade_fills = HummingbotDatabase(zero_fill_db).get_trade_fills()

        assert len(trade_fills) == 2
        assert trade_fills["price"].tolist() == [100000.0, 101000.0]
        assert trade_fills["amount"].tolist() == [2.0, 2.0]
        assert trade_fills["cum_fees_in_quote"].tolist() == [0.5, 1.2]


class TestTheRoutesAnswerTheZeroFillArchive:
    """The two routes David saw 500, exercised through their own bodies."""

    @pytest.fixture(autouse=True)
    def _resolve_to_the_fixture(self, monkeypatch, zero_fill_db):
        import routers.archived_bots as archived_bots

        monkeypatch.setattr(archived_bots, "_validate_db_path", lambda db_path: zero_fill_db)

    @pytest.mark.asyncio
    async def test_summary_counts_the_orders_and_reports_no_trades(self):
        from routers.archived_bots import get_database_summary

        summary = await get_database_summary("archived/zero_fill_repro.sqlite")

        assert summary["total_trades"] == 0
        assert summary["total_orders"] == 1
        assert summary["trading_pairs"] == ["BTC-USDT"]

    @pytest.mark.asyncio
    async def test_performance_says_there_are_no_trades(self):
        from routers.archived_bots import get_database_performance

        performance = await get_database_performance("archived/zero_fill_repro.sqlite")

        assert performance["performance_data"] == []
        assert performance["error"] == "No trades found in database"
