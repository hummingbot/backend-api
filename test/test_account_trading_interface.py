from unittest.mock import MagicMock

from services.trading_service import AccountTradingInterface


def test_account_trading_interface_exposes_market_data_provider():
    connector_service = MagicMock()
    market_data_service = MagicMock()

    trading_interface = AccountTradingInterface(
        connector_service=connector_service,
        market_data_service=market_data_service,
        account_name="test_account",
    )

    assert trading_interface.market_data_provider is market_data_service
