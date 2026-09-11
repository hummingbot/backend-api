"""Read-only Aomi preparation requests shared by all supported Solana venues."""
from typing import Any, Dict, Literal

from pydantic import BaseModel, ConfigDict, Field


class OnchainPreparationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["venues", "market", "prepare", "position"]
    arguments: Dict[str, Any] = Field(default_factory=dict)
