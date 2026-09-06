"""
Unit and integration test for Derive options holdings parsing and valuation.
"""
import asyncio
import os
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("hummingbot")

from hummingbot.connector.exchange.derive.derive_exchange import DeriveExchange
from services.accounts_service import AccountsService
from utils.patch_derive_connector import apply_derive_options_patch

# Ensure Derive options patch is applied to DeriveExchange before tests run
apply_derive_options_patch()

# Scrubbed test credentials (can be overridden via env vars for integration testing)
TEST_DERIVE_API_KEY = os.getenv("TEST_DERIVE_API_KEY", "test_derive_api_key")
TEST_DERIVE_API_SECRET = os.getenv("TEST_DERIVE_API_SECRET", "test_derive_api_secret")
TEST_DERIVE_SUB_ID = int(os.getenv("TEST_DERIVE_SUB_ID", "10000"))


def create_test_derive_connector() -> DeriveExchange:
    """Helper to instantiate DeriveExchange with scrubbed test credentials."""
    apply_derive_options_patch()
    return DeriveExchange(
        derive_api_key=TEST_DERIVE_API_KEY,
        derive_api_secret=TEST_DERIVE_API_SECRET,
        sub_id=TEST_DERIVE_SUB_ID,
    )


@pytest.mark.asyncio
async def test_derive_exchange_positions_parsing():
    print("Testing DeriveExchange positions parsing...")
    connector = create_test_derive_connector()

    # Mock response matching Derive private/get_subaccount payload structure
    mock_api_response = {
        "result": {
            "collaterals": [
                {"asset_name": "USDC", "amount": "1000.50"}
            ],
            "positions": [
                {
                    "instrument_name": "ETH-20260925-3000-C",
                    "instrument_type": "option",
                    "amount": "2.5",
                    "mark_price": "250.75",
                    "mark_value": "626.875",
                    "unrealized_pnl": "50.25",
                    "index_price": "3200.00",
                    "delta": "0.55",
                    "gamma": "0.002",
                    "theta": "-4.5",
                    "vega": "15.0",
                },
                {
                    "instrument_name": "BTC-20241227-100000-P",
                    "instrument_type": "option",
                    "amount": "1.0",
                    "mark_price": "1200.00",
                    "mark_value": "1200.00",
                    "unrealized_pnl": "-100.00",
                    "index_price": "95000.00",
                    "delta": "-0.25",
                    "gamma": "0.0001",
                    "theta": "-10.0",
                    "vega": "25.0",
                }
            ]
        }
    }

    connector._api_post = AsyncMock(return_value=mock_api_response)

    await connector._update_balances()

    balances = connector.get_all_balances()
    print("Balances parsed:", balances)
    assert balances.get("USDC") == Decimal("1000.50")
    assert balances.get("ETH-20260925-3000-C") == Decimal("2.5")
    assert balances.get("BTC-20241227-100000-P") == Decimal("1.0")

    # Verify custom token price getter
    eth_opt_price = connector.get_token_price("ETH-20260925-3000-C")
    btc_opt_price = connector.get_token_price("BTC-20241227-100000-P")
    print("ETH Option Mark Price:", eth_opt_price)
    print("BTC Option Mark Price:", btc_opt_price)
    assert eth_opt_price == Decimal("250.75")
    assert btc_opt_price == Decimal("1200.00")

    # Verify option metadata
    option_positions = connector.get_option_positions()
    assert "ETH-20260925-3000-C" in option_positions
    assert option_positions["ETH-20260925-3000-C"]["delta"] == Decimal("0.55")

    print("DeriveExchange positions parsing test passed successfully!")


@pytest.mark.asyncio
async def test_accounts_service_token_info():
    print("Testing AccountsService token info for options...")
    db_manager = MagicMock()
    connector_service = MagicMock()
    market_data_service = MagicMock()
    market_data_service.get_rate_for_connector.return_value = None
    trading_service = MagicMock()

    accounts_service = AccountsService(
        db_manager=db_manager,
        connector_service=connector_service,
        market_data_service=market_data_service,
        trading_service=trading_service,
    )

    mock_connector = create_test_derive_connector()
    mock_connector._account_balances = {
        "USDC": Decimal("1000.50"),
        "ETH-20260925-3000-C": Decimal("2.5"),
    }
    mock_connector._account_available_balances = {
        "USDC": Decimal("1000.50"),
        "ETH-20260925-3000-C": Decimal("2.5"),
    }
    mock_connector._position_mark_prices = {
        "ETH-20260925-3000-C": Decimal("250.75"),
    }

    print("DEBUG mock_connector hasattr get_token_price:", hasattr(mock_connector, "get_token_price"))
    print("DEBUG mock_connector get_token_price:", mock_connector.get_token_price("ETH-20260925-3000-C"))

    tokens_info = await accounts_service._get_connector_tokens_info(
        connector=mock_connector,
        connector_name="derive",
        skip_balance_refresh=True
    )

    print("Tokens info generated:", tokens_info)
    eth_info = next(t for t in tokens_info if t["token"] == "ETH-20260925-3000-C")
    assert eth_info["price"] == 250.75
    assert eth_info["units"] == 2.5
    assert eth_info["value"] == 626.875

    print("AccountsService token info test passed successfully!")


if __name__ == "__main__":
    asyncio.run(test_derive_exchange_positions_parsing())
    asyncio.run(test_accounts_service_token_info())
