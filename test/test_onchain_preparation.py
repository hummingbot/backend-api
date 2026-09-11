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


@pytest.mark.asyncio
async def test_admission_contention_retries_each_unsigned_call_without_replaying_context(monkeypatch):
    from aomi.pipeline.errors import PipelineError
    pause = AsyncMock()
    monkeypatch.setattr("services.onchain_preparation.asyncio.sleep", pause)
    c = client()
    busy = PipelineError(409, "operation_in_flight", "private detail")
    c.svm_context.side_effect = [busy, {"address": "wallet", "cluster": "mainnet-beta"}]
    c.invoke.side_effect = [busy, SimpleNamespace(result={"wallet": "wallet", "cluster": "mainnet-beta"}, build=None)]
    await prepare_onchain(OnchainPreparationRequest(operation="position"), c, application_id=123)
    assert c.svm_context.await_count == 2
    assert c.invoke.await_count == 2
    assert pause.await_count == 2


@pytest.mark.asyncio
async def test_persistent_contention_is_bounded_and_propagated(monkeypatch):
    from aomi.pipeline.errors import PipelineError
    from services.onchain_preparation import admitted_read
    pause = AsyncMock()
    monkeypatch.setattr("services.onchain_preparation.asyncio.sleep", pause)
    busy = PipelineError(409, "operation_in_flight", "busy")
    call = AsyncMock(side_effect=busy)
    with pytest.raises(PipelineError) as caught:
        await admitted_read(call)
    assert caught.value is busy
    assert call.await_count == 6
    assert sum(args.args[0] for args in pause.await_args_list) == pytest.approx(4.65)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(0, "transport_error"), (408, "timeout"), (429, "rate_limit"),
                                         (500, "backend_error"), (502, "backend_unavailable"),
                                         (409, "build_changed"), (403, "operation_in_flight")])
async def test_unknown_outcome_or_other_rejection_is_never_retried(status, code, monkeypatch):
    from aomi.pipeline.errors import PipelineError
    from services.onchain_preparation import admitted_read
    pause = AsyncMock()
    monkeypatch.setattr("services.onchain_preparation.asyncio.sleep", pause)
    error = PipelineError(status, code, "private upstream detail")
    call = AsyncMock(side_effect=error)
    with pytest.raises(PipelineError) as caught:
        await admitted_read(call)
    assert caught.value is error
    call.assert_awaited_once()
    pause.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code,expected", [(409, "operation_in_flight", 409),
                                                  (502, "backend_unavailable", 502)])
async def test_router_reports_busy_and_keeps_upstream_details_private(status, code, expected, monkeypatch, caplog):
    from unittest.mock import MagicMock
    from aomi.pipeline.errors import PipelineError
    from fastapi import HTTPException
    from routers.executors import onchain_preparation
    from services.onchain_executor import OnchainExecutor
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client())
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(OnchainExecutor, "_default_client", lambda: context)
    failure = PipelineError(status, code, "PRIVATE_URL_AND_BODY", details={"secret": "PRIVATE_TOKEN"})
    monkeypatch.setattr("services.onchain_preparation.prepare_onchain", AsyncMock(side_effect=failure))
    with pytest.raises(HTTPException) as caught:
        await onchain_preparation(OnchainPreparationRequest(operation="venues"))
    assert caught.value.status_code == expected
    assert "PRIVATE_" not in str(caught.value.detail) + caplog.text
    if expected == 409:
        assert "busy" in caught.value.detail
