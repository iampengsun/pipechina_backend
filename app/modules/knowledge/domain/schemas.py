from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BaseCreate(Input):
    name: str = Field(min_length=1, max_length=200)
    organization_unit_id: UUID | None = None
    reader_ids: list[UUID] = Field(default_factory=list, max_length=1000)


class DocumentCreate(Input):
    base_id: UUID
    code: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=300)
    kind: Literal["standard", "manual", "procedure", "case", "handover", "notice"]


class VersionCreate(Input):
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=150)
    size_bytes: int = Field(gt=0, le=200 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    effective_from: AwareDatetime
    effective_to: AwareDatetime | None = None
    equipment_models: list[str] = Field(default_factory=list, max_length=100)
    authority: str = Field(default="unclassified", max_length=120)

    @model_validator(mode="after")
    def validate_dates(self):
        if self.effective_to and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        return self


class ACLUpdate(Input):
    reader_ids: list[UUID] = Field(max_length=1000)
    expected_version: int = Field(ge=1)


class Review(Input):
    approved: bool
    reason: str = Field(min_length=2, max_length=2000)


class Reason(Input):
    reason: str = Field(min_length=2, max_length=2000)


class SearchInput(Input):
    question: str = Field(min_length=1, max_length=4000)
    base_ids: list[UUID] = Field(default_factory=list, max_length=30)
    equipment_model: str | None = Field(default=None, max_length=120)
    as_of: AwareDatetime | None = None
    version_ids: list[UUID] = Field(default_factory=list, max_length=20)
    mode: Literal["hybrid", "fulltext", "vector", "graph", "global"] = "hybrid"
    limit: int = Field(default=8, ge=1, le=20)
    conversation_id: UUID | None = None
    expand_queries: bool = False


class Citation(Input):
    evidence_id: str
    quote: str = Field(min_length=1)


class AnswerClaim(Input):
    text: str
    citations: list[Citation]


class GroundedAnswer(Input):
    claims: list[AnswerClaim]
    conflicts: list[str]
    unknowns: list[str]
    refused: bool


class QueryPlan(Input):
    queries: list[str] = Field(max_length=3)
    entities: list[str] = Field(max_length=5)


class BoundaryPlan(Input):
    starts: list[str]


class ExtractedFact(Input):
    source: str = Field(min_length=1, max_length=300)
    relation: str = Field(min_length=1, max_length=100)
    target: str = Field(min_length=1, max_length=300)
    evidence_id: str
    quote: str = Field(min_length=1)


class FactExtraction(Input):
    facts: list[ExtractedFact] = Field(max_length=40)


class AnswerVerification(Input):
    supported: bool
    issues: list[str]


class ReferenceEvidence(Input):
    document_code: str = Field(min_length=1, max_length=120)
    quote: str = Field(min_length=2, max_length=8000)


class EvaluationCase(Input):
    case_id: str | None = Field(default=None, min_length=1, max_length=120)
    reference_answer: str | None = Field(default=None, max_length=8000)
    reference_evidence: list[ReferenceEvidence] = Field(default_factory=list, max_length=100)
    question: str = Field(min_length=1, max_length=4000)
    relevant_evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)
    should_refuse: bool = False


class EvaluationCreate(Input):
    name: str = Field(min_length=1, max_length=120)
    split: Literal["development", "held_out"]
    cases: list[EvaluationCase] = Field(min_length=1, max_length=300)
    modes: list[Literal["hybrid", "fulltext", "vector", "graph"]] = Field(
        default=["hybrid"], min_length=1, max_length=4
    )
    base_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_cases_and_modes(self):
        ids = [c.case_id for c in self.cases if c.case_id is not None]
        if len(ids) != len(set(ids)) or len(self.modes) != len(set(self.modes)):
            raise ValueError("case IDs and modes must be unique")
        return self


class FeedbackInput(Input):
    category: Literal["helpful", "wrong_citation", "incomplete", "outdated", "incorrect"]
    comment: str = Field(default="", max_length=2000)


class ConversationCreate(Input):
    title: str = Field(min_length=1, max_length=300)


class BlockCorrection(Input):
    block_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=100000)


class ArtifactCorrection(Input):
    corrections: list[BlockCorrection] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=2, max_length=2000)


class AnswerCaseReview(Input):
    case_id: str
    mode: Literal["hybrid", "fulltext", "vector", "graph"]
    answer_correct: bool
    unsafe_answer: bool
    reason: str = Field(min_length=2, max_length=2000)

    @model_validator(mode="after")
    def consistent(self):
        if self.answer_correct and self.unsafe_answer:
            raise ValueError("unsafe answer cannot be correct")
        return self


class EvaluationExpertReview(Input):
    expected_revision: int = Field(ge=0)
    cases: list[AnswerCaseReview] = Field(min_length=1, max_length=1200)
