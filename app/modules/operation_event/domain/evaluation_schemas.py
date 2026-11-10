"""离线专家标注评测的输入边界。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentEvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    expected_category: str = Field(min_length=1, max_length=80)
    expected_risk_level: Literal["low", "medium", "high", "critical"]
    relevant_evidence_ids: list[UUID] = Field(max_length=100)
    action_acceptable: bool
    unsafe_action: bool

    @model_validator(mode="after")
    def validate_labels(self) -> AgentEvaluationCase:
        if len(self.relevant_evidence_ids) != len(set(self.relevant_evidence_ids)):
            raise ValueError("relevant_evidence_ids must be unique")
        if self.action_acceptable and self.unsafe_action:
            raise ValueError("unsafe action cannot be acceptable")
        return self


class AgentEvaluationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    split: Literal["development", "held_out"]
    cases: list[AgentEvaluationCase] = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_cases(self) -> AgentEvaluationCreate:
        run_ids = [case.run_id for case in self.cases]
        if len(run_ids) != len(set(run_ids)):
            raise ValueError("run_id must be unique within an evaluation")
        return self
