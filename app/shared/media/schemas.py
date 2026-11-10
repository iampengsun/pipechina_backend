"""Shared contracts for private business attachments."""

from pydantic import BaseModel, Field


class AttachmentCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=3, max_length=120)
    size_bytes: int = Field(gt=0, le=20 * 1024 * 1024)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


class AttachmentUploadComplete(BaseModel):
    server_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
