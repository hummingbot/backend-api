"""
Shared helpers for the gateway trading routers: extra_params validation and
Gateway write-response status mapping.

Gateway destructures a fixed set of connector-specific keys from each request
body and silently ignores everything else, so hapi rejects loudly instead of
letting a typo'd key, a key sent to a connector that ignores it, or a value of
the wrong type get silently dropped or misparsed downstream.
"""
import re
from typing import Any, Dict, Optional, Set, Tuple

from fastapi import HTTPException


def get_transaction_status_from_response(gateway_response: dict) -> str:
    """Map a Gateway write response's status to hapi's transaction vocabulary:
    status 1 -> CONFIRMED, negative (-1 failed, -2 dropped) -> FAILED,
    0 or missing -> SUBMITTED.

    Single source: both the swap and CLMM routers import this — two drifting
    copies of load-bearing status mapping is how a failed tx gets recorded as
    submitted on one surface but not the other.
    """
    status = gateway_response.get("status")

    if status == 1:
        return "CONFIRMED"
    # Gateway's TransactionStatus uses negative values for terminal failures
    # (e.g. a failed EVM swap returns status -1 with zeroed amounts).
    if isinstance(status, (int, float)) and status < 0:
        return "FAILED"
    return "SUBMITTED"


def get_transaction_hash_from_response(gateway_response: dict) -> Optional[str]:
    """The transaction id a Gateway write response reports, whatever chain it came from.

    Gateway names the same field three ways — Solana routes answer `signature`, EVM
    routes `txHash`, and a few older shapes `hash` — so reading only one of them is
    reading only one family of chains. The CLMM open handler did exactly that
    (`signature` alone) and answered every uniswap/pancakeswap open with a 500 saying
    no signature was returned, for a position that had in fact just been opened.

    Returns None when the response carries no id at all; callers decide whether that is
    a 500 (the routes that must answer with a hash) or an empty column (the AMM event
    recorder, which records what it has and never fails the write over it).
    """
    return (
        gateway_response.get("signature")
        or gateway_response.get("txHash")
        or gateway_response.get("hash")
    )


# A Solana signature or an EVM transaction hash, as they appear inside Gateway's
# landed-but-failed message: "Transaction <id> landed on-chain but failed: <reason>".
_TRANSACTION_ID = re.compile(r"[Tt]ransaction ([1-9A-HJ-NP-Za-km-z]{43,88}|0x[0-9a-fA-F]{64})")


def transaction_id_from_error(error: Exception) -> Optional[str]:
    """The transaction a failed Gateway call actually sent, if it sent one.

    This is the distinction worth keeping: a pre-flight simulation failure never got a
    signature and cost nothing, while a transaction that landed and reverted has one and
    paid gas for the privilege. Gateway names it in the message either way it can, and it
    was reaching a log line and nowhere else.
    """
    match = _TRANSACTION_ID.search(str(error))
    return match.group(1) if match else None


# Spec entry: key -> (allowed value types, connectors that honor the key).
# Deliberate strictness (accepted residual): numeric keys are typed int even though
# Gateway's TypeBox says Number (JS has one number type) — the domains are integral
# (enum values, bin steps, config indexes), so a float like 1.0 gets a loud 400
# here rather than reaching Gateway; no semantically distinct value is blocked.
ExtraParamsSpec = Dict[str, Tuple[Tuple[type, ...], Set[str]]]


def validate_extra_params(
    extra_params: Optional[Dict[str, Any]],
    spec: ExtraParamsSpec,
    connector: str,
    route: str,
) -> None:
    """Reject extra_params Gateway's `route` would silently drop or misparse.

    Typed swap providers like 'jupiter/router' validate on their base name.
    """
    for key, value in (extra_params or {}).items():
        if key not in spec:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported extra_params key '{key}': Gateway's {route} honors "
                    f"only {sorted(spec)} and silently ignores everything else."
                ),
            )
        allowed_types, connectors = spec[key]
        if connector.split("/")[0] not in connectors:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"extra_params.{key} only applies to {sorted(connectors)}; "
                    f"'{connector}' would silently ignore it."
                ),
            )
        # bool subclasses int: check it explicitly so True never passes as an int.
        type_ok = (bool in allowed_types) if isinstance(value, bool) else isinstance(value, allowed_types)
        if not type_ok:
            expected = "/".join(t.__name__ for t in allowed_types)
            raise HTTPException(
                status_code=400,
                detail=f"extra_params.{key} must be {expected}, got {type(value).__name__} ({value!r}).",
            )
