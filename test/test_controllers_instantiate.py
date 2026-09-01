"""A controller must actually construct against the wheel it runs on.

`lp_rebalancer.__init__` called `parse_provider(config.lp_provider,
default_trading_type="clmm")`. The wheel had dropped that argument on purpose —
"the trading type is never defaulted: Gateway rejects a guessed one with a 400, so an
untyped provider must fail here rather than mid-operation" — and this caller was not
updated with it. Deploying the bot produced:

    ERROR - Error adding controller:
    parse_provider() got an unexpected keyword argument 'default_trading_type'

and then, because strategy_v2_base catches that and carries on, the bot came up healthy
with no controller at all: status "stopped", controller "N/A", and four cheerful INFO
lines about the network connecting. Nothing said the strategy was empty.

Importing the module is not enough to catch it — the module imported fine and the config
class resolved fine. Only construction runs `__init__`, which is where a caller and a
signature meet.
"""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("hummingbot")

from utils.file_system import fs_util  # noqa: E402

CONTROLLERS = Path(__file__).resolve().parent.parent / "bots" / "controllers"

# Enough of a config for each controller type to construct. Only controllers that need
# more than the base fields need an entry.
EXTRA_FIELDS = {
    "lp_rebalancer": {
        "connector_name": "solana-mainnet-beta",
        "trading_pair": "ANSEM-USDC",
        "lp_provider": "meteora/clmm",
        "pool_address": "BetLT47eFXDZnjM1cmZhQ4oNJkYaPZYH5yv6atfPfAri",
        "total_amount_quote": 100,
    },
}


def _package_controllers():
    """Controllers shipped as a package: a directory holding __init__.py AND a module of
    its own name, which is what makes it importable as `<type>.<name>`. `examples/` has an
    __init__.py and no such module, so it is a folder of controllers rather than one.
    lp_rebalancer is the only package controller today."""
    return sorted(
        (path.parent.parent.name, path.parent.name)
        for path in CONTROLLERS.rglob("*/__init__.py")
        if path.parent.parent.name in {"generic", "directional_trading", "market_making"}
        and (path.parent / f"{path.parent.name}.py").exists()
    )


def test_the_discovery_finds_the_package_controller():
    """A glob that quietly matched nothing would make the parametrised test vacuous."""
    assert ("generic", "lp_rebalancer") in _package_controllers()


@pytest.mark.parametrize("controller_type,name", _package_controllers())
def test_a_controller_constructs_against_the_installed_wheel(controller_type, name):
    config_class = fs_util.load_controller_config_class(controller_type, name)
    assert config_class is not None, f"{name} resolves to no config class"

    fields = {"id": "test", "controller_name": name, **EXTRA_FIELDS.get(name, {})}
    config = config_class(**fields)

    # market_data_provider and actions_queue are the two the base class takes.
    controller = config.get_controller_class()(config, MagicMock(), MagicMock())

    assert controller is not None


def test_lp_rebalancer_reads_the_provider_it_was_given():
    """The specific call that broke: the type comes from lp_provider, never a default."""
    config_class = fs_util.load_controller_config_class("generic", "lp_rebalancer")
    config = config_class(id="test", controller_name="lp_rebalancer",
                          **EXTRA_FIELDS["lp_rebalancer"])

    controller = config.get_controller_class()(config, MagicMock(), MagicMock())

    assert (controller.lp_dex_name, controller.lp_trading_type) == ("meteora", "clmm")


def test_an_untyped_provider_is_refused_rather_than_guessed():
    """Gateway 400s on a guessed trading type, so a provider with no type has to fail
    before the bot runs, not mid-operation. The core's parse_provider defaults an untyped
    provider to "router" — wrong branch entirely for an LP controller — so the config class
    owns the contract and rejects it at load, whichever wheel is installed."""
    config_class = fs_util.load_controller_config_class("generic", "lp_rebalancer")
    fields = {**EXTRA_FIELDS["lp_rebalancer"], "lp_provider": "meteora"}

    with pytest.raises(ValueError, match="expected 'name/type'"):
        config_class(id="test", controller_name="lp_rebalancer", **fields)
