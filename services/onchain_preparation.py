"""Aomi-side market preparation; this service never creates or commits an executor."""
from typing import Any, Dict

from aomi.pipeline.client import PipelineClient

from models.onchain_preparation import OnchainPreparationRequest


async def prepare_onchain(
    request: OnchainPreparationRequest, client: PipelineClient, *, application_id: int | None
) -> Dict[str, Any]:
    if type(application_id) is not int or application_id <= 0:
        raise ValueError("Configure the registered Aomi preparation application ID before preparing markets")
    context = await client.svm_context()
    wallet = context.get("address")
    cluster = context.get("cluster")
    if not isinstance(wallet, str) or not wallet or cluster != "mainnet-beta":
        raise ValueError("Connect a Solana mainnet wallet before preparing these markets")
    arguments = dict(request.arguments)
    if request.operation != "venues":
        if arguments.get("wallet", wallet) != wallet:
            raise ValueError("Requested wallet does not match the Aomi signing wallet")
        arguments["wallet"] = wallet
    result = await client.invoke(
        "solana-defi", f"solana_defi_{request.operation}", arguments, application_id=application_id
    )
    if result.build is not None:
        raise ValueError("Preparation unexpectedly staged a transaction")
    payload = result.result
    if not isinstance(payload, dict):
        raise ValueError("Invalid Aomi preparation response")
    if request.operation != "venues" and (payload.get("wallet") != wallet or payload.get("cluster") != cluster):
        raise ValueError("Preparation wallet or network does not match the connected account")
    return {"wallet": wallet, "cluster": cluster, "result": payload}
