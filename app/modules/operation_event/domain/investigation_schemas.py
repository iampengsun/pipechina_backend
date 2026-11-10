"""Shared contracts for investigation execution, human input and bounded planning."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SubInvestigation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "knowledge",
        "history",
        "verification",
        "recurrence",
        "impact",
        "weather",
        "geography",
        "enterprise_gis",
    ]
    query: str = Field(max_length=100)
    completion_criteria: str = Field(
        default="核查本问题的支持证据与相反证据，说明无法确定的事实。", min_length=5, max_length=500
    )
    required: bool = False


class InvestigationOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skills: list[str] | None = Field(default=None, max_length=3)
    max_parallel: int = Field(default=3, ge=1, le=3)
    max_branch_steps: int = Field(default=4, ge=1, le=8)
    branch_timeout_seconds: int = Field(default=90, ge=1, le=300)
    max_model_calls: int = Field(default=40, ge=8, le=80)
    max_tool_calls: int = Field(default=20, ge=1, le=40)
    max_tokens: int = Field(default=60000, ge=1000, le=200000)
    max_cost_microusd: int | None = Field(default=None, ge=1, le=100000000)
    context_chars: int = Field(default=16000, ge=2000, le=24000)
    prompt_candidate_id: UUID | None = None


class ResumeInvestigation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_version: int = Field(ge=1)
    confirmed_observation_ids: list[UUID] = Field(min_length=1, max_length=30)
    reason: str = Field(min_length=2, max_length=2000)
