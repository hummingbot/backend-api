"""
Patch script and runtime monkey-patch helper for updating Derive exchange connector
in Hummingbot environment to track options contract positions alongside collateral balances.
"""
from decimal import Decimal
import os
import sys
from typing import Dict, Optional


def apply_derive_options_patch() -> bool:
    """
    Applies options contract position tracking and mark price retrieval methods
    directly to `DeriveExchange` class in Python memory.
    Safe and idempotent to call on startup.
    """
    try:
        from hummingbot.connector.exchange.derive.derive_exchange import DeriveExchange
        from hummingbot.connector.exchange.derive import derive_constants as CONSTANTS
    except ImportError:
        return False

    if hasattr(DeriveExchange, "get_token_price") and hasattr(DeriveExchange, "get_option_positions"):
        return True

    orig_init = DeriveExchange.__init__

    def patched_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        self._position_mark_prices = {}
        self._option_positions = {}

    async def patched_update_balances(self):
        """
        Calls the REST API to update total and available balances (collaterals and active positions).
        """
        local_asset_names = set(self._account_balances.keys())
        remote_asset_names = set()

        account_info = await self._api_post(
            path_url=CONSTANTS.ACCOUNTS_PATH_URL,
            data={"subaccount_id": self._sub_id},
            is_auth_required=True
        )
        if "error" in account_info:
            self.logger().error(f"Error fetching account balances: {account_info['error']['message']}")
            raise Exception(account_info["error"]["message"])
        else:
            balances = account_info["result"].get("collaterals", [])
            for balance_entry in balances:
                asset_name = balance_entry["asset_name"]
                free_balance = Decimal(str(balance_entry["amount"]))
                total_balance = Decimal(str(balance_entry["amount"]))
                self._account_available_balances[asset_name] = free_balance
                self._account_balances[asset_name] = total_balance
                remote_asset_names.add(asset_name)

            positions = account_info["result"].get("positions", [])
            if not hasattr(self, "_position_mark_prices"):
                self._position_mark_prices = {}
            if not hasattr(self, "_option_positions"):
                self._option_positions = {}

            self._position_mark_prices.clear()
            self._option_positions.clear()

            for pos in positions:
                instrument_name = pos.get("instrument_name")
                if not instrument_name:
                    continue
                amount = Decimal(str(pos.get("amount", 0)))
                if amount != 0:
                    mark_price = Decimal(str(pos.get("mark_price", 0)))
                    self._account_available_balances[instrument_name] = amount
                    self._account_balances[instrument_name] = amount
                    self._position_mark_prices[instrument_name] = mark_price
                    self._option_positions[instrument_name] = {
                        "instrument_name": instrument_name,
                        "instrument_type": pos.get("instrument_type"),
                        "amount": amount,
                        "mark_price": mark_price,
                        "mark_value": Decimal(str(pos.get("mark_value", 0))),
                        "unrealized_pnl": Decimal(str(pos.get("unrealized_pnl", 0))),
                        "index_price": Decimal(str(pos.get("index_price", 0))),
                        "delta": Decimal(str(pos.get("delta", 0))),
                        "gamma": Decimal(str(pos.get("gamma", 0))),
                        "theta": Decimal(str(pos.get("theta", 0))),
                        "vega": Decimal(str(pos.get("vega", 0))),
                    }
                    remote_asset_names.add(instrument_name)

            asset_names_to_remove = local_asset_names.difference(remote_asset_names)
            for asset_name in asset_names_to_remove:
                del self._account_available_balances[asset_name]
                del self._account_balances[asset_name]

    def get_token_price(self, token: str) -> Optional[Decimal]:
        """
        Returns the mark price for an option position or token if available.
        """
        if hasattr(self, "_position_mark_prices") and token in self._position_mark_prices:
            return self._position_mark_prices[token]
        return None

    def get_option_positions(self) -> Dict[str, Dict]:
        """
        Returns cached option positions metadata.
        """
        if hasattr(self, "_option_positions"):
            return self._option_positions
        return {}

    DeriveExchange.__init__ = patched_init
    DeriveExchange._update_balances = patched_update_balances
    DeriveExchange.get_token_price = get_token_price
    DeriveExchange.get_option_positions = get_option_positions
    return True


TARGET_PATH = "/opt/conda/envs/hummingbot-api/lib/python3.12/site-packages/hummingbot/connector/exchange/derive/derive_exchange.py"

OLD_INIT_SNIPPET = """        self._instrument_ticker = []
        super().__init__(balance_asset_limit, rate_limits_share_pct)
        self.real_time_balance_update = False"""

NEW_INIT_SNIPPET = """        self._instrument_ticker = []
        self._position_mark_prices = {}
        self._option_positions = {}
        super().__init__(balance_asset_limit, rate_limits_share_pct)
        self.real_time_balance_update = False"""

OLD_UPDATE_BALANCES = """    async def _update_balances(self):
        \"\"\"
        Calls the REST API to update total and available balances.
        \"\"\"
        local_asset_names = set(self._account_balances.keys())
        remote_asset_names = set()

        account_info = await self._api_post(
            path_url=CONSTANTS.ACCOUNTS_PATH_URL,
            data={"subaccount_id": self._sub_id},
            is_auth_required=True)
        if "error" in account_info:
            self.logger().error(f"Error fetching account balances: {account_info['error']['message']}")
            raise
        else:
            balances = account_info["result"]["collaterals"]
            for balance_entry in balances:
                asset_name = balance_entry["asset_name"]
                free_balance = Decimal(balance_entry["amount"])
                total_balance = Decimal(balance_entry["amount"])
                self._account_available_balances[asset_name] = free_balance
                self._account_balances[asset_name] = total_balance
                remote_asset_names.add(asset_name)

            asset_names_to_remove = local_asset_names.difference(remote_asset_names)
            for asset_name in asset_names_to_remove:
                del self._account_available_balances[asset_name]
                del self._account_balances[asset_name]"""

NEW_UPDATE_BALANCES = """    async def _update_balances(self):
        \"\"\"
        Calls the REST API to update total and available balances (collaterals and active positions).
        \"\"\"
        local_asset_names = set(self._account_balances.keys())
        remote_asset_names = set()

        account_info = await self._api_post(
            path_url=CONSTANTS.ACCOUNTS_PATH_URL,
            data={"subaccount_id": self._sub_id},
            is_auth_required=True)
        if "error" in account_info:
            self.logger().error(f"Error fetching account balances: {account_info['error']['message']}")
            raise
        else:
            balances = account_info["result"].get("collaterals", [])
            for balance_entry in balances:
                asset_name = balance_entry["asset_name"]
                free_balance = Decimal(str(balance_entry["amount"]))
                total_balance = Decimal(str(balance_entry["amount"]))
                self._account_available_balances[asset_name] = free_balance
                self._account_balances[asset_name] = total_balance
                remote_asset_names.add(asset_name)

            positions = account_info["result"].get("positions", [])
            self._position_mark_prices.clear()
            self._option_positions.clear()

            for pos in positions:
                instrument_name = pos.get("instrument_name")
                if not instrument_name:
                    continue
                amount = Decimal(str(pos.get("amount", 0)))
                if amount != 0:
                    mark_price = Decimal(str(pos.get("mark_price", 0)))
                    self._account_available_balances[instrument_name] = amount
                    self._account_balances[instrument_name] = amount
                    self._position_mark_prices[instrument_name] = mark_price
                    self._option_positions[instrument_name] = {
                        "instrument_name": instrument_name,
                        "instrument_type": pos.get("instrument_type"),
                        "amount": amount,
                        "mark_price": mark_price,
                        "mark_value": Decimal(str(pos.get("mark_value", 0))),
                        "unrealized_pnl": Decimal(str(pos.get("unrealized_pnl", 0))),
                        "index_price": Decimal(str(pos.get("index_price", 0))),
                        "delta": Decimal(str(pos.get("delta", 0))),
                        "gamma": Decimal(str(pos.get("gamma", 0))),
                        "theta": Decimal(str(pos.get("theta", 0))),
                        "vega": Decimal(str(pos.get("vega", 0))),
                    }
                    remote_asset_names.add(instrument_name)

            asset_names_to_remove = local_asset_names.difference(remote_asset_names)
            for asset_name in asset_names_to_remove:
                del self._account_available_balances[asset_name]
                del self._account_balances[asset_name]

    def get_token_price(self, token: str) -> Optional[Decimal]:
        \"\"\"
        Returns the mark price for an option position or token if available.
        \"\"\"
        if hasattr(self, "_position_mark_prices") and token in self._position_mark_prices:
            return self._position_mark_prices[token]
        return None

    def get_option_positions(self) -> Dict[str, Dict]:
        \"\"\"
        Returns cached option positions metadata.
        \"\"\"
        if hasattr(self, "_option_positions"):
            return self._option_positions
        return {}"""


def patch_file(file_path: str):
    if not os.path.exists(file_path):
        print(f"Target file not found: {file_path}")
        return False

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    if "get_option_positions" in content:
        print("Derive exchange connector is already patched.")
        return True

    if OLD_INIT_SNIPPET not in content:
        print("OLD_INIT_SNIPPET not matched.")
        return False

    content = content.replace(OLD_INIT_SNIPPET, NEW_INIT_SNIPPET)

    if OLD_UPDATE_BALANCES not in content:
        print("OLD_UPDATE_BALANCES not matched.")
        return False

    content = content.replace(OLD_UPDATE_BALANCES, NEW_UPDATE_BALANCES)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

    print("Successfully patched Derive exchange connector!")
    return True


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else TARGET_PATH
    success = patch_file(target)
    sys.exit(0 if success else 1)
