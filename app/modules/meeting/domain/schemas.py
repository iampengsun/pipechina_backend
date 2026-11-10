from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ParticipantInput(Input):
    participant_key: str = Field(min_length=1, max_length=120)
    display_name: str = Field(min_length=1, max_length=200)
    user_id: UUID | None = None
    role: str | None = Field(default=None, max_length=100)


class MeetingCreate(Input):
    title: str = Field(min_length=1, max_length=300)
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    location: str | None = Field(default=None, max_length=300)
    organization_unit_id: UUID | None = None
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    mime_type: str = Field(min_length=3, max_length=120)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    participants: list[ParticipantInput] = Field(default_factory=list, max_length=500)
    reader_ids: list[UUID] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def validate_range(self):
        if self.ended_at and self.ended_at <= self.started_at:
            raise ValueError("ended_at must be after started_at")
        keys = [p.participant_key for p in self.participants]
        if len(keys) != len(set(keys)):
            raise ValueError("participant_key must be unique")
        return self


class UploadComplete(Input):
    server_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


class RecordingDelete(Input):
    reason: str = Field(min_length=2, max_length=1000)


class MinutesAction(Input):
    task: str = Field(min_length=1, max_length=1000)
    owner: str | None = Field(default=None, max_length=200)
    due_at: str | None = Field(default=None, max_length=100)


class MeetingMinutes(Input):
    summary: str = Field(min_length=1, max_length=10000)
    decisions: list[str] = Field(default_factory=list, max_length=100)
    action_items: list[MinutesAction] = Field(default_factory=list, max_length=100)
    unknowns: list[str] = Field(default_factory=list, max_length=100)


class MinutesUpdate(Input):
    content: MeetingMinutes


class SegmentInput(Input):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    text: str = Field(min_length=1)
    speaker_label: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_range(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class TranscriptUpdate(Input):
    full_text: str = Field(min_length=1)
    segments: list[SegmentInput] = Field(default_factory=list, max_length=100000)

    @model_validator(mode="after")
    def validate_segments(self):
        for previous, current in zip(self.segments, self.segments[1:], strict=False):
            if current.start_ms < previous.end_ms:
                raise ValueError("segments must be ordered and non-overlapping")
        return self


class SpeakerBinding(Input):
    speaker_label: str = Field(min_length=1, max_length=120)
    participant_key: str = Field(min_length=1, max_length=120)


class KnowledgePublicationCreate(Input):
    knowledge_base_id: UUID
    effective_from: AwareDatetime | None = None
    authority: str = Field(default="meeting-confirmed", max_length=120)


class MeetingView(BaseModel):
    id: UUID
    organization_unit_id: UUID
    title: str
    started_at: datetime
    ended_at: datetime | None
    location: str | None
    upload_status: str
    recording_deleted_at: datetime | None
    status: str
    current_transcript_version_id: UUID | None
    acl_version: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class MeetingACLUpdate(Input):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=2, max_length=1000)
    reader_ids: list[UUID] = Field(max_length=1000)
