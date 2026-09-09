"""Pydantic contracts for every persisted V2 handoff."""
from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .ids import canonicalize_url


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RecordStatus(StrEnum):
    VALID = "valid"
    REVIEW = "review"
    REJECTED = "rejected"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    REVIEW = "review"


class FeedStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DEGRADED = "degraded"
    DISABLED = "disabled"
    QUARANTINED = "quarantined"


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    UNKNOWN = "unknown"
    REJECTED = "rejected"


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    supports: str = Field(min_length=1)
    provider: str = "web"
    retrieved_at: datetime = Field(default_factory=utc_now)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return canonicalize_url(value)


class DiscoveryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    run_id: str
    provider: str
    provider_id: str = ""
    discovered_url: str
    resolved_url: str
    canonical_url: str
    title: str = ""
    source_id: str
    source_name: str
    source_domain: str
    published_at: datetime | None = None
    discovered_at: datetime = Field(default_factory=utc_now)
    raw_artifact_path: str = ""
    raw_artifact_hash: str = ""
    record_status: RecordStatus = RecordStatus.VALID
    validation_errors: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("discovered_url", "resolved_url", "canonical_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return canonicalize_url(value)

    @model_validator(mode="after")
    def review_has_reason(self) -> "DiscoveryCandidate":
        if self.record_status == RecordStatus.REVIEW and not self.validation_errors:
            raise ValueError("review candidates require validation_errors")
        return self


class Organization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: str
    canonical_name: str = Field(min_length=1)
    domain: str = ""
    location: str = ""
    aliases: list[str] = Field(default_factory=list)
    employee_count: dict[str, Any] | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    inferred_identity: bool = False


class Person(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person_id: str
    organization_id: str
    name: str = Field(min_length=1)
    title: str = ""
    scope: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    inferred_identity: bool = False


class LeadEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lead_event_id: str
    run_id: str
    organization_id: str
    primary_candidate_id: str
    supporting_candidate_ids: list[str] = Field(min_length=1)
    event: str = Field(min_length=1)
    location: str
    state: str = "Arizona"
    date_posted: date | None = None
    summary: str = ""
    priority: str
    property_type: str = "other"
    service_angle: str = ""
    why_line: str = ""
    why_template_key: str = ""
    why_slots: dict[str, str] = Field(default_factory=dict)
    why_confidence: str = "low"
    why_line_status: str = "missing"
    why_sources: list[str] = Field(default_factory=list)
    filter_reason: str = ""
    confidence: str = "high"
    record_status: RecordStatus = RecordStatus.VALID
    validation_errors: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)

    @field_validator("priority")
    @classmethod
    def priority_value(cls, value: str) -> str:
        normalized = value.lower().strip()
        if normalized not in {"high", "medium", "low"}:
            raise ValueError("priority must be high, medium, or low")
        return normalized

    @model_validator(mode="after")
    def valid_arizona_event(self) -> "LeadEvent":
        if self.state != "Arizona":
            raise ValueError("qualified V2 lead events must be in Arizona")
        if self.primary_candidate_id not in self.supporting_candidate_ids:
            raise ValueError("primary_candidate_id must be a supporting candidate")
        if self.record_status == RecordStatus.REVIEW and not self.validation_errors:
            raise ValueError("review lead events require validation_errors")
        return self


class CompanyProfile(BaseModel):
    """Canonical company identity and one validated company-level why line."""

    model_config = ConfigDict(extra="forbid")

    company_id: str
    run_id: str
    canonical_name: str
    domain: str = ""
    aliases: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    organization_ids: list[str] = Field(default_factory=list)
    lead_event_ids: list[str] = Field(default_factory=list)
    anchor_lead_event_id: str
    relationship_type: str = "unknown"
    relationship_confidence: str = "low"
    relationship_sources: list[str] = Field(default_factory=list)
    outreach_route: str = "review"
    why_line: str = ""
    why_template_key: str = ""
    why_slots: dict[str, str] = Field(default_factory=dict)
    why_confidence: str = "low"
    why_sources: list[str] = Field(default_factory=list)
    why_line_status: str = "review"
    record_status: RecordStatus = RecordStatus.REVIEW
    validation_errors: list[str] = Field(default_factory=list)


class OutreachRecipient(BaseModel):
    """Deterministically ranked person/contact for one canonical company."""

    model_config = ConfigDict(extra="forbid")

    recipient_id: str
    run_id: str
    company_id: str
    person_id: str
    contact_candidate_id: str
    full_name: str
    first_name: str
    title: str = ""
    scope: str = ""
    email: str
    source_provider: str = ""
    source_verification_status: str = "unknown"
    source_verification_reason: str = ""
    role_score: int = Field(ge=0)
    rank: int = Field(ge=1)
    primary: bool = False
    selection_rationale: list[str] = Field(default_factory=list)
    eligibility_status: str = "review"
    eligibility_reasons: list[str] = Field(default_factory=list)


class ContactCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_candidate_id: str
    run_id: str
    lead_event_id: str
    organization_id: str
    person_id: str
    person_name: str
    title: str = ""
    email: str = ""
    phone: str = ""
    linkedin: str = ""
    provider: str
    verification_status: VerificationStatus = VerificationStatus.UNKNOWN
    verification_reason: str = ""
    selected: bool = False
    evidence: list[Evidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def sourced_and_reachable(self) -> "ContactCandidate":
        if not any((self.email, self.phone, self.linkedin)):
            raise ValueError("contact candidate must have at least one contact method")
        if not self.evidence:
            raise ValueError("contact candidate requires evidence")
        return self


class LeadScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    lead_event_id: str
    score: int = Field(ge=0, le=100)
    model: str
    attempt_id: str
    rationale: str = ""


class ReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_id: str
    run_id: str
    stage: str
    record_type: str
    record_id: str
    reason_code: str
    validation_errors: list[str] = Field(min_length=1)
    raw_artifact_path: str = ""
    retry_count: int = Field(default=0, ge=0)
    state: str = "open"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: str
    stamp: date
    since: date
    status: StageStatus = StageStatus.PENDING
    configuration: dict[str, Any] = Field(default_factory=dict)
    stages: dict[str, dict[str, Any]] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    usage: dict[str, int | float] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
