from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: str


class ReportRequest(BaseModel):
    description: str = Field(default="", max_length=2000)
    device_id: str | None = Field(default=None, max_length=64)


class DetailRequest(BaseModel):
    description: str | None = Field(default=None, max_length=2000)
    device_id: str | None = Field(default=None, max_length=64)


class ApprovalRequest(BaseModel):
    decision: Literal["approve", "reject"]
    version: int = Field(ge=1)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(default="", max_length=500)
