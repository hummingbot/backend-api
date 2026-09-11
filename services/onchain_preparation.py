"""Aomi-side market preparation; this service never creates or commits an executor."""
import asyncio
from typing import Any, Awaitable, Callable, Dict, TypeVar

from aomi.pipeline.client import PipelineClient
from aomi.pipeline.errors import PipelineError

from models.onchain_preparation import OnchainPreparationRequest


T = TypeVar("T")


async def admitted_read(call: Callable[[], Awaitable[T]]) -> T:
    """Only retry an explicit refusal before dispatch; never replay an unknown outcome."""
    delays = (0.15, 0.3, 0.6, 1.2, 2.4)
    for attempt in range(len(delays) + 1):
        try:
            return await call()
        except PipelineError as exc:
            if exc.status != 409 or exc.code != "operation_in_flight" or attempt == len(delays):
                raise
            await asyncio.sleep(delays[attempt])
    raise AssertionError("unreachable")


async def prepare_onchain(
    request: OnchainPreparationRequest, client: PipelineClient, *, application_id: int | None
) -> Dict[str, Any]:
    if type(application_id) is not int or application_id <= 0:
        raise ValueError("Configure the registered Aomi preparation application ID before preparing markets")
    context = await admitted_read(client.svm_context)
    wallet = context.get("address")
    cluster = context.get("cluster")
    if not isinstance(wallet, str) or not wallet or cluster != "mainnet-beta":
        raise ValueError("Connect a Solana mainnet wallet before preparing these markets")
    arguments = dict(request.arguments)
    if request.operation != "venues":
        if arguments.get("wallet", wallet) != wallet:
            raise ValueError("Requested wallet does not match the Aomi signing wallet")
        arguments["wallet"] = wallet
    result = await admitted_read(lambda: client.invoke(
        "solana-defi", f"solana_defi_{request.operation}", arguments, application_id=application_id
    ))
    if result.build is not None:
        raise ValueError("Preparation unexpectedly staged a transaction")
    payload = result.result
    if not isinstance(payload, dict):
        raise ValueError("Invalid Aomi preparation response")
    if request.operation != "venues" and (payload.get("wallet") != wallet or payload.get("cluster") != cluster):
        raise ValueError("Preparation wallet or network does not match the connected account")
    return {"wallet": wallet, "cluster": cluster, "result": payload}
