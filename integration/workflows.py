"""Idempotent business workflows connecting Scout, Warmy, Gmail, and Pipedrive."""

from __future__ import annotations

import logging
import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Any

from .config import ActivationBlocked, Settings
from .campaign import campaign_manifest_hash
from .models import (
    ContactSync,
    EligibilityStatus,
    LeadEventSync,
    MappingRecord,
    OutreachSequenceSync,
    RecipientSync,
    ReplyDisposition,
    SequenceApprovalState,
    SuppressionReason,
    VerificationStatus,
    WorkItem,
)
from .providers import (
    GmailClient,
    GmailHistoryExpired,
    PipedriveClient,
    ProviderError,
    WarmyClient,
)
from .security import issue_unsubscribe_token
from .recipient_guard import inspect_recipient_guard
from recipient_policy import SENDABLE_ADDRESS_STATUSES, assess_recipient

LOG = logging.getLogger(__name__)

FALLBACK_SENDABLE_VERIFICATION_STATUSES = frozenset(
    {
        VerificationStatus.VALID,
        VerificationStatus.CATCH_ALL,
        VerificationStatus.UNKNOWN,
    }
)
FALLBACK_SENDABLE_SOURCE_STATUSES = SENDABLE_ADDRESS_STATUSES


def verification_allows_fallback_send(status: VerificationStatus) -> bool:
    """Allow uncertain-but-not-invalid addresses as the best available fallback."""
    return status in FALLBACK_SENDABLE_VERIFICATION_STATUSES


class WorkflowRetry(RuntimeError):
    """A normal incomplete provider operation that should be retried."""


class SalesWorkflows:
    def __init__(
        self,
        settings: Settings,
        db,
        *,
        warmy: WarmyClient | None = None,
        pipedrive: PipedriveClient | None = None,
        gmail: GmailClient | None = None,
    ):
        self.settings = settings
        self.db = db
        self._warmy = warmy
        self._pipedrive = pipedrive
        self._gmail = gmail
        self._operation_owner = uuid.uuid4().hex

    @property
    def warmy(self) -> WarmyClient:
        if self._warmy is None:
            self._warmy = WarmyClient(self.settings)
        return self._warmy

    @property
    def pipedrive(self) -> PipedriveClient:
        if self._pipedrive is None:
            self._pipedrive = PipedriveClient(self.settings)
        return self._pipedrive

    @property
    def gmail(self) -> GmailClient:
        if self._gmail is None:
            self._gmail = GmailClient(self.settings)
        return self._gmail

    def close(self) -> None:
        if self._warmy:
            self._warmy.close()
        if self._pipedrive:
            self._pipedrive.close()

    def handle(self, item: WorkItem) -> None:
        handlers: dict[str, Callable[[dict[str, Any]], None]] = {
            "scout.lead.sync": self.sync_lead_event,
            "scout.sequence.sync": self.sync_sequence,
            "warmy.sequence.enroll": self.enroll_sequence,
            "scout.contact.sync": self.sync_contact,
            "warmy.verify": self.verify_contact,
            "warmy.enroll": self.enroll_contact,
            "warmy.event": self.handle_warmy_event,
            "pipedrive.event": self.handle_pipedrive_event,
            "pipedrive.convert": self.convert_lead,
            "pipedrive.sequence.convert": self.convert_sequence,
            "suppression.sync": self.sync_suppression,
            "gmail.sync": self.sync_jordan_inbox,
        }
        try:
            handler = handlers[item.kind]
        except KeyError as error:
            raise ValueError(f"unknown work kind: {item.kind}") from error
        try:
            handler(item.payload)
        except ProviderError as error:
            archived = error.provider == "pipedrive" and error.status_code == 403 and error.code.casefold() == "lead is archived"
            if not archived or item.kind not in {"scout.lead.sync", "scout.sequence.sync"}:
                raise
            if item.kind == "scout.sequence.sync":
                sequence = OutreachSequenceSync.model_validate(item.payload["sequence"])
                event_id = sequence.anchor_lead_event_id
                self._block_sequence(sequence, ["anchor_lead_archived"])
            else:
                event_id = item.payload["lead_event"]["lead_event_id"]
            self.db.update_lead_event(event_id, crm_state="archived")
            LOG.info("preserved archived Pipedrive lead", extra={"lead_event_id":event_id})

    def _operation(
        self,
        provider: str,
        key: str,
        payload: dict[str, Any],
        call: Callable[[], dict[str, Any]],
        reconcile: Callable[[], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        existing = self.db.get_operation(provider, key)
        if existing and existing.get("status") == "completed":
            return existing.get("response_payload") or existing.get("response") or {}
        if existing and existing.get("status") == "uncertain":
            if reconcile is None:
                raise WorkflowRetry(
                    f"{provider} operation outcome is uncertain and has no reconciliation: {key}"
                )
            reconciled = reconcile()
            if reconciled:
                self.db.complete_operation(
                    provider,
                    key,
                    reconciled,
                    external_id=_external_id(reconciled),
                )
                return reconciled
        if not self.db.claim_operation(
            provider,
            key,
            payload,
            owner=self._operation_owner,
            lease_seconds=self.settings.worker_lease_seconds,
        ):
            raise WorkflowRetry(f"{provider} operation is already in progress: {key}")
        try:
            response = call()
        except Exception as error:
            if isinstance(error, (TimeoutError, ProviderError)) and (
                not isinstance(error, ProviderError) or error.retryable
            ):
                self.db.mark_operation_uncertain(provider, key, error)
            else:
                self.db.fail_operation(provider, key, error)
            raise
        external_id = _external_id(response)
        self.db.complete_operation(provider, key, response, external_id=external_id)
        return response

    def _find_available_organization(self, company: dict[str, Any]) -> int | None:
        """A name match cannot steal a CRM ID owned by a distinct local entity."""
        found = self.pipedrive.find_organization(company["canonical_name"])
        if found is None:
            return None
        with self.db.connection() as conn:
            owner = conn.execute(
                "SELECT company_id FROM sales_companies WHERE pipedrive_organization_id=?",
                (int(found),),
            ).fetchone()
        if owner and owner["company_id"] != company["company_id"]:
            return None
        return int(found)

    def sync_lead_event(self, payload: dict[str, Any]) -> None:
        event = LeadEventSync.model_validate(payload["lead_event"])
        if not event.crm_eligible:
            return
        company = self.db.get_company(event.company_id)
        if not company:
            raise WorkflowRetry(f"company not found: {event.company_id}")
        self.settings.require_provider_writes()
        organization_id = company.get("pipedrive_organization_id")
        if organization_id is None:
            organization_id = self._find_available_organization(company)
        if organization_id is None:
            response = self._operation(
                "pipedrive",
                f"organization:create:{event.company_id}",
                company["payload"],
                lambda: {
                    "id": self.pipedrive.create_organization(
                        company["canonical_name"],
                        self.settings.pipedrive_jordan_user_id,
                        event.location,
                    )
                },
                reconcile=lambda: (
                    {"id": found}
                    if (
                        found := self._find_available_organization(company)
                    )
                    else None
                ),
            )
            organization_id = int(response["id"])
        self.db.update_company(
            event.company_id, pipedrive_organization_id=int(organization_id)
        )

        row = self.db.get_lead_event(event.lead_event_id)
        lead_id = str(row.get("pipedrive_lead_id") or "") if row else ""
        if not lead_id:
            lead_id = self.pipedrive.find_lead_by_event_id(event.lead_event_id) or ""
        lead_fields = self._deal_fields(
            aether_lead_event_id=event.lead_event_id,
            canonical_company_id=event.company_id,
            event_role=event.event_role.value,
            outreach_state=(
                "anchor_ready"
                if event.event_role.value == "anchor"
                else "research_only"
            ),
            article_url=event.article_url,
            date_posted=event.date_posted,
        )
        if not lead_id:
            response = self._operation(
                "pipedrive",
                f"lead:create:event:{event.lead_event_id}",
                event.model_dump(mode="json"),
                lambda: {
                    "id": self.pipedrive.create_lead(
                        f"{event.organization_name} — {event.event}"[:255],
                        None,
                        int(organization_id),
                        self.settings.pipedrive_jordan_user_id,
                        lead_fields,
                    )
                },
                reconcile=lambda: (
                    {"id": found}
                    if (found := self.pipedrive.find_lead_by_event_id(event.lead_event_id))
                    else None
                ),
            )
            lead_id = str(response["id"])
        self.pipedrive.update_lead(
            lead_id,
            {"organization_id": int(organization_id), **lead_fields},
        )
        self.db.update_lead_event(
            event.lead_event_id,
            pipedrive_lead_id=lead_id,
            crm_state=(
                "anchor_ready"
                if event.event_role.value == "anchor"
                else "research_only"
            ),
        )

    def sync_sequence(self, payload: dict[str, Any]) -> None:
        sequence = OutreachSequenceSync.model_validate(payload["sequence"])
        recipient = RecipientSync.model_validate(payload["recipient"])
        if recipient.recipient_id != sequence.primary_recipient_id:
            raise ValueError("sequence primary recipient payload mismatch")
        guard = inspect_recipient_guard(self.db, recipient.company_id, recipient.email)
        assessment = assess_recipient(
            verification_status=recipient.source_verification_status,
            verification_reason=recipient.source_verification_reason,
            primary=recipient.primary and recipient.rank == 1,
            role_score=recipient.role_score,
            alternative_email_count=self.db.recipient_email_count(recipient.company_id),
            suppressed=self.db.is_suppressed(recipient.email),
            identity_excluded=guard.hard_failure,
        )
        policy_reasons = [
            *assessment.hard_failures,
            *assessment.selection_blocks,
            *(reason for reason in guard.reasons if reason != "company_identity_mismatch"),
        ]
        if policy_reasons:
            self._block_sequence(sequence, list(dict.fromkeys(policy_reasons)))
            return
        company = self.db.get_company(sequence.company_id)
        event = self.db.get_lead_event(sequence.anchor_lead_event_id)
        if not company or not event:
            raise WorkflowRetry("sequence company or anchor event is missing")
        if event.get("crm_state") == "archived":
            self._block_sequence(sequence, ["anchor_lead_archived"])
            return
        if not event.get("pipedrive_lead_id"):
            raise WorkflowRetry("anchor Pipedrive Lead has not been synchronized")

        token, token_id = issue_unsubscribe_token(
            self.settings.unsubscribe_secret, recipient.email
        )
        self.db.store_unsubscribe_token(token_id, recipient.email)
        unsubscribe_url = f"{self.settings.public_base_url}/unsubscribe?t={token}"
        merge_snapshot = dict(sequence.merge_snapshot)
        merge_snapshot["unsubscribeUrl"] = unsubscribe_url
        from .subjects import project_reference
        merge_snapshot["projectPropertyName"] = project_reference(sequence.model_dump(mode="json"))
        merge_hash = hashlib.sha256(
            json.dumps(
                merge_snapshot,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        sequence = sequence.model_copy(
            update={"merge_snapshot": merge_snapshot, "merge_hash": merge_hash}
        )
        self.db.save_sequence(sequence)

        self.settings.require_provider_writes()
        organization_id = int(company["pipedrive_organization_id"])
        person_row = self.db.get_recipient(recipient_id=recipient.recipient_id)
        person_id = person_row.get("pipedrive_person_id") if person_row else None
        person_fields = self._person_fields(
            aether_person_id=recipient.person_id,
            verification_status=VerificationStatus.PENDING.value,
            suppressed="no",
            suppression_reason="",
            unsubscribe_url=unsubscribe_url,
        )
        if person_id is None:
            person_id = self.pipedrive.find_person(recipient.email)
        if person_id is None:
            response = self._operation(
                "pipedrive",
                f"person:create:{recipient.person_id}:{recipient.email}",
                recipient.model_dump(mode="json"),
                lambda: {
                    "id": self.pipedrive.create_person(
                        recipient.full_name,
                        recipient.email,
                        organization_id,
                        self.settings.pipedrive_jordan_user_id,
                        person_fields,
                    )
                },
                reconcile=lambda: (
                    {"id": found}
                    if (found := self.pipedrive.find_person(recipient.email))
                    else None
                ),
            )
            person_id = int(response["id"])
        self.db.update_recipient(
            recipient.recipient_id, pipedrive_person_id=int(person_id)
        )
        if person_fields:
            self.pipedrive.update_person(int(person_id), person_fields)
        self.pipedrive.update_lead(
            str(event["pipedrive_lead_id"]),
            {
                "person_id": int(person_id),
                **self._deal_fields(
                    outreach_sequence_id=sequence.sequence_id,
                    outreach_state="verification_pending",
                    unsubscribe_url=unsubscribe_url,
                ),
            },
        )

        policy = self.settings.warmy_verification_policy_version
        verification = self.db.get_email_verification(recipient.email, policy)
        if verification is None:
            response = self._operation(
                "warmy",
                f"verify:{policy}:{recipient.email}",
                {"email": recipient.email, "policy": policy},
                lambda: self.warmy.verify_email(
                    recipient.email,
                    f"aether-verify-{policy}-{recipient.email}",
                ),
                reconcile=lambda: self.db.get_email_verification(
                    recipient.email, policy
                ),
            )
            data = response.get("data") or response
            status = _verification_status(data)
            reason = str(data.get("reason") or data.get("message") or "")
            self.db.cache_email_verification(
                recipient.email,
                policy,
                status.value,
                reason,
                data,
            )
            verification = {
                "status": status.value,
                "reason": reason,
                "provider_payload": data,
            }
        status = VerificationStatus(str(verification["status"]))
        self.db.update_recipient(
            recipient.recipient_id,
            verification_status=status.value,
            verification_policy_version=policy,
            verification_reason=str(verification.get("reason") or ""),
        )
        if not verification_allows_fallback_send(status):
            self._block_sequence(sequence, [f"warmy_verification_{status.value}"])
            if status == VerificationStatus.INVALID:
                self.db.suppress(
                    recipient.email,
                    SuppressionReason.INVALID,
                    "warmy_verification",
                )
            return

        prospect_id = str(person_row.get("warmy_prospect_id") or "") if person_row else ""
        if not prospect_id:
            existing = self.warmy.find_prospect_by_email(recipient.email)
            prospect_id = str((existing or {}).get("id") or "")
        warmy_payload = {
            "email": recipient.email,
            "first_name": recipient.first_name,
            "last_name": '' if recipient.scope.startswith('company_mailbox:') else _split_name(recipient.full_name)[1],
            "organization_name": company["canonical_name"],
            "title": recipient.title,
            "lead_event_id": sequence.anchor_lead_event_id,
            "outreach_id": sequence.sequence_id,
            "source_contact_candidate_id": recipient.contact_candidate_id,
            "why_line": sequence.personalized_why_line,
            "project_property_name": merge_snapshot["projectPropertyName"],
            "unsubscribe_url": unsubscribe_url,
        }
        if not prospect_id:
            response = self._operation(
                "warmy",
                f"prospect:create:{recipient.email}",
                warmy_payload,
                lambda: self.warmy.create_prospect(
                    warmy_payload,
                    f"aether-prospect-{recipient.email}",
                ),
                reconcile=lambda: self.warmy.find_prospect_by_email(recipient.email),
            )
            prospect = response.get("data") or response
            prospect_id = str(prospect["id"])
        else:
            self._operation(
                "warmy",
                f"prospect:update:{prospect_id}:{merge_hash[:16]}",
                warmy_payload,
                lambda: self.warmy.update_prospect(
                    prospect_id,
                    warmy_payload,
                    f"aether-prospect-update-{prospect_id}-{merge_hash[:16]}",
                ),
                reconcile=lambda: self.warmy.find_prospect_by_email(recipient.email),
            )
        self.db.update_recipient(
            recipient.recipient_id, warmy_prospect_id=prospect_id
        )
        self.db.update_sequence(
            sequence.sequence_id,
            eligibility_status=EligibilityStatus.READY.value,
            eligibility_reasons=[],
            merge_hash=merge_hash,
            payload=sequence.model_dump(mode="json"),
        )
        self.pipedrive.update_lead(
            str(event["pipedrive_lead_id"]),
            self._deal_fields(
                warmy_prospect_id=prospect_id,
                outreach_sequence_id=sequence.sequence_id,
                outreach_state="ready_for_approval",
            ),
        )

    def enroll_sequence(self, payload: dict[str, Any]) -> None:
        sequence_id = str(payload["sequence_id"])
        sequence = self.db.get_sequence(sequence_id)
        if not sequence:
            raise WorkflowRetry(f"sequence not found: {sequence_id}")
        if sequence["eligibility_status"] != EligibilityStatus.READY.value:
            raise ActivationBlocked("sequence is not eligible")
        if not self.db.valid_approval_for_sequence(
            sequence_id,
            campaign_id=self.settings.warmy_campaign_id,
            campaign_manifest_hash=self.settings.warmy_campaign_manifest_hash,
        ):
            raise ActivationBlocked("sequence has no matching immutable approval batch")
        recipient = self.db.get_recipient(
            recipient_id=sequence["primary_recipient_id"]
        )
        if not recipient or not verification_allows_fallback_send(
            VerificationStatus(recipient["verification_status"])
        ):
            raise ActivationBlocked("primary recipient has no sendable verification result")
        if self.db.is_suppressed(recipient["normalized_email"]):
            raise ActivationBlocked("primary recipient is suppressed")
        guard = inspect_recipient_guard(
            self.db, sequence["company_id"], recipient["normalized_email"]
        )
        if guard.identity_exclusion:
            raise ActivationBlocked("recipient belongs to a different company")
        if guard.review_hold:
            raise ActivationBlocked("recipient is held for accuracy review")
        prospect_id = str(recipient.get("warmy_prospect_id") or "")
        if not prospect_id:
            raise WorkflowRetry("Warmy prospect has not been created")
        draft_only = payload.get("draft_only") is True
        self.settings.require_campaign_enrollment(draft_only=draft_only)
        campaign = self.warmy.get_campaign(self.settings.warmy_campaign_id)
        if draft_only:
            campaign_data = campaign.get("data") or campaign
            if str(campaign_data.get("status") or "").casefold() != "draft":
                raise ActivationBlocked("draft-only ingestion requires a live draft campaign")
        mailbox_verification = self._validate_live_campaign(
            campaign, for_enrollment=True
        )
        self.db.set_state(
            f"warmy:campaign:mailbox-verification:{self.settings.warmy_campaign_id}",
            mailbox_verification,
        )
        self._operation(
            "warmy",
            f"enroll:{self.settings.warmy_campaign_id}:{sequence_id}:{prospect_id}",
            payload,
            lambda: self.warmy.enroll(
                self.settings.warmy_campaign_id,
                [prospect_id],
                f"aether-enroll-{self.settings.warmy_campaign_id}-{sequence_id}",
            ),
        )
        self.db.update_sequence(
            sequence_id,
            approval_state=SequenceApprovalState.ENROLLED.value,
            warmy_campaign_id=self.settings.warmy_campaign_id,
        )
        event = self.db.get_lead_event(sequence["anchor_lead_event_id"])
        if event and event.get("pipedrive_lead_id"):
            self.pipedrive.update_lead(
                event["pipedrive_lead_id"],
                self._deal_fields(outreach_state="enrolled"),
            )

    def _block_sequence(
        self, sequence: OutreachSequenceSync, reasons: list[str]
    ) -> None:
        all_reasons = list(dict.fromkeys([*sequence.eligibility_reasons, *reasons]))
        self.db.update_sequence(
            sequence.sequence_id,
            eligibility_status=EligibilityStatus.BLOCKED.value,
            eligibility_reasons=all_reasons,
        )
        self.db.record_eligibility_decision(
            str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"sequence:{sequence.sequence_id}:{'|'.join(all_reasons)}",
                )
            ),
            "outreach_sequence",
            sequence.sequence_id,
            "blocked",
            all_reasons,
            {"merge_hash": sequence.merge_hash},
        )

    def sync_contact(self, payload: dict[str, Any]) -> None:
        contact = ContactSync.model_validate(payload)
        mapping = self.db.get_mapping(outreach_id=contact.outreach_id)
        if mapping is None:
            mapping = MappingRecord(
                outreach_id=contact.outreach_id,
                source_contact_candidate_id=contact.source_contact_candidate_id,
                source_verification_status=contact.source_verification_status,
                source_verification_reason=contact.source_verification_reason,
                source_provider=contact.source_provider,
                email=contact.email,
                lead_event_id=contact.lead_event_id,
                organization_id=contact.organization_id,
                person_id=contact.person_id,
                why_line=contact.why_line,
            )
            self.db.upsert_mapping(mapping)
        else:
            if mapping.email != contact.email:
                self.apply_suppression(
                    mapping.email,
                    SuppressionReason.MANUAL,
                    "scout_contact_revision",
                    mapping=mapping,
                )
                self.db.update_mapping(
                    contact.outreach_id,
                    email=contact.email,
                    pipedrive_person_id=None,
                    warmy_prospect_id=None,
                    warmy_campaign_id=None,
                    warmy_mailbox_id=None,
                    gmail_thread_id=None,
                    verification_status=VerificationStatus.PENDING,
                    reply_disposition=None,
                    reply_received_at=None,
                )
            self.db.update_mapping(
                contact.outreach_id,
                source_contact_candidate_id=contact.source_contact_candidate_id,
                source_verification_status=contact.source_verification_status,
                source_verification_reason=contact.source_verification_reason,
                source_provider=contact.source_provider,
                why_line=contact.why_line,
            )
        mapping = self._mapping(contact.outreach_id)

        token, token_id = issue_unsubscribe_token(
            self.settings.unsubscribe_secret, contact.email
        )
        self.db.store_unsubscribe_token(token_id, contact.email)
        unsubscribe_url = f"{self.settings.public_base_url}/unsubscribe?t={token}"

        self.settings.require_provider_writes()
        organization_id = mapping.pipedrive_organization_id
        if organization_id is None:
            organization_id = self.pipedrive.find_organization(
                contact.organization_name
            )
            if organization_id is None:
                response = self._operation(
                    "pipedrive",
                    f"organization:create:{contact.organization_id}",
                    contact.model_dump(mode="json"),
                    lambda: {
                        "id": self.pipedrive.create_organization(
                            contact.organization_name,
                            self.settings.pipedrive_jordan_user_id,
                            contact.location,
                        )
                    },
                )
                organization_id = int(response["id"])
            self.db.update_mapping(
                contact.outreach_id,
                pipedrive_organization_id=organization_id,
            )

        person_id = mapping.pipedrive_person_id
        person_fields = self._person_fields(
            aether_person_id=contact.person_id,
            verification_status=VerificationStatus.PENDING.value,
            suppressed="yes" if self.db.is_suppressed(contact.email) else "no",
            suppression_reason=""
            if not self.db.is_suppressed(contact.email)
            else SuppressionReason.MANUAL.value,
            unsubscribe_url=unsubscribe_url,
        )
        if person_id is None:
            person_id = self.pipedrive.find_person(contact.email)
            if person_id is None:
                response = self._operation(
                    "pipedrive",
                    f"person:create:{contact.person_id}:{contact.email}",
                    contact.model_dump(mode="json"),
                    lambda: {
                        "id": self.pipedrive.create_person(
                            contact.person_name,
                            contact.email,
                            organization_id,
                            self.settings.pipedrive_jordan_user_id,
                            person_fields,
                        )
                    },
                )
                person_id = int(response["id"])
            self.db.update_mapping(contact.outreach_id, pipedrive_person_id=person_id)
        if person_fields:
            self.pipedrive.update_person(person_id, person_fields)

        lead_id = mapping.pipedrive_lead_id
        lead_fields = self._deal_fields(
            aether_lead_event_id=contact.lead_event_id,
            aether_outreach_id=contact.outreach_id,
            aether_contact_candidate_id=contact.source_contact_candidate_id,
            outreach_state="suppressed"
            if self.db.is_suppressed(contact.email)
            else "created",
            unsubscribe_url=unsubscribe_url,
            article_url=contact.article_url,
            date_posted=contact.date_posted,
        )
        if lead_id is None:
            lead_id = self.pipedrive.find_lead_by_outreach_id(contact.outreach_id)
            if lead_id is None:
                response = self._operation(
                    "pipedrive",
                    f"lead:create:{contact.outreach_id}",
                    contact.model_dump(mode="json"),
                    lambda: {
                        "id": self.pipedrive.create_lead(
                            _lead_title(contact),
                            person_id,
                            organization_id,
                            self.settings.pipedrive_jordan_user_id,
                            lead_fields,
                        )
                    },
                )
                lead_id = str(response["id"])
            self.db.update_mapping(contact.outreach_id, pipedrive_lead_id=lead_id)
        self.pipedrive.update_lead(
            lead_id,
            {
                "person_id": person_id,
                "organization_id": organization_id,
                **lead_fields,
            },
        )
        if mapping.pipedrive_deal_id:
            self.pipedrive.update_deal(
                mapping.pipedrive_deal_id,
                {"person_id": person_id, "custom_fields": lead_fields},
            )

        if self.db.is_suppressed(contact.email):
            return

        mapping = self.db.get_mapping(outreach_id=contact.outreach_id)
        if not mapping.warmy_prospect_id:
            reusable = next(
                (
                    item
                    for item in self.db.get_mappings_by_email(contact.email)
                    if item.warmy_prospect_id
                ),
                None,
            )
            if reusable:
                self.db.update_mapping(
                    contact.outreach_id,
                    warmy_prospect_id=reusable.warmy_prospect_id,
                )
                mapping = self.db.get_mapping(outreach_id=contact.outreach_id)
        if not mapping.warmy_prospect_id:
            first_name, last_name = _split_name(contact.person_name)
            warmy_payload = contact.model_dump(mode="json") | {
                "first_name": first_name,
                "last_name": last_name,
                "unsubscribe_url": unsubscribe_url,
            }
            response = self._operation(
                "warmy",
                f"prospect:create:{contact.email}",
                warmy_payload,
                lambda: self.warmy.create_prospect(
                    warmy_payload,
                    f"aether-prospect-{contact.email}",
                ),
            )
            prospect = response.get("data") or response
            prospect_id = str(prospect["id"])
            self.db.update_mapping(
                contact.outreach_id,
                warmy_prospect_id=prospect_id,
            )
        else:
            first_name, last_name = _split_name(contact.person_name)
            warmy_payload = contact.model_dump(mode="json") | {
                "first_name": first_name,
                "last_name": last_name,
                "unsubscribe_url": unsubscribe_url,
            }
            revision = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{contact.outreach_id}:{contact.why_line}",
            ).hex[:16]
            self._operation(
                "warmy",
                f"prospect:update:{mapping.warmy_prospect_id}:{revision}",
                warmy_payload,
                lambda: self.warmy.update_prospect(
                    mapping.warmy_prospect_id,
                    warmy_payload,
                    f"aether-prospect-update-{mapping.warmy_prospect_id}-{revision}",
                ),
            )
        mapping = self._mapping(contact.outreach_id)
        fields = self._deal_fields(warmy_prospect_id=mapping.warmy_prospect_id)
        if fields:
            self.pipedrive.update_lead(lead_id, fields)

        self.db.enqueue_work(
            "warmy.verify",
            f"warmy:verify:{contact.outreach_id}:{contact.email}",
            {
                "outreach_id": contact.outreach_id,
                "email": contact.email,
            },
        )

    def verify_contact(self, payload: dict[str, Any]) -> None:
        outreach_id = str(payload["outreach_id"])
        email = str(payload["email"]).strip().casefold()
        mapping = self._mapping(outreach_id)
        if mapping.email != email:
            return
        response = self._operation(
            "warmy",
            f"verify:{mapping.email}",
            payload,
            lambda: self.warmy.verify_email(email, f"aether-verify-{mapping.email}"),
        )
        data = response.get("data") or response
        status = _verification_status(data)
        self.db.update_mapping(outreach_id, verification_status=status)
        fields = self._person_fields(verification_status=status.value)
        if mapping.pipedrive_person_id and fields:
            self.pipedrive.update_person(mapping.pipedrive_person_id, fields)

        if verification_allows_fallback_send(status):
            self.db.enqueue_work(
                "warmy.enroll",
                f"warmy:enroll:{self.settings.warmy_campaign_id}:{mapping.warmy_prospect_id}",
                {"outreach_id": outreach_id},
            )
        elif status == VerificationStatus.INVALID:
            self.apply_suppression(
                email,
                SuppressionReason.INVALID,
                "warmy_verification",
                mapping=mapping,
            )

    def enroll_contact(self, payload: dict[str, Any]) -> None:
        outreach_id = str(payload["outreach_id"])
        mapping = self._mapping(outreach_id)
        if not verification_allows_fallback_send(mapping.verification_status):
            return
        if self.db.is_suppressed(mapping.email):
            return
        self.settings.require_campaign_activation()
        if not mapping.why_line:
            raise ActivationBlocked("campaign activation blocked: why_line missing")
        if not mapping.warmy_prospect_id:
            raise WorkflowRetry("Warmy prospect has not been created")
        campaign = self.warmy.get_campaign(self.settings.warmy_campaign_id)
        self._validate_live_campaign(campaign)
        route_key = f"warmy-route:{mapping.warmy_prospect_id}"
        route = self.db.get_state(route_key) or {}
        if route.get("outreach_id") not in (None, "", outreach_id):
            if mapping.pipedrive_lead_id:
                fields = self._deal_fields(outreach_state="duplicate_email")
                if fields:
                    self.pipedrive.update_lead(mapping.pipedrive_lead_id, fields)
            return
        self._operation(
            "warmy",
            f"enroll:{self.settings.warmy_campaign_id}:{mapping.warmy_prospect_id}",
            payload,
            lambda: self.warmy.enroll(
                self.settings.warmy_campaign_id,
                [mapping.warmy_prospect_id],
                f"aether-enroll-{self.settings.warmy_campaign_id}-{mapping.warmy_prospect_id}",
            ),
        )
        self.db.update_mapping(
            outreach_id,
            warmy_campaign_id=self.settings.warmy_campaign_id,
        )
        self.db.set_state(
            route_key,
            {
                "outreach_id": outreach_id,
                "campaign_id": self.settings.warmy_campaign_id,
            },
        )
        if mapping.pipedrive_lead_id:
            fields = self._deal_fields(outreach_state="enrolled")
            if fields:
                self.pipedrive.update_lead(mapping.pipedrive_lead_id, fields)

    def handle_warmy_event(self, payload: dict[str, Any]) -> None:
        event_type = str(
            payload.get("event_type")
            or payload.get("type")
            or payload.get("event")
            or ""
        )
        data = payload.get("data") or {}
        event_id = str(payload.get("event_id") or "")
        email = str(data.get("prospectEmail") or data.get("email") or "").casefold()
        prospect_id = str(data.get("prospectId") or "")
        campaign_id = str(data.get("campaignId") or "")
        if campaign_id and campaign_id != self.settings.warmy_campaign_id:
            LOG.info(
                "ignoring Warmy event for a foreign campaign",
                extra={"event_id": event_id, "campaign_id": campaign_id},
            )
            return
        get_sequence_for_prospect = getattr(
            self.db, "get_sequence_for_prospect", None
        )
        sequence = (
            get_sequence_for_prospect(prospect_id, campaign_id)
            if prospect_id and get_sequence_for_prospect is not None
            else None
        )
        if sequence is not None:
            self._handle_sequence_warmy_event(
                sequence,
                event_type=event_type,
                event_id=event_id,
                data=data,
                email=email,
            )
            return
        mapping = None
        if prospect_id:
            route = self.db.get_state(f"warmy-route:{prospect_id}") or {}
            if route.get("outreach_id"):
                mapping = self.db.get_mapping(outreach_id=route["outreach_id"])
            if mapping is None:
                mapping = self.db.get_mapping(warmy_prospect_id=prospect_id)
        elif email:
            candidates = self.db.get_mappings_by_email(email)
            mapping = next(
                (item for item in candidates if item.warmy_campaign_id),
                candidates[0] if candidates else None,
            )

        if event_type == "email.bounced":
            if email:
                self.apply_suppression(
                    email,
                    SuppressionReason.BOUNCE,
                    "warmy",
                    external_event_id=event_id,
                    mapping=mapping,
                )
            return
        if event_type in {"email.unsubscribed", "prospect.suppressed"}:
            if email:
                self.apply_suppression(
                    email,
                    SuppressionReason.UNSUBSCRIBE,
                    "warmy",
                    external_event_id=event_id,
                    mapping=mapping,
                )
            return
        if event_type != "reply.received":
            return
        if mapping is None:
            raise WorkflowRetry(f"no mapping for Warmy reply {prospect_id or email}")

        received_at = _parse_datetime(data.get("receivedAt"))
        self.db.update_mapping(
            mapping.outreach_id,
            reply_disposition=ReplyDisposition.PENDING_REVIEW,
            reply_received_at=received_at,
            warmy_mailbox_id=str(data.get("mailboxId") or "") or None,
        )
        mailbox_id = str(data.get("mailboxId") or "")
        review_note = "The reply is available in the WarmySender Inbox."
        if self.settings.gmail_reply_forwarding_enabled:
            mailbox = self.settings.warmy_mailbox_emails.get(mailbox_id, "")
            message_ref = str(data.get("messageId") or "")
            if not mailbox:
                raise WorkflowRetry(
                    f"no Gmail mailbox mapping for Warmy mailbox {mailbox_id}"
                )
            gmail_message_id = self.gmail.find_message(mailbox, message_ref)
            if not gmail_message_id:
                raise WorkflowRetry(f"Gmail message not indexed yet: {message_ref}")
            self._operation(
                "gmail",
                f"forward:{event_id or gmail_message_id}",
                {"mailbox": mailbox, "message_id": gmail_message_id},
                lambda: self.gmail.forward_message(
                    mailbox, gmail_message_id, self.settings.gmail_forward_to
                ),
            )
            message = self.gmail.get_message(
                mailbox, gmail_message_id, format="metadata"
            )
            thread_id = str(message.get("threadId") or "")
            if thread_id:
                self.db.update_mapping(mapping.outreach_id, gmail_thread_id=thread_id)
            review_note = "The full message was forwarded to Jordan."
        if mapping.pipedrive_lead_id:
            self._operation(
                "pipedrive",
                f"review-activity:{event_id or gmail_message_id}",
                payload,
                lambda: {
                    "id": self.pipedrive.add_lead_activity(
                        mapping.pipedrive_lead_id,
                        "Review Warmy reply",
                        self.settings.pipedrive_jordan_user_id,
                        note=(
                            f"Reply received from {mapping.email}. "
                            f"Subject: {data.get('subject') or '(no subject)'}. "
                            f"{review_note}"
                        ),
                    )
                },
            )
            fields = self._deal_fields(
                reply_disposition=ReplyDisposition.PENDING_REVIEW.value,
                reply_received_at=received_at.isoformat(),
                outreach_state="reply_review",
            )
            if fields:
                self.pipedrive.update_lead(mapping.pipedrive_lead_id, fields)

    def handle_pipedrive_event(self, payload: dict[str, Any]) -> None:
        meta = payload.get("meta") or {}
        current = payload.get("current") or payload.get("data") or {}
        entity = str(
            meta.get("entity") or meta.get("object") or current.get("object") or ""
        )
        if entity not in {"lead", "leads"}:
            return
        lead_id = str(
            meta.get("entity_id") or meta.get("id") or current.get("id") or ""
        )
        get_lead_event = getattr(self.db, "get_lead_event_by_pipedrive_id", None)
        event_row = get_lead_event(lead_id) if get_lead_event is not None else None
        if event_row is not None:
            sequence = self.db.get_sequence_for_event(event_row["lead_event_id"])
            if sequence is None:
                return
            field_key = self.settings.pipedrive_deal_fields.get(
                "reply_disposition", ""
            )
            custom_fields = current.get("custom_fields") or {}
            raw_disposition = (
                custom_fields.get(field_key)
                if field_key
                else current.get("reply_disposition")
            )
            disposition = self._disposition(raw_disposition)
            if disposition is None:
                return
            self.db.update_sequence(
                sequence["sequence_id"], reply_disposition=disposition.value
            )
            if disposition == ReplyDisposition.POSITIVE:
                self.db.enqueue_work(
                    "pipedrive.sequence.convert",
                    f"pipedrive:sequence:convert:{sequence['sequence_id']}",
                    {"sequence_id": sequence["sequence_id"]},
                )
            elif disposition in {
                ReplyDisposition.NEGATIVE,
                ReplyDisposition.UNSUBSCRIBE,
            }:
                recipient = self.db.get_recipient(
                    recipient_id=sequence["primary_recipient_id"]
                )
                if recipient:
                    reason = (
                        SuppressionReason.NEGATIVE_REPLY
                        if disposition == ReplyDisposition.NEGATIVE
                        else SuppressionReason.UNSUBSCRIBE
                    )
                    self.db.suppress(
                        recipient["normalized_email"], reason, "pipedrive"
                    )
                for linked in self.db.sequence_events(sequence["sequence_id"]):
                    if linked.get("pipedrive_lead_id"):
                        self.pipedrive.archive_lead(linked["pipedrive_lead_id"])
            return
        mapping = self.db.get_mapping(pipedrive_lead_id=lead_id)
        if mapping is None:
            return
        field_key = self.settings.pipedrive_deal_fields.get("reply_disposition", "")
        custom_fields = current.get("custom_fields") or {}
        raw_disposition = (
            custom_fields.get(field_key)
            if field_key
            else current.get("reply_disposition")
        )
        disposition = self._disposition(raw_disposition)
        if disposition is None:
            return
        self.db.update_mapping(mapping.outreach_id, reply_disposition=disposition)
        if disposition == ReplyDisposition.POSITIVE:
            self.db.enqueue_work(
                "pipedrive.convert",
                f"pipedrive:convert:{mapping.pipedrive_lead_id}",
                {"outreach_id": mapping.outreach_id},
            )
        elif disposition in {ReplyDisposition.NEGATIVE, ReplyDisposition.UNSUBSCRIBE}:
            reason = (
                SuppressionReason.NEGATIVE_REPLY
                if disposition == ReplyDisposition.NEGATIVE
                else SuppressionReason.UNSUBSCRIBE
            )
            self.apply_suppression(mapping.email, reason, "pipedrive", mapping=mapping)
            if mapping.pipedrive_lead_id:
                self.pipedrive.archive_lead(mapping.pipedrive_lead_id)

    def _handle_sequence_warmy_event(
        self,
        sequence: dict[str, Any],
        *,
        event_type: str,
        event_id: str,
        data: dict[str, Any],
        email: str,
    ) -> None:
        recipient = self.db.get_recipient(
            recipient_id=sequence["primary_recipient_id"]
        )
        recipient_email = (
            str(recipient["normalized_email"])
            if recipient
            else email.strip().casefold()
        )
        if event_type in {
            "email.bounced",
            "email.unsubscribed",
            "prospect.suppressed",
        }:
            reason = (
                SuppressionReason.BOUNCE
                if event_type == "email.bounced"
                else SuppressionReason.UNSUBSCRIBE
            )
            if recipient_email:
                self.db.suppress(
                    recipient_email,
                    reason,
                    "warmy",
                    external_event_id=event_id,
                )
            self.db.update_sequence(
                sequence["sequence_id"],
                eligibility_status=EligibilityStatus.BLOCKED.value,
                eligibility_reasons=[reason.value],
                reply_disposition=(
                    ReplyDisposition.UNSUBSCRIBE.value
                    if reason == SuppressionReason.UNSUBSCRIBE
                    else ReplyDisposition.OTHER.value
                ),
            )
            for event in self.db.sequence_events(sequence["sequence_id"]):
                if event.get("pipedrive_lead_id"):
                    self.pipedrive.update_lead(
                        event["pipedrive_lead_id"],
                        self._deal_fields(outreach_state="suppressed"),
                    )
            return
        if event_type != "reply.received":
            return
        received_at = _parse_datetime(data.get("receivedAt"))
        mailbox_id = str(data.get("mailboxId") or "")
        self.db.update_sequence(
            sequence["sequence_id"],
            approval_state=SequenceApprovalState.REPLIED.value,
            reply_disposition=ReplyDisposition.PENDING_REVIEW.value,
            reply_received_at=received_at.isoformat(),
            warmy_mailbox_id=mailbox_id or None,
        )
        anchor = self.db.get_lead_event(sequence["anchor_lead_event_id"])
        if not anchor or not anchor.get("pipedrive_lead_id"):
            raise WorkflowRetry("sequence reply has no anchor Pipedrive Lead")
        review_note = "The reply is available in the WarmySender Inbox."
        if self.settings.gmail_reply_forwarding_enabled:
            mailbox = self.settings.warmy_mailbox_emails.get(mailbox_id, "")
            message_ref = str(data.get("messageId") or "")
            if not mailbox:
                raise WorkflowRetry(
                    f"no Gmail mailbox mapping for Warmy mailbox {mailbox_id}"
                )
            if not message_ref:
                raise WorkflowRetry("Warmy reply did not include a message ID")
            gmail_message_id = self.gmail.find_message(mailbox, message_ref)
            if not gmail_message_id:
                raise WorkflowRetry(f"Gmail message not indexed yet: {message_ref}")
            self._operation(
                "gmail",
                f"forward:sequence:{event_id or gmail_message_id}",
                {"mailbox": mailbox, "message_id": gmail_message_id},
                lambda: self.gmail.forward_message(
                    mailbox, gmail_message_id, self.settings.gmail_forward_to
                ),
            )
            message = self.gmail.get_message(
                mailbox, gmail_message_id, format="metadata"
            )
            thread_id = str(message.get("threadId") or "")
            if thread_id:
                self.db.set_state(
                    f"sequence-gmail-route:{sequence['sequence_id']}",
                    {"mailbox": mailbox, "thread_id": thread_id},
                )
            review_note = "The full message was forwarded to Jordan."
        self._operation(
            "pipedrive",
            f"review-activity:{event_id or sequence['sequence_id']}",
            data,
            lambda: {
                "id": self.pipedrive.add_lead_activity(
                    anchor["pipedrive_lead_id"],
                    "Review Warmy reply",
                    self.settings.pipedrive_jordan_user_id,
                    note=(
                        f"Reply received from {recipient_email}. "
                        f"Subject: {data.get('subject') or '(no subject)'}. "
                        f"{review_note}"
                    ),
                )
            },
        )
        self.pipedrive.update_lead(
            anchor["pipedrive_lead_id"],
            self._deal_fields(
                reply_disposition=ReplyDisposition.PENDING_REVIEW.value,
                reply_received_at=received_at.isoformat(),
                outreach_state="reply_review",
            ),
        )

    def convert_sequence(self, payload: dict[str, Any]) -> None:
        sequence_id = str(payload["sequence_id"])
        sequence = self.db.get_sequence(sequence_id)
        if not sequence:
            raise WorkflowRetry(f"sequence not found: {sequence_id}")
        if sequence.get("pipedrive_deal_id"):
            return
        anchor = self.db.get_lead_event(sequence["anchor_lead_event_id"])
        if not anchor or not anchor.get("pipedrive_lead_id"):
            raise WorkflowRetry("anchor Pipedrive Lead has not been created")
        existing_deal = self.pipedrive.find_deal_by_sequence_id(sequence_id)
        if existing_deal:
            deal_id = existing_deal
        else:
            response = self._operation(
                "pipedrive",
                f"convert:sequence:{sequence_id}:{anchor['pipedrive_lead_id']}",
                payload,
                lambda: {
                    "conversion_id": self.pipedrive.convert_lead(
                        anchor["pipedrive_lead_id"],
                        self.settings.pipedrive_stage_id,
                    )
                },
                reconcile=lambda: (
                    {"deal_id": found, "reconciled": True}
                    if (found := self.pipedrive.find_deal_by_sequence_id(sequence_id))
                    else None
                ),
            )
            deal_id = int(response.get("deal_id") or 0)
            if not deal_id:
                conversion_id = str(response["conversion_id"])
                status = self.pipedrive.conversion_status(
                    anchor["pipedrive_lead_id"], conversion_id
                )
                if status.get("status") in {"not_started", "running"}:
                    raise WorkflowRetry("Pipedrive Lead conversion is still running")
                if status.get("status") != "completed":
                    raise RuntimeError(f"Pipedrive Lead conversion failed: {status}")
                deal_id = int(status.get("deal_id") or status.get("dealId"))
        outreach_state = (
            "qualified_reply"
            if self.settings.pipedrive_automation_ready
            else "qualified_reply_no_automation"
        )
        self.pipedrive.update_deal(
            int(deal_id),
            {
                "owner_id": self.settings.pipedrive_jordan_user_id,
                "stage_id": self.settings.pipedrive_stage_id,
                "custom_fields": self._deal_fields(
                    outreach_sequence_id=sequence_id,
                    outreach_state=outreach_state,
                ),
            },
        )
        linked = self.db.sequence_events(sequence_id)
        note_lines = ["Aether supporting lead events:"]
        for event in linked:
            event_payload = event["payload"]
            note_lines.append(
                f"- {event_payload.get('event')}: "
                f"{event_payload.get('article_url') or '(no article URL)'}"
            )
        self._operation(
            "pipedrive",
            f"deal-note:sequence:{sequence_id}:{deal_id}",
            {"sequence_id": sequence_id, "deal_id": deal_id},
            lambda: {
                "id": self.pipedrive.add_note(
                    "\n".join(note_lines), deal_id=int(deal_id)
                )
            },
        )
        for event in linked:
            self.db.update_lead_event(
                event["lead_event_id"],
                pipedrive_deal_id=int(deal_id),
                crm_state="converted" if event["event_role"] == "anchor" else "archived_supporting",
            )
            if (
                event["event_role"] == "supporting"
                and event.get("pipedrive_lead_id")
            ):
                self.pipedrive.archive_lead(event["pipedrive_lead_id"])
        self.db.update_sequence(
            sequence_id,
            pipedrive_deal_id=int(deal_id),
            approval_state=SequenceApprovalState.REPLIED.value,
        )

    def convert_lead(self, payload: dict[str, Any]) -> None:
        if not self.settings.pipedrive_automation_ready:
            raise ActivationBlocked(
                "PIPEDRIVE_AUTOMATION_READY is disabled; conversion would miss follow-ups"
            )
        mapping = self._mapping(str(payload["outreach_id"]))
        if mapping.pipedrive_deal_id:
            return
        if not mapping.pipedrive_lead_id:
            raise WorkflowRetry("Pipedrive Lead has not been created")
        state_key = f"conversion:{mapping.pipedrive_lead_id}"
        state = self.db.get_state(state_key) or {}
        conversion_id = state.get("conversion_id")
        if not conversion_id:
            response = self._operation(
                "pipedrive",
                f"convert:{mapping.pipedrive_lead_id}",
                payload,
                lambda: {
                    "conversion_id": self.pipedrive.convert_lead(
                        mapping.pipedrive_lead_id,
                        self.settings.pipedrive_stage_id,
                    )
                },
            )
            conversion_id = response["conversion_id"]
            self.db.set_state(state_key, {"conversion_id": conversion_id})
        status = self.pipedrive.conversion_status(
            mapping.pipedrive_lead_id, conversion_id
        )
        if status.get("status") in {"not_started", "running"}:
            raise WorkflowRetry("Pipedrive Lead conversion is still running")
        if status.get("status") != "completed":
            raise RuntimeError(f"Pipedrive Lead conversion failed: {status}")
        deal_id = int(status.get("deal_id") or status.get("dealId"))
        self.pipedrive.update_deal(
            deal_id,
            {
                "owner_id": self.settings.pipedrive_jordan_user_id,
                "stage_id": self.settings.pipedrive_stage_id,
                "custom_fields": self._deal_fields(outreach_state="qualified_reply"),
            },
        )
        self.db.update_mapping(mapping.outreach_id, pipedrive_deal_id=deal_id)

    def apply_suppression(
        self,
        email: str,
        reason: SuppressionReason,
        source: str,
        *,
        external_event_id: str = "",
        mapping: MappingRecord | None = None,
    ) -> None:
        self.db.suppress(
            email,
            reason,
            source,
            external_event_id=external_event_id,
        )
        self.db.enqueue_work(
            "suppression.sync",
            f"suppression:{email.casefold()}:{reason.value}",
            {
                "email": email,
                "reason": reason.value,
                "source": source,
                "campaign_ids": [mapping.warmy_campaign_id]
                if mapping and mapping.warmy_campaign_id
                else [],
                "person_ids": [mapping.pipedrive_person_id]
                if mapping and mapping.pipedrive_person_id
                else [],
                "lead_ids": [mapping.pipedrive_lead_id]
                if mapping and mapping.pipedrive_lead_id
                else [],
                "deal_ids": [mapping.pipedrive_deal_id]
                if mapping and mapping.pipedrive_deal_id
                else [],
            },
        )
        if mapping and reason in {SuppressionReason.BOUNCE, SuppressionReason.INVALID}:
            self.db.update_mapping(
                mapping.outreach_id,
                verification_status=VerificationStatus.INVALID,
            )

    def sync_suppression(self, payload: dict[str, Any]) -> None:
        email = str(payload["email"]).casefold()
        reason = str(payload["reason"])
        mappings = self.db.get_mappings_by_email(email)
        self._operation(
            "warmy",
            f"suppress:{email}",
            payload,
            lambda: self.warmy.suppress([email], reason, f"aether-suppress-{email}"),
        )
        campaign_ids = set(payload.get("campaign_ids") or []) | {
            mapping.warmy_campaign_id
            for mapping in mappings
            if mapping.warmy_campaign_id
        }
        for campaign_id in campaign_ids:
            self._operation(
                "warmy",
                f"unenroll:{campaign_id}:{email}",
                payload,
                lambda campaign_id=campaign_id: self.warmy.unenroll(
                    campaign_id,
                    [email],
                    f"aether-unenroll-{campaign_id}-{email}",
                ),
            )
        person_ids = set(payload.get("person_ids") or []) | {
            mapping.pipedrive_person_id
            for mapping in mappings
            if mapping.pipedrive_person_id
        }
        for person_id in person_ids:
            fields = self._person_fields(suppressed="yes", suppression_reason=reason)
            if fields:
                self.pipedrive.update_person(person_id, fields)
        lead_ids = set(payload.get("lead_ids") or []) | {
            mapping.pipedrive_lead_id
            for mapping in mappings
            if mapping.pipedrive_lead_id
        }
        for lead_id in lead_ids:
            fields = self._deal_fields(outreach_state="suppressed")
            if fields:
                self.pipedrive.update_lead(lead_id, fields)
        deal_ids = set(payload.get("deal_ids") or []) | {
            mapping.pipedrive_deal_id
            for mapping in mappings
            if mapping.pipedrive_deal_id
        }
        for deal_id in deal_ids:
            fields = self._deal_fields(outreach_state="suppressed")
            if fields:
                self.pipedrive.update_deal(
                    deal_id,
                    {"custom_fields": fields},
                )

    def sync_jordan_inbox(self, payload: dict[str, Any]) -> None:
        mailbox = self.settings.gmail_forward_to
        state_key = f"gmail-history:{mailbox}"
        state = self.db.get_state(state_key)
        if not state:
            profile = self.gmail.profile(mailbox)
            self.db.set_state(state_key, {"history_id": str(profile["historyId"])})
            return
        try:
            response = self.gmail.history(mailbox, str(state["history_id"]))
            message_ids = [
                str((added.get("message") or {}).get("id") or "")
                for event in response.get("history") or []
                for added in event.get("messagesAdded") or []
            ]
            latest = str(response.get("historyId") or state["history_id"])
        except GmailHistoryExpired:
            message_ids = self.gmail.recent_inbox_messages(
                mailbox, newer_than_days=7, limit=500
            )
            latest = str(self.gmail.profile(mailbox)["historyId"])
        for message_id in dict.fromkeys(filter(None, message_ids)):
            self._sync_jordan_message(mailbox, message_id)
        self.db.set_state(state_key, {"history_id": latest})

    def unsubscribe(self, email: str) -> None:
        mapping = self.db.get_mapping(email=email)
        self.apply_suppression(
            email,
            SuppressionReason.UNSUBSCRIBE,
            "unsubscribe_link",
            mapping=mapping,
        )

    def _mapping(self, outreach_id: str) -> MappingRecord:
        mapping = self.db.get_mapping(outreach_id=outreach_id)
        if mapping is None:
            raise WorkflowRetry(f"mapping not found: {outreach_id}")
        return mapping

    def _sync_jordan_message(self, mailbox: str, message_id: str) -> None:
        message = self.gmail.get_message(mailbox, message_id, format="metadata")
        headers = {
            item["name"].casefold(): item["value"]
            for item in (message.get("payload") or {}).get("headers") or []
        }
        sender = parseaddr(headers.get("from", ""))[1].casefold()
        mapping = self.db.get_mapping(email=sender) if sender else None
        if not mapping or not mapping.pipedrive_deal_id:
            return
        self.db.update_mapping(
            mapping.outreach_id,
            reply_received_at=datetime.now(UTC),
            gmail_thread_id=str(message.get("threadId") or "") or None,
        )
        fields = self._deal_fields(outreach_state="replied")
        if fields:
            self.pipedrive.update_deal(
                mapping.pipedrive_deal_id,
                {"custom_fields": fields},
            )

    def _validate_live_campaign(
        self, response: dict[str, Any], *, for_enrollment: bool = False
    ) -> dict[str, Any]:
        campaign = response.get("data") if isinstance(response, dict) else None
        if not isinstance(campaign, dict):
            campaign = response
        errors: list[str] = []
        if str(campaign.get("id") or "") != self.settings.warmy_campaign_id:
            errors.append("campaign ID mismatch")
        status = str(campaign.get("status") or "").casefold()
        allowed_statuses = {"draft", "paused", "scheduled", "running"}
        if for_enrollment and not self.settings.campaign_start_enabled:
            allowed_statuses = {"draft", "paused"}
        if status not in allowed_statuses:
            errors.append(f"unsafe campaign status {status or '(missing)'}")
        mailbox_field_present = "mailboxIds" in campaign or "mailboxes" in campaign
        mailbox_values = campaign.get("mailboxIds")
        if mailbox_values is None and "mailboxes" in campaign:
            mailbox_values = campaign.get("mailboxes")
        mailbox_ids = {
            str(item.get("id") if isinstance(item, dict) else item)
            for item in mailbox_values or []
        }
        mailbox_mismatch = mailbox_field_present and mailbox_ids != set(
            self.settings.warmy_mailbox_ids
        )
        if mailbox_mismatch:
            errors.append("mailbox set mismatch")
        mailbox_verification = (
            {
                "status": "mismatch",
                "requires_ui_check": False,
            }
            if mailbox_mismatch
            else (
                {
                    "status": "verified",
                    "requires_ui_check": False,
                }
                if mailbox_field_present
                else {
                    "status": "not_returned",
                    "requires_ui_check": True,
                    "reason": (
                        "Warmy readback omitted mailboxIds/mailboxes; verify in the UI"
                    ),
                }
            )
        )
        if int(campaign.get("dailySendLimit") or 0) != self.settings.warmy_daily_limit:
            errors.append("daily send limit mismatch")
        if len(campaign.get("steps") or []) != 4:
            errors.append("campaign must contain four steps")
        for name in ("stopOnReply", "stopOnBounce", "stopOnUnsubscribe"):
            if campaign.get(name) is not True:
                errors.append(f"{name} must be enabled")
        hash_payload = dict(campaign)
        if not mailbox_field_present:
            hash_payload["mailboxIds"] = list(self.settings.warmy_mailbox_ids)
        elif "mailboxIds" not in hash_payload:
            hash_payload["mailboxIds"] = mailbox_values
        actual_hash = campaign_manifest_hash(hash_payload)
        if actual_hash != self.settings.warmy_campaign_manifest_hash:
            errors.append("campaign manifest hash mismatch")
        if errors:
            raise ActivationBlocked("live Warmy campaign blocked: " + ", ".join(errors))
        return mailbox_verification

    def _deal_fields(self, **values: Any) -> dict[str, Any]:
        result: dict[str, Any] = {}
        disposition_ids = {
            value.casefold().replace(" ", "_"): option_id
            for option_id, value in self.settings.pipedrive_reply_disposition_values.items()
        }
        for semantic, field_key in self.settings.pipedrive_deal_fields.items():
            if semantic not in values or not field_key:
                continue
            value = values[semantic]
            if value in (None, ""):
                continue
            if semantic == "reply_disposition":
                normalized = str(value).casefold().replace(" ", "_")
                value = disposition_ids.get(normalized, value)
                if str(value).isdigit():
                    value = int(value)
            result[field_key] = value
        return result

    def _person_fields(self, **values: Any) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for semantic, field_key in self.settings.pipedrive_person_fields.items():
            if semantic not in values or not field_key:
                continue
            value = values[semantic]
            if value in (None, ""):
                continue
            normalized = str(value).casefold().replace(" ", "_")
            option_id = self.settings.pipedrive_person_enum_values.get(
                f"{semantic}.{normalized}"
            )
            if option_id:
                value = int(option_id) if option_id.isdigit() else option_id
            else:
                value = str(value)
            result[field_key] = value
        return result

    def _disposition(self, value: Any) -> ReplyDisposition | None:
        if value is None:
            return None
        if isinstance(value, dict):
            value = value.get("id", value.get("value", value.get("label")))
        if value is None:
            return None
        normalized = (
            self.settings.pipedrive_reply_disposition_values.get(str(value), str(value))
            .casefold()
            .replace(" ", "_")
        )
        try:
            return ReplyDisposition(normalized)
        except ValueError:
            return None


def _external_id(response: dict[str, Any]) -> str:
    data = response.get("data") if isinstance(response, dict) else None
    target = data if isinstance(data, dict) else response
    return str(target.get("id") or target.get("conversion_id") or "")


def _split_name(name: str) -> tuple[str, str]:
    first, separator, last = name.strip().partition(" ")
    return first, last if separator else ""


def _lead_title(contact: ContactSync) -> str:
    subject = contact.event or contact.organization_name
    return f"{contact.organization_name} — {contact.person_name} — {subject}"[:255]


def _verification_status(data: dict[str, Any]) -> VerificationStatus:
    status = str(data.get("status") or "unknown").casefold().replace("-", "_")
    if data.get("is_catch_all") or status in {"catch_all", "catchall"}:
        return VerificationStatus.CATCH_ALL
    if status in {"valid", "deliverable", "verified"}:
        return VerificationStatus.VALID
    if status in {"invalid", "undeliverable", "rejected"}:
        return VerificationStatus.INVALID
    return VerificationStatus.UNKNOWN


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if value:
        return datetime.fromisoformat(str(value))
    return datetime.now(UTC)
