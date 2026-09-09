"""Validated V2 state projections to compatibility CSV, JSONL, and HTML."""
from __future__ import annotations

import json
import hashlib
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

import build_email
import csvio

from .artifacts import ArtifactStore
from .contracts import ContactCandidate, DiscoveryCandidate, Organization, Person
from .contracts import CompanyProfile, OutreachRecipient, RecordStatus
from .ids import stable_uuid
from .state import StateStore
from outreach_contract import WHY_LINE_PROTOCOL_VERSION, personalize_why_line
from integration.handoff import (
    HANDOFF_PROTOCOL_VERSION,
    HANDOFF_SCHEMA_VERSION,
    handoff_content_hash,
)
from integration.models import (
    CompanySync,
    EligibilityStatus,
    EventRole,
    LeadEventSync,
    OutreachSequenceSync,
    RecipientSync,
    SalesHandoff,
)


LEAD_FIELDS = [
    "link",
    "business_name",
    "person",
    "event",
    "date_posted",
    "location",
    "summary",
    "why_line",
    "why_template_key",
    "why_confidence",
    "why_line_status",
    "why_sources",
    "state",
    "source_site",
    "aka",
    "priority",
    "property_type",
    "service_angle",
    "filter_reason",
    "Decision_Makers",
    "Employee_Count",
    "Decision_Maker_Sources",
    "score",
    "lead_event_id",
    "organization_id",
    "primary_candidate_id",
    "supporting_candidate_ids",
    "run_id",
    "record_status",
    "provenance_json",
]
CONTACT_FIELDS = [
    "business_name",
    "state",
    "location",
    "event",
    "date_posted",
    "summary",
    "why_line",
    "why_template_key",
    "why_confidence",
    "why_line_status",
    "why_sources",
    "link",
    "employee_count",
    "person",
    "title",
    "linkedin",
    "email",
    "phone",
    "sources",
    "score",
    "lead_event_id",
    "organization_id",
    "person_id",
    "contact_candidate_id",
    "verification_status",
    "verification_reason",
    "provider",
    "run_id",
    "record_status",
    "provenance_json",
]
UNCERTAIN_FIELDS = [
    "link",
    "business_name",
    "event",
    "date_posted",
    "location",
    "summary",
    "state",
    "source_site",
    "candidate_id",
    "run_id",
    "record_status",
    "validation_errors",
    "review_id",
    "review_stage",
]


class ExportService:
    def __init__(
        self,
        state: StateStore,
        artifacts: ArtifactStore,
        results_dir: str | Path,
        stamp: str,
    ):
        self.state = state
        self.artifacts = artifacts
        self.results_dir = Path(results_dir)
        self.stamp = stamp
        self.day_dir = self.results_dir / stamp

    def export(self) -> dict[str, object]:
        self.day_dir.mkdir(parents=True, exist_ok=True)
        events = self.state.active_events_for_run(self.artifacts.run_id)
        organizations = {
            item.organization_id: item
            for item in self.state.organizations({event.organization_id for event in events})
        }
        candidates = {
            item.candidate_id: item
            for item in self.state.candidates_for_run(self.artifacts.run_id)
        }
        people = self.state.people()
        people_by_org: dict[str, list[Person]] = defaultdict(list)
        for person in people:
            people_by_org[person.organization_id].append(person)
        contacts = self.state.contacts_for_run(self.artifacts.run_id)
        selected_contacts = {
            (item.lead_event_id, item.person_id): item
            for item in contacts
            if item.selected and item.verification_status.value != "rejected"
        }
        scores = {
            item.lead_event_id: item.score
            for item in self.state.scores_for_run(self.artifacts.run_id)
        }
        profiles = self.state.company_profiles_for_run(self.artifacts.run_id)
        outreach_recipients = self.state.outreach_recipients_for_run(
            self.artifacts.run_id
        )
        lead_rows = [
            self._lead_row(
                event,
                organizations[event.organization_id],
                candidates,
                people_by_org[event.organization_id],
                scores.get(event.lead_event_id),
            )
            for event in events
            if event.organization_id in organizations
        ]
        lead_rows.sort(key=lambda row: (-_score(row.get("score")), row["lead_event_id"]))
        contact_rows = []
        for event in events:
            organization = organizations.get(event.organization_id)
            primary = candidates.get(event.primary_candidate_id)
            if not organization:
                continue
            for person in people_by_org[event.organization_id]:
                contact = selected_contacts.get((event.lead_event_id, person.person_id))
                if contact is None:
                    continue
                contact_rows.append(
                    self._contact_row(
                        event,
                        organization,
                        person,
                        contact,
                        primary,
                        scores.get(event.lead_event_id),
                    )
                )
        contact_rows.sort(key=lambda row: (-_score(row.get("score")), row["lead_event_id"], row["person_id"]))
        uncertain_rows = self._uncertain_rows(candidates)

        raw_path = self.day_dir / "raw_leads.csv"
        contacts_path = self.day_dir / "contacts.csv"
        uncertain_path = self.day_dir / "uncertain_leads.csv"
        csvio.write_csv(str(raw_path), lead_rows, LEAD_FIELDS)
        csvio.write_csv(str(contacts_path), contact_rows, CONTACT_FIELDS)
        csvio.write_csv(str(uncertain_path), uncertain_rows, UNCERTAIN_FIELDS)
        artifacts = [
            self.artifacts.record_existing("export", "csv", raw_path),
            self.artifacts.record_existing("export", "csv", contacts_path),
            self.artifacts.record_existing("export", "csv", uncertain_path),
            self.artifacts.write_jsonl("export", "lead_events.jsonl", events),
            self.artifacts.write_jsonl("export", "contacts.jsonl", contacts),
            self.artifacts.write_jsonl("export", "company_profiles.jsonl", profiles),
            self.artifacts.write_jsonl(
                "export", "outreach_recipients.jsonl", outreach_recipients
            ),
            self.artifacts.write_jsonl(
                "export",
                "reviews.jsonl",
                self.state.reviews_for_run(self.artifacts.run_id, state="open"),
            ),
        ]
        handoff = self._sales_handoff(
            events,
            organizations,
            candidates,
            scores,
            profiles,
            outreach_recipients,
        )
        handoff_artifact = self.artifacts.write_json(
            "export", "sales_handoff.json", handoff.model_dump(mode="json")
        )
        artifacts.append(handoff_artifact)
        html_path = Path(build_email.build(self.stamp, str(self.results_dir)))
        artifacts.append(self.artifacts.record_existing("export", "html", html_path))
        return {
            "lead_count": len(lead_rows),
            "contact_count": len(contact_rows),
            "review_count": len(uncertain_rows),
            "paths": {
                "raw_leads": str(raw_path),
                "contacts": str(contacts_path),
                "uncertain_leads": str(uncertain_path),
                "html": str(html_path),
                "sales_handoff": handoff_artifact["path"],
            },
            "artifacts": artifacts,
        }

    def _lead_row(self, event, organization, candidates, people, score):
        primary = candidates.get(event.primary_candidate_id)
        source_urls = [
            candidates[item].canonical_url
            for item in event.supporting_candidate_ids
            if item in candidates
        ]
        decision_sources = list(
            dict.fromkeys(
                evidence.url for person in people for evidence in person.evidence
            )
        )
        return {
            "link": primary.canonical_url if primary else (source_urls[0] if source_urls else ""),
            "business_name": organization.canonical_name,
            "person": people[0].name if people else "",
            "event": event.event,
            "date_posted": str(event.date_posted or ""),
            "location": event.location,
            "summary": event.summary,
            "why_line": event.why_line,
            "why_template_key": event.why_template_key,
            "why_confidence": event.why_confidence,
            "why_line_status": event.why_line_status,
            "why_sources": " ".join(event.why_sources),
            "state": event.state,
            "source_site": _site(primary.canonical_url) if primary else "",
            "aka": ", ".join(organization.aliases),
            "priority": event.priority,
            "property_type": event.property_type,
            "service_angle": event.service_angle,
            "filter_reason": event.filter_reason,
            "Decision_Makers": "; ".join(_person_label(person) for person in people),
            "Employee_Count": _employee_count(organization),
            "Decision_Maker_Sources": " ".join(decision_sources),
            "score": "" if score is None else str(score),
            "lead_event_id": event.lead_event_id,
            "organization_id": event.organization_id,
            "primary_candidate_id": event.primary_candidate_id,
            "supporting_candidate_ids": ",".join(event.supporting_candidate_ids),
            "run_id": event.run_id,
            "record_status": event.record_status.value,
            "provenance_json": json.dumps(
                {"source_urls": source_urls, "evidence": [item.model_dump(mode="json") for item in event.evidence]},
                sort_keys=True,
                default=str,
            ),
        }

    def _contact_row(self, event, organization, person, contact, primary, score):
        evidence = contact.evidence if contact else person.evidence
        return {
            "business_name": organization.canonical_name,
            "state": event.state,
            "location": event.location,
            "event": event.event,
            "date_posted": str(event.date_posted or ""),
            "summary": event.summary,
            "why_line": _personalize_why_line(event.why_line, person.name),
            "why_template_key": event.why_template_key,
            "why_confidence": event.why_confidence,
            "why_line_status": event.why_line_status,
            "why_sources": " ".join(event.why_sources),
            "link": primary.canonical_url if primary else "",
            "employee_count": _employee_count(organization),
            "person": person.name,
            "title": person.title,
            "linkedin": contact.linkedin if contact else "",
            "email": contact.email if contact else "",
            "phone": contact.phone if contact else "",
            "sources": " ".join(item.url for item in evidence),
            "score": "" if score is None else str(score),
            "lead_event_id": event.lead_event_id,
            "organization_id": organization.organization_id,
            "person_id": person.person_id,
            "contact_candidate_id": contact.contact_candidate_id if contact else "",
            "verification_status": contact.verification_status.value if contact else "unknown",
            "verification_reason": contact.verification_reason if contact else "no_contact_candidate",
            "provider": contact.provider if contact else "",
            "run_id": event.run_id,
            "record_status": "valid" if contact else "review",
            "provenance_json": json.dumps(
                [item.model_dump(mode="json") for item in evidence], sort_keys=True, default=str
            ),
        }

    def _sales_handoff(
        self,
        events,
        organizations: dict[str, Organization],
        candidates: dict[str, DiscoveryCandidate],
        scores: dict[str, int],
        profiles: list[CompanyProfile],
        recipients: list[OutreachRecipient],
    ) -> SalesHandoff:
        profile_by_org = {
            organization_id: profile
            for profile in profiles
            for organization_id in profile.organization_ids
        }
        recipients_by_company: dict[str, list[OutreachRecipient]] = defaultdict(list)
        for recipient in recipients:
            recipients_by_company[recipient.company_id].append(recipient)

        company_models = [
            CompanySync(
                company_id=profile.company_id,
                canonical_name=profile.canonical_name,
                domain=profile.domain,
                aliases=profile.aliases,
                legacy_ids=profile.organization_ids,
                relationship_type=profile.relationship_type,
                relationship_confidence=profile.relationship_confidence,
                relationship_sources=profile.relationship_sources,
                outreach_route=profile.outreach_route,
            )
            for profile in profiles
        ]
        event_models: list[LeadEventSync] = []
        for event in events:
            profile = profile_by_org.get(event.organization_id)
            organization = organizations.get(event.organization_id)
            if not profile or not organization:
                continue
            is_anchor = event.lead_event_id == profile.anchor_lead_event_id
            actionable = profile.why_line_status == "valid"
            reasons = []
            if event.record_status != RecordStatus.VALID:
                reasons.append("event_record_not_valid")
            if event.confidence != "high":
                reasons.append("event_confidence_not_high")
            if scores.get(event.lead_event_id, 0) <= 0:
                reasons.append("event_score_zero")
            if not actionable:
                reasons.append(f"company_route_{profile.why_line_status}")
            primary = candidates.get(event.primary_candidate_id)
            event_models.append(
                LeadEventSync(
                    run_id=self.artifacts.run_id,
                    lead_event_id=event.lead_event_id,
                    company_id=profile.company_id,
                    organization_name=profile.canonical_name,
                    event_role=EventRole.ANCHOR if is_anchor else EventRole.SUPPORTING,
                    event=event.event,
                    location=event.location,
                    date_posted=str(event.date_posted or ""),
                    summary=event.summary,
                    article_url=primary.canonical_url if primary else "",
                    score=scores.get(event.lead_event_id, 0),
                    confidence=event.confidence,
                    record_status=event.record_status.value,
                    actionable_route=actionable,
                    supporting_event_ids=[
                        item for item in profile.lead_event_ids if item != event.lead_event_id
                    ],
                    crm_eligible=not reasons,
                    crm_exclusion_reasons=reasons,
                )
            )

        recipient_models = [
            RecipientSync(
                recipient_id=item.recipient_id,
                company_id=item.company_id,
                person_id=item.person_id,
                contact_candidate_id=item.contact_candidate_id,
                full_name=item.full_name,
                first_name=item.first_name,
                title=item.title,
                scope=item.scope,
                email=item.email,
                source_provider=item.source_provider,
                source_verification_status=item.source_verification_status,
                source_verification_reason=item.source_verification_reason,
                role_score=item.role_score,
                rank=item.rank,
                primary=item.primary,
                selection_rationale=item.selection_rationale,
            )
            for item in recipients
        ]

        sequences: list[OutreachSequenceSync] = []
        for profile in profiles:
            primary = next(
                (
                    item
                    for item in recipients_by_company.get(profile.company_id, [])
                    if item.primary
                ),
                None,
            )
            if not primary or not profile.why_line:
                continue
            personalized = personalize_why_line(profile.why_line, primary.first_name)
            merge_snapshot = {
                "firstName": primary.first_name,
                "company": profile.canonical_name,
                "whyLine": personalized,
                "unsubscribeUrl": "__integration_generated__",
            }
            merge_hash = hashlib.sha256(
                json.dumps(
                    merge_snapshot,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            sequences.append(
                OutreachSequenceSync(
                    sequence_id=stable_uuid(
                        "outreach-sequence",
                        profile.company_id,
                        HANDOFF_PROTOCOL_VERSION,
                    ),
                    run_id=self.artifacts.run_id,
                    company_id=profile.company_id,
                    campaign_protocol=HANDOFF_PROTOCOL_VERSION,
                    anchor_lead_event_id=profile.anchor_lead_event_id,
                    supporting_event_ids=[
                        item
                        for item in profile.lead_event_ids
                        if item != profile.anchor_lead_event_id
                    ],
                    primary_recipient_id=primary.recipient_id,
                    why_template_key=profile.why_template_key,
                    why_slots=profile.why_slots,
                    why_sources=profile.why_sources,
                    why_confidence=profile.why_confidence,
                    company_why_line=profile.why_line,
                    personalized_why_line=personalized,
                    merge_snapshot=merge_snapshot,
                    merge_hash=merge_hash,
                    eligibility_status=(
                        EligibilityStatus.READY
                        if primary.eligibility_status == "ready"
                        else EligibilityStatus.BLOCKED
                    ),
                    eligibility_reasons=primary.eligibility_reasons,
                )
            )
        value = SalesHandoff(
            schema_version=HANDOFF_SCHEMA_VERSION,
            protocol_version=HANDOFF_PROTOCOL_VERSION,
            run_id=self.artifacts.run_id,
            companies=company_models,
            lead_events=event_models,
            recipients=recipient_models,
            sequences=sequences,
            content_hash="pending",
        )
        return value.model_copy(update={"content_hash": handoff_content_hash(value)})

    def _uncertain_rows(self, candidates: dict[str, DiscoveryCandidate]) -> list[dict]:
        rows = []
        for review in self.state.reviews_for_run(self.artifacts.run_id, state="open"):
            candidate = candidates.get(review.record_id)
            if not candidate:
                continue
            rows.append(
                {
                    "link": candidate.canonical_url,
                    "business_name": candidate.title,
                    "event": "",
                    "date_posted": str(candidate.published_at.date()) if candidate.published_at else "",
                    "location": "",
                    "summary": "",
                    "state": "Arizona",
                    "source_site": _site(candidate.canonical_url),
                    "candidate_id": candidate.candidate_id,
                    "run_id": candidate.run_id,
                    "record_status": candidate.record_status.value,
                    "validation_errors": " | ".join(review.validation_errors),
                    "review_id": review.review_id,
                    "review_stage": review.stage,
                }
            )
        return rows


def _site(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def _person_label(person: Person) -> str:
    title = f" — {person.title}" if person.title else ""
    scope = f" ({person.scope})" if person.scope else ""
    return f"{person.name}{title}{scope}"


def _employee_count(organization: Organization) -> str:
    value = organization.employee_count or {}
    count = str(value.get("value") or "").strip()
    detail = ", ".join(
        str(value.get(key) or "").strip()
        for key in ("scope", "as_of")
        if str(value.get(key) or "").strip()
    )
    return f"{count} ({detail})" if count and detail else count


def _score(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _personalize_why_line(line: str, person_name: str) -> str:
    if not line:
        return ""
    parts = person_name.split()
    while parts and parts[0].casefold().rstrip(".") in {
        "dr",
        "mr",
        "mrs",
        "ms",
        "prof",
    }:
        parts.pop(0)
    if not parts or not line.startswith("Hi [first name]"):
        return ""
    return f"Hi {parts[0]}{line[len('Hi [first name]') :]}"
