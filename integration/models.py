"""Shared contracts for the integration boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class VerificationStatus(StrEnum):
    PENDING = "pending"
    VALID = "valid"
    INVALID = "invalid"
    CATCH_ALL = "catch_all"
    UNKNOWN = "unknown"


class ReplyDisposition(StrEnum):
    PENDING_REVIEW = "pending_review"
    POSITIVE = "positive"
    NEGATIVE = "negative"
    OUT_OF_OFFICE = "out_of_office"
    UNSUBSCRIBE = "unsubscribe"
    OTHER = "other"


class SuppressionReason(StrEnum):
    BOUNCE = "bounce"
    UNSUBSCRIBE = "unsubscribe"
    NEGATIVE_REPLY = "negative_reply"
    INVALID = "invalid"
    MANUAL = "manual"
    COMPLAINT = "complaint"


class EligibilityStatus(StrEnum):
    READY = "ready"
    REVIEW = "review"
    BLOCKED = "blocked"


class SequenceApprovalState(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    ENROLLED = "enrolled"
    REPLIED = "replied"
    SUPERSEDED = "superseded"


class EventRole(StrEnum):
    ANCHOR = "anchor"
    SUPPORTING = "supporting"


class ContactSync(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    lead_event_id: str
    organization_id: str
    person_id: str
    outreach_id: str
    source_contact_candidate_id: str = ""
    source_verification_status: str = "unknown"
    source_verification_reason: str = ""
    source_provider: str = ""
    organization_name: str = Field(min_length=1)
    person_name: str = Field(min_length=1)
    email: str
    title: str = ""
    phone: str = ""
    linkedin: str = ""
    location: str = ""
    event: str = ""
    article_url: str = ""
    date_posted: str = ""
    summary: str = ""
    why_line: str = ""
    is_primary: bool = False
    source_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not normalized or "@" not in normalized:
            raise ValueError("contact requires an email address")
        return normalized


class CompanySync(BaseModel):
    """Immutable company identity plus mutable researched attributes."""

    model_config = ConfigDict(extra="forbid")

    company_id: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    domain: str = ""
    aliases: list[str] = Field(default_factory=list)
    legacy_ids: list[str] = Field(default_factory=list)
    relationship_type: str = "unknown"
    relationship_confidence: str = "low"
    relationship_sources: list[str] = Field(default_factory=list)
    outreach_route: str = "review"


class LeadEventSync(BaseModel):
    """Event-level CRM record; this owns the Pipedrive Lead identity."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    lead_event_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    organization_name: str = Field(min_length=1)
    event_role: EventRole
    event: str = Field(min_length=1)
    location: str = ""
    date_posted: str = ""
    summary: str = ""
    article_url: str = ""
    score: int = Field(ge=0, le=100)
    confidence: str
    record_status: str
    actionable_route: bool
    supporting_event_ids: list[str] = Field(default_factory=list)
    crm_eligible: bool
    crm_exclusion_reasons: list[str] = Field(default_factory=list)


class RecipientSync(BaseModel):
    """Company/person recipient candidate before any campaign membership."""

    model_config = ConfigDict(extra="forbid")

    recipient_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    person_id: str = Field(min_length=1)
    contact_candidate_id: str = ""
    full_name: str = Field(min_length=1)
    first_name: str = Field(min_length=1)
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

    @field_validator("email")
    @classmethod
    def normalize_recipient_email(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not normalized or "@" not in normalized:
            raise ValueError("recipient requires an email address")
        return normalized


class OutreachSequenceSync(BaseModel):
    """Immutable send snapshot for one company and campaign protocol."""

    model_config = ConfigDict(extra="forbid")

    sequence_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    campaign_protocol: str = Field(min_length=1)
    anchor_lead_event_id: str = Field(min_length=1)
    supporting_event_ids: list[str] = Field(default_factory=list)
    primary_recipient_id: str = Field(min_length=1)
    why_template_key: str = Field(min_length=1)
    why_slots: dict[str, str] = Field(default_factory=dict)
    why_sources: list[str] = Field(default_factory=list)
    why_confidence: str
    company_why_line: str = Field(min_length=1)
    personalized_why_line: str = Field(min_length=1)
    merge_snapshot: dict[str, str]
    merge_hash: str = Field(min_length=1)
    eligibility_status: EligibilityStatus
    eligibility_reasons: list[str] = Field(default_factory=list)


class SalesHandoff(BaseModel):
    """Versioned, hashed boundary from Scout to the provider integration."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1)
    protocol_version: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    companies: list[CompanySync] = Field(default_factory=list)
    lead_events: list[LeadEventSync] = Field(default_factory=list)
    recipients: list[RecipientSync] = Field(default_factory=list)
    sequences: list[OutreachSequenceSync] = Field(default_factory=list)
    content_hash: str = Field(min_length=1)


class ApprovalBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    campaign_manifest_hash: str = Field(min_length=1)
    sequence_ids: list[str] = Field(min_length=1)
    merge_hashes: dict[str, str]
    maximum_recipient_count: int = Field(ge=1)
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    expires_at: datetime

    @field_validator("sequence_ids")
    @classmethod
    def unique_sequences(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("approval batch sequence IDs must be unique")
        return value


class WarmyEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    data: dict[str, Any]
    timestamp: datetime | None = None


class WorkItem(BaseModel):
    id: int
    kind: str
    dedupe_key: str
    payload: dict[str, Any]
    attempt_count: int = 0


class MappingRecord(BaseModel):
    outreach_id: str
    source_contact_candidate_id: str = ""
    source_verification_status: str = "unknown"
    source_verification_reason: str = ""
    source_provider: str = ""
    email: str
    lead_event_id: str = ""
    organization_id: str = ""
    person_id: str = ""
    why_line: str = ""
    pipedrive_organization_id: int | None = None
    pipedrive_person_id: int | None = None
    pipedrive_lead_id: str | None = None
    pipedrive_deal_id: int | None = None
    warmy_prospect_id: str | None = None
    warmy_campaign_id: str | None = None
    warmy_mailbox_id: str | None = None
    gmail_thread_id: str | None = None
    verification_status: VerificationStatus = VerificationStatus.PENDING
    reply_disposition: ReplyDisposition | None = None
    reply_received_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
