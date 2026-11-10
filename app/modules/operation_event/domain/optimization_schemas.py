"""Contracts for bounded reflective prompt search and human publication."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.ports.models import StrictProviderOutput


class OptimizationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evaluation_id: UUID
    generations: int = Field(default=2, ge=1, le=4)
    max_cases: int = Field(default=4, ge=1, le=8)
    repetitions: int = Field(default=2, ge=1, le=3)
    max_model_calls: int = Field(default=128, ge=4, le=512)
    max_tokens: int = Field(default=500000, ge=10000, le=1000000)
    max_cost_microusd: int | None = Field(default=None, ge=1)


class Reflection(StrictProviderOutput):
    lessons: list[str] = Field(min_length=1, max_length=10)
    instructions: str = Field(min_length=10, max_length=6000)


class PublishOptimization(BaseModel):
    model_config = ConfigDict(extra="forbid")
    improvement_id: UUID
    reason: str = Field(min_length=5, max_length=2000)
