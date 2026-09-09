"""Company-level why-line enrichment and deterministic recipient selection."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable

from outreach_contract import (
    WHY_LINE_PROTOCOL_VERSION,
    anchor_event,
    first_name,
    normalized_domain,
    parse_why_line_selection,
    template_catalog,
)
from recipient_policy import assess_recipient, candidate_rank_key, current_employer_match

from .artifacts import ArtifactStore
from .contracts import (
    CompanyProfile,
    ContactCandidate,
    LeadEvent,
    LeadScore,
    Organization,
    OutreachRecipient,
    Person,
    RecordStatus,
    ReviewItem,
    VerificationStatus,
)
from .ids import normalize_text, stable_uuid
from .state import StateStore
from .verification import is_organization_mismatch


ModelCall = Callable[[str, str, list[dict]], tuple[str, dict]]
ROLE_AUTO_SEND_THRESHOLD = 70

COMPANY_PROMPT = """Select one approved Aether cold-email opening template for one company and return only sourced insertion values.

Profile key: {profile_key}
Company name: {company_name}
Known official domain: {domain}
Known aliases: {names}
Known locations: {locations}
Deterministic anchor event ID: {anchor_id}
Events: {events}

Use web search to verify the canonical company, official domain, and the strongest specific property event. Distinguish the event's operator/owner from its developer, general contractor, broker/leasing representative, and project name. Do not silently relabel one as another. The selected lead_event_id must be supplied. Relationship uncertainty is a quality signal, not a reason to discard the opportunity: preserve it as relationship_type unknown and outreach_route review. For acquisitions, use acquisition only when the company is the buyer/new owner. Route sellers, brokers, listings, auctions, and unclear ownership to route_new_owner. Route closures, bankruptcies, lawsuits, stalled or abandoned projects to skip_negative unless a reopening or reuse is verified. Route general market or portfolio signals without a property trigger to skip_general.

Return insertion slots only; never write the final sentence. Company/project references are at most three words. Location is one leaf locality or neighborhood, at most three words, without state, county, region, parent city, road detail, comma, or second city. Cite supplied articles, official company/government pages, or credible business evidence.

Approved templates and routes:
{templates}

Return strict JSON only as {{"canonical_name":"","domain":"","employee_count":"","relationship_type":"operator|owner|property_manager|developer|contractor|broker|tenant|project|unknown","relationship_confidence":"high|medium|low","relationship_sources":[],"outreach_route":"direct_facility|construction_closeout|referral|review","selection":{{"template_key":"","lead_event_id":"","slots":{{}},"confidence":"high|medium|low","source_urls":[]}}}}."""


class CompanyOutreachService:
    def __init__(
        self,
        state: StateStore,
        artifacts: ArtifactStore,
        model: str,
        *,
        call_model: ModelCall | None = None,
    ):
        self.state = state
        self.artifacts = artifacts
        self.model = model
        self.call_model = call_model or _default_model_call

    def build(
        self,
        events: list[LeadEvent],
        organizations: list[Organization],
        people: list[Person],
        contacts: list[ContactCandidate],
        scores: list[LeadScore],
    ) -> tuple[list[CompanyProfile], list[OutreachRecipient], list[ReviewItem]]:
        organizations_by_id = {item.organization_id: item for item in organizations}
        scores_by_event = {item.lead_event_id: item.score for item in scores}
        groups: dict[str, list[LeadEvent]] = defaultdict(list)
        for event in events:
            organization = organizations_by_id.get(event.organization_id)
            if not organization:
                continue
            identity = organization.domain or normalize_text(organization.canonical_name)
            groups[identity].append(event)

        profiles: list[CompanyProfile] = []
        reviews: list[ReviewItem] = []
        for identity, group_events in sorted(groups.items()):
            profile, review = self._build_company(
                identity, group_events, organizations_by_id, scores_by_event
            )
            profiles.append(profile)
            self.state.save_company_profile(profile)
            if review:
                self.state.add_review(review)
                reviews.append(review)
            for event in group_events:
                self.state.save_lead_event(
                    event.model_copy(
                        update={
                            "why_line": profile.why_line,
                            "why_template_key": profile.why_template_key,
                            "why_slots": profile.why_slots,
                            "why_confidence": profile.why_confidence,
                            "why_line_status": profile.why_line_status,
                            "why_sources": profile.why_sources,
                        }
                    )
                )

        recipients = self._rank_recipients(
            profiles, people, contacts, events, scores_by_event
        )
        for recipient in recipients:
            self.state.save_outreach_recipient(recipient)
        self.state.prune_company_outreach(
            self.artifacts.run_id,
            {profile.company_id for profile in profiles},
            {recipient.recipient_id for recipient in recipients},
        )
        return profiles, recipients, reviews

    def _build_company(
        self,
        identity: str,
        events: list[LeadEvent],
        organizations: dict[str, Organization],
        scores: dict[str, int],
    ) -> tuple[CompanyProfile, ReviewItem | None]:
        anchor = anchor_event(events, scores)
        orgs = [
            organizations[item]
            for item in sorted({event.organization_id for event in events})
            if item in organizations
        ]
        known_names = list(
            dict.fromkeys(
                value
                for org in orgs
                for value in [org.canonical_name, *org.aliases]
                if value
            )
        )
        known_domain = next((org.domain for org in orgs if org.domain), "")
        company_id = self.state.resolve_company_profile_identity(
            known_domain, known_names
        ) or stable_uuid("company", identity)
        event_payload = [
            {
                "lead_event_id": event.lead_event_id,
                "event": event.event,
                "date": str(event.date_posted or ""),
                "location": event.location,
                "summary": event.summary,
                "priority": event.priority,
                "score": scores.get(event.lead_event_id),
                "sources": [item.url for item in event.evidence],
            }
            for event in events
        ]
        prompt = COMPANY_PROMPT.format(
            profile_key=company_id,
            company_name=known_names[0] if known_names else identity,
            domain=known_domain,
            names=json.dumps(known_names),
            locations=json.dumps(
                list(dict.fromkeys([*(org.location for org in orgs), *(e.location for e in events)]))
            ),
            anchor_id=anchor.lead_event_id,
            events=json.dumps(event_payload, sort_keys=True),
            templates=template_catalog(),
        )
        attempt_number = self.state.next_provider_attempt_number(
            self.artifacts.run_id,
            "company-outreach",
            "company",
            company_id,
        )
        attempt_id = stable_uuid(
            "attempt",
            self.artifacts.run_id,
            "company-outreach",
            company_id,
            attempt_number,
        )
        request = self.artifacts.write_raw(
            "company-outreach",
            f"{attempt_id}-request.json",
            {"model": self.model, "prompt": prompt, "protocol": WHY_LINE_PROTOCOL_VERSION},
        )
        started = datetime.now(timezone.utc).isoformat()
        try:
            text, usage = self.call_model(self.model, prompt, [{"type": "web_search"}])
            response = self.artifacts.write_raw_text(
                "company-outreach", f"{attempt_id}-response.txt", text
            )
            payload = _parse_object(text)
            selection = parse_why_line_selection(
                payload,
                allowed_event_ids={event.lead_event_id for event in events},
                known_company_names=known_names,
            )
            canonical_name = str(payload.get("canonical_name") or known_names[0]).strip()
            domain = normalized_domain(str(payload.get("domain") or ""))
            company_id = self.state.resolve_company_profile_identity(
                domain, [*known_names, canonical_name]
            ) or company_id
            errors = list(selection.validation_errors)
            record_status = RecordStatus.VALID if not errors else RecordStatus.REVIEW
            relationship_type = _normalized_choice(
                payload.get("relationship_type"),
                {
                    "operator",
                    "owner",
                    "property_manager",
                    "developer",
                    "contractor",
                    "broker",
                    "tenant",
                    "project",
                    "unknown",
                },
                "unknown",
            )
            relationship_confidence = _normalized_choice(
                payload.get("relationship_confidence"),
                {"high", "medium", "low"},
                "low",
            )
            outreach_route = _normalized_choice(
                payload.get("outreach_route"),
                {"direct_facility", "construction_closeout", "referral", "review"},
                "review",
            )
            relationship_sources = [
                str(value).strip()
                for value in payload.get("relationship_sources", [])
                if isinstance(value, str) and str(value).strip()
            ]
            profile = CompanyProfile(
                company_id=company_id,
                run_id=self.artifacts.run_id,
                canonical_name=canonical_name,
                domain=domain,
                aliases=list(dict.fromkeys([*known_names, canonical_name])),
                locations=list(
                    dict.fromkeys(
                        value
                        for value in [*(org.location for org in orgs), *(e.location for e in events)]
                        if value
                    )
                ),
                organization_ids=sorted(event.organization_id for event in events),
                lead_event_ids=sorted(event.lead_event_id for event in events),
                anchor_lead_event_id=anchor.lead_event_id,
                relationship_type=relationship_type,
                relationship_confidence=relationship_confidence,
                relationship_sources=list(dict.fromkeys(relationship_sources)),
                outreach_route=outreach_route,
                why_line=selection.text,
                why_template_key=selection.template_key,
                why_slots=selection.slots,
                why_confidence=selection.confidence,
                why_sources=selection.source_urls,
                why_line_status=selection.status,
                record_status=record_status,
                validation_errors=errors,
            )
            self.state.record_provider_attempt(
                attempt_id=attempt_id,
                run_id=self.artifacts.run_id,
                stage="company-outreach",
                provider="model",
                target_type="company",
                target_id=company_id,
                status="completed" if not errors else "review",
                token_usage=usage,
                request_artifact_path=request["path"],
                response_artifact_path=response["path"],
                error={"validation_errors": errors} if errors else None,
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as exc:
            error = f"company_contract:{type(exc).__name__}:{exc}"
            profile = CompanyProfile(
                company_id=company_id,
                run_id=self.artifacts.run_id,
                canonical_name=known_names[0] if known_names else identity,
                aliases=known_names,
                locations=[value for value in (org.location for org in orgs) if value],
                organization_ids=sorted(event.organization_id for event in events),
                lead_event_ids=sorted(event.lead_event_id for event in events),
                anchor_lead_event_id=anchor.lead_event_id,
                validation_errors=[error],
            )
            self.state.record_provider_attempt(
                attempt_id=attempt_id,
                run_id=self.artifacts.run_id,
                stage="company-outreach",
                provider="model",
                target_type="company",
                target_id=company_id,
                status="review",
                request_artifact_path=request["path"],
                error={"type": type(exc).__name__, "message": str(exc)},
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        if profile.record_status == RecordStatus.VALID:
            return profile, None
        review = ReviewItem(
            review_id=stable_uuid(
                "review", self.artifacts.run_id, "company-outreach", company_id
            ),
            run_id=self.artifacts.run_id,
            stage="company-outreach",
            record_type="company_profile",
            record_id=company_id,
            reason_code="company_outreach_invalid",
            validation_errors=profile.validation_errors or ["company_outreach_invalid"],
        )
        return profile, review

    def _rank_recipients(
        self,
        profiles: list[CompanyProfile],
        people: list[Person],
        contacts: list[ContactCandidate],
        events: list[LeadEvent],
        scores: dict[str, int],
    ) -> list[OutreachRecipient]:
        people_by_id = {item.person_id: item for item in people}
        profile_by_org = {
            organization_id: profile
            for profile in profiles
            for organization_id in profile.organization_ids
        }
        events_by_id = {item.lead_event_id: item for item in events}
        open_review_ids = {
            item.record_id
            for item in self.state.reviews_for_run(self.artifacts.run_id)
            if item.state == "open"
        }
        candidates: dict[tuple[str, str], tuple[ContactCandidate, Person]] = {}
        for contact in contacts:
            if not contact.selected or not contact.email:
                continue
            if not _eligible_for_authoritative_verification(contact):
                continue
            person = people_by_id.get(contact.person_id)
            profile = profile_by_org.get(contact.organization_id)
            if not person or not profile:
                continue
            key = (profile.company_id, person.person_id)
            prior = candidates.get(key)
            if prior is None or _contact_preference(contact) > _contact_preference(prior[0]):
                candidates[key] = (contact, person)

        by_company: dict[str, list[tuple[int, list[str], ContactCandidate, Person]]] = defaultdict(list)
        for (company_id, _), (contact, person) in candidates.items():
            role_score, rationale = score_recipient_role(person.title, person.scope)
            if contact.provider.casefold() != "apollo":
                role_score += 2
                rationale = [*rationale, "non_apollo_source_contact_candidate"]
            by_company[company_id].append((role_score, rationale, contact, person))

        output: list[OutreachRecipient] = []
        profiles_by_id = {item.company_id: item for item in profiles}
        for company_id, rows in sorted(by_company.items()):
            profile = profiles_by_id[company_id]
            rows.sort(
                key=lambda row: candidate_rank_key(
                    current_employer=current_employer_match(row[2].email, profile.domain),
                    role_score=row[0],
                    local_scope=_local_scope(row[3].scope),
                    non_fallback_provider=row[2].provider.casefold() != "apollo",
                    verification_status=row[2].verification_status.value,
                    evidence_count=len(row[2].evidence),
                    stable_id=row[3].person_id,
                )
            )
            anchor = events_by_id[profile.anchor_lead_event_id]
            sole_email = len({row[2].email.strip().casefold() for row in rows}) == 1
            for index, (role_score, rationale, contact, person) in enumerate(rows, start=1):
                assessment = assess_recipient(
                    verification_status=contact.verification_status.value,
                    verification_reason=contact.verification_reason,
                    primary=index == 1,
                    role_score=role_score,
                    alternative_email_count=1 if sole_email else len(rows),
                )
                reasons: list[str] = [
                    *assessment.hard_failures,
                    *assessment.selection_blocks,
                ]
                rationale = [*rationale, *assessment.quality_signals]
                rationale.append(f"company_relationship_{profile.relationship_type}")
                rationale.append(f"outreach_route_{profile.outreach_route}")
                if profile.relationship_confidence == "low":
                    rationale.append("company_relationship_confidence_low")
                if profile.record_status != RecordStatus.VALID:
                    reasons.append("company_profile_not_valid")
                if profile.why_line_status != "valid":
                    reasons.append(f"why_line_status_{profile.why_line_status}")
                if profile.why_confidence not in {"high", "medium"}:
                    reasons.append("why_line_confidence_low")
                if anchor.record_status != RecordStatus.VALID:
                    reasons.append("anchor_record_not_valid")
                if anchor.confidence != "high":
                    reasons.append("anchor_confidence_not_high")
                if scores.get(anchor.lead_event_id, 0) <= 0:
                    reasons.append("anchor_score_zero")
                if {profile.company_id, anchor.lead_event_id, person.person_id, contact.contact_candidate_id} & open_review_ids:
                    reasons.append("blocking_open_review")
                name = 'team' if person.scope.startswith('company_mailbox:') else first_name(person.name)
                if not name:
                    reasons.append("recipient_first_name_missing")
                output.append(
                    OutreachRecipient(
                        recipient_id=stable_uuid("recipient", company_id, person.person_id),
                        run_id=self.artifacts.run_id,
                        company_id=company_id,
                        person_id=person.person_id,
                        contact_candidate_id=contact.contact_candidate_id,
                        full_name=person.name,
                        first_name=name or "unknown",
                        title=person.title,
                        scope=person.scope,
                        email=contact.email,
                        source_provider=contact.provider,
                        source_verification_status=contact.verification_status.value,
                        source_verification_reason=contact.verification_reason,
                        role_score=role_score,
                        rank=index,
                        primary=index == 1,
                        selection_rationale=rationale,
                        eligibility_status="ready" if not reasons else "blocked",
                        eligibility_reasons=reasons,
                    )
                )
        return output


def _eligible_for_authoritative_verification(contact: ContactCandidate) -> bool:
    """Allow a sourced address to reach Warmy's mailbox-level verification.

    MX presence is intentionally not called mailbox verification. It is only a
    bounded precheck that permits the integration layer to request the
    authoritative Warmy result before approval eligibility is granted.
    """
    return contact.verification_status in {
        VerificationStatus.VERIFIED,
        VerificationStatus.UNKNOWN,
    }


def score_recipient_role(title: str, scope: str) -> tuple[int, list[str]]:
    text = normalize_text(f"{title} {scope}")
    rationale: list[str] = []
    if re.search(r"facilit|property management", text):
        score = 100
        rationale.append("facilities_or_property_management")
    elif re.search(r"operations|real property|branch manager|general manager|site manager|regional manager", text):
        score = 90
        rationale.append("operations_or_site_management")
    elif re.search(r"owner|partner|president|chief executive|ceo|chief operating|coo|managing member", text):
        score = 70
        rationale.append("company_leadership")
    elif re.search(r"real estate|leasing", text):
        score = 70
        rationale.append("real_estate_or_leasing")
    elif re.search(r"development|acquisition", text):
        score = 50
        rationale.append("development_or_acquisition")
    else:
        score = 0
        rationale.append("role_not_facilities_relevant")
    if re.search(r"facilit|property|operations", normalize_text(scope)):
        score += 10
        rationale.append("scope_facilities_property_operations")
    if _local_scope(scope):
        score += 5
        rationale.append("scope_arizona_or_local")
    return score, rationale


def _local_scope(scope: str) -> bool:
    text = normalize_text(scope)
    return any(value in text for value in ("arizona", "phoenix", "local", "regional"))


def _contact_preference(contact: ContactCandidate) -> tuple[int, int, int, str]:
    return (
        int(not is_organization_mismatch(contact)),
        int(contact.provider.casefold() != "apollo"),
        len(contact.evidence),
        contact.contact_candidate_id,
    )


def _parse_object(text: str) -> dict:
    cleaned = re.sub(r"<<ccr:[^>]+>>", "", str(text or ""))
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        raise ValueError("company response did not contain a JSON object")
    value = json.loads(match.group())
    if not isinstance(value, dict):
        raise ValueError("company response must be an object")
    return value


def _normalized_choice(value: object, allowed: set[str], default: str) -> str:
    normalized = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    return normalized if normalized in allowed else default


def _default_model_call(model: str, prompt: str, tools: list[dict]) -> tuple[str, dict]:
    import llm

    return llm.call(model, prompt, tools=tools, text_format="json_object", with_usage=True)
