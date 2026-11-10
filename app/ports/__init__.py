"""应用端口：由可替换基础设施适配器实现的稳定接口。"""

from app.ports.models import (
    ASRResult,
    ASRSegment,
    AssessmentCandidate,
    EventCandidate,
    EventCandidateList,
    FindingCandidate,
    FindingCandidateList,
    HandoverSummary,
    MediaRef,
    ReportDraft,
)
from app.ports.speech import SpeechToTextProvider
from app.ports.storage import ObjectMetadata, StorageProvider, UploadGrant
from app.ports.text import TextLLMProvider
from app.ports.vision import VisionProvider

__all__ = [
    "ASRResult",
    "ASRSegment",
    "AssessmentCandidate",
    "EventCandidate",
    "EventCandidateList",
    "FindingCandidate",
    "FindingCandidateList",
    "HandoverSummary",
    "MediaRef",
    "ObjectMetadata",
    "ReportDraft",
    "SpeechToTextProvider",
    "StorageProvider",
    "TextLLMProvider",
    "UploadGrant",
    "VisionProvider",
]
