from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from models.onchain_preparation import OnchainPreparationRequest
from services.onchain_preparation import prepare_onchain


def client(payload=None, build=None, cluster="mainnet-beta"):
    return SimpleNamespace(
        svm_context=AsyncMock(return_value={"address": "wallet", "cluster": cluster, "rpc_endpoint": "private-rpc"}),
        invoke=AsyncMock(return_value=SimpleNamespace(result=payload or {"wallet": "wallet", "cluster": cluster}, build=build)),
    )


@pytest.mark.asyncio
async def test_preparation_uses_connected_wallet_and_does_not_expose_rpc():
    c = client()
    request = OnchainPreparationRequest(operation="prepare", arguments={"venue": "kamino-earn", "market": "vault"})
    result = await prepare_onchain(request, c, application_id=123)
    c.invoke.assert_awaited_once_with(
        "solana-defi", "solana_defi_prepare",
        {"venue": "kamino-earn", "market": "vault", "wallet": "wallet"}, application_id=123,
    )
    assert result["wallet"] == "wallet"
    assert "private-rpc" not in str(result)
    assert "wallet" not in request.arguments


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments,cluster", [({"wallet": "other"}, "mainnet-beta"), ({}, "devnet")])
async def test_wallet_and_network_mismatch_refuse_before_app_invocation(arguments, cluster):
    c = client(cluster=cluster)
    with pytest.raises(ValueError):
        await prepare_onchain(OnchainPreparationRequest(operation="prepare", arguments=arguments), c, application_id=123)
    c.invoke.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,build", [
    ({"wallet": "other", "cluster": "mainnet-beta"}, None),
    ({"wallet": "wallet", "cluster": "devnet"}, None), ({}, object()),
])
async def test_invalid_or_staged_app_response_is_not_an_approved_plan(payload, build):
    with pytest.raises(ValueError):
        await prepare_onchain(OnchainPreparationRequest(operation="prepare"), client(payload, build), application_id=123)


def test_arbitrary_operations_and_fields_are_not_accepted():
    with pytest.raises(ValidationError):
        OnchainPreparationRequest(operation="commit")
    with pytest.raises(ValidationError):
        OnchainPreparationRequest(operation="prepare", url="http://other-service")


@pytest.mark.asyncio
@pytest.mark.parametrize("application_id", [None, 0, -1, True, "123"])
async def test_missing_or_invalid_registered_identity_refuses_before_any_remote_call(application_id):
    c = client()
    with pytest.raises(ValueError, match="application ID"):
        await prepare_onchain(OnchainPreparationRequest(operation="venues"), c, application_id=application_id)
    c.svm_context.assert_not_awaited()
    c.invoke.assert_not_awaited()
