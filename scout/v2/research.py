"""Stable-identity decision-maker and contact research services."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from outreach_contract import normalized_domain

from .artifacts import ArtifactStore
from .contracts import (
    ContactCandidate,
    Evidence,
    LeadEvent,
    Organization,
    Person,
    ReviewItem,
    VerificationStatus,
)
from .ids import normalize_text, person_id, stable_hash, stable_uuid
from .state import StateStore
from .verification import ContactVerifier, select_best


ModelCall = Callable[[str, str, list[dict]], tuple[str, dict]]


class EvidencePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    url: str
    supports: str = "Supports the research result."


class DecisionMakerPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    decision_makers: list[dict] = Field(default_factory=list)
    employee_count: dict | None = None
    canonical_domain: str = ""
    aliases: list[str] = Field(default_factory=list)
    sources: list[EvidencePayload] = Field(default_factory=list)


class ContactPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""
    organization: str = ""
    linkedin: str = ""
    email: str = ""
    phone: str = ""
    sources: list[EvidencePayload | str] = Field(default_factory=list)


DECISION_PROMPT = """Build one bounded web-research dossier for up to three current decision makers for this Arizona commercial-property organization.
Organization ID: {organization_id}
Organization: {name}
Known aliases: {aliases}
Location: {location}
Date: {today}

Use one research path. First verify the exact current organization and its official domain; guard against namesakes, project names, landlords, brokers, affiliates, and former employers. Prefer local or regional authority over facilities, property/asset management, development, leasing, ownership, or operations. HR, recruiting, privacy, safety, and unrelated-region roles are lower-fit signals, not automatic exclusions: retain a sourced reachable person when no stronger contact is found. For each verified person, also return any public professional LinkedIn, email, and phone details found during the same research. Never guess. Each person's sources must support the current role and any returned contact fields. Return strict JSON:
{{"canonical_domain":"","aliases":[],"decision_makers":[{{"name":"","title":"","scope":"","linkedin":"","email":"","phone":"","sources":[{{"url":"","supports":""}}]}}],"employee_count":{{"value":"","scope":"company|location","as_of":"","confidence":"high|medium|low"}},"sources":[{{"url":"","supports":""}}]}}
Use an empty decision_makers list and null employee_count when nothing is verified."""


CONTACT_PROMPT = """Use web search to research this exact person and organization.
Person ID: {person_id}
Name: {name}
Organization: {organization}
Location: {location}

Find sourced professional LinkedIn, email, and phone details. Verify the exact person, current employer, employer domain, relevant division/geography, and relationship to the supplied organization; guard against a namesake or a former-employer address. Prefer a current-employer email. If the only sourced, non-invalid email is on another domain, return it rather than hiding it—the verifier will label it as a fallback and downstream research will keep looking for a better address. Never guess. Return strict JSON:
{{"name":"{name}","organization":"{organization}","linkedin":"","email":"","phone":"","sources":[{{"url":"","supports":""}}]}}
Use empty strings when a field cannot be verified."""


class DecisionMakerService:
    def __init__(
        self,
        state: StateStore,
        artifacts: ArtifactStore,
        model: str,
        call_model: ModelCall | None = None,
        verifier: ContactVerifier | None = None,
        events: list[LeadEvent] | None = None,
    ):
        self.state = state
        self.artifacts = artifacts
        self.model = model
        self.call_model = call_model or _default_model_call
        self.verifier = verifier
        self.events_by_org: dict[str, list[LeadEvent]] = {}
        for event in events or []:
            self.events_by_org.setdefault(event.organization_id, []).append(event)

    def research(self, organizations: list[Organization], attempts: int = 2) -> tuple[list[Person], list[ReviewItem]]:
        organization_ids = {item.organization_id for item in organizations}
        people: dict[str, Person] = {
            person.person_id: person
            for person in self.state.people()
            if person.organization_id in organization_ids
        }
        reviews: list[ReviewItem] = []
        for organization in organizations:
            if any(
                person.organization_id == organization.organization_id
                and not person.inferred_identity
                and bool(person.title)
                for person in people.values()
            ):
                continue
            last_review = None
            for attempt in range(1, attempts + 1):
                found, last_review = self._research_one(organization, attempt)
                if last_review is None:
                    people.update({person.person_id: person for person in found})
                    break
            if last_review:
                reviews.append(last_review)
        return list(people.values()), reviews

    def _research_one(self, organization: Organization, attempt_number: int) -> tuple[list[Person], ReviewItem | None]:
        attempt_number = self.state.next_provider_attempt_number(
            self.artifacts.run_id,
            "decision-makers",
            "organization",
            organization.organization_id,
        )
        attempt_id = stable_uuid(
            "attempt",
            self.artifacts.run_id,
            organization.organization_id,
            "decision-maker",
            attempt_number,
        )
        prompt = DECISION_PROMPT.format(
            organization_id=organization.organization_id,
            name=organization.canonical_name,
            aliases=json.dumps(organization.aliases),
            location=organization.location,
            today=date.today().isoformat(),
        )
        request = self.artifacts.write_raw(
            "decision-makers", f"{attempt_id}-request.json", {"model": self.model, "prompt": prompt}
        )
        started = datetime.now(timezone.utc).isoformat()
        try:
            text, usage = self.call_model(self.model, prompt, [{"type": "web_search"}])
            response = self.artifacts.write_raw_text(
                "decision-makers", f"{attempt_id}-response.txt", text
            )
            payload = DecisionMakerPayload.model_validate(_parse_json(text))
            evidence = _evidence(payload.sources, "Supports the decision-maker research result.")
            domain = normalized_domain(payload.canonical_domain)
            self.state.save_organization(
                organization.model_copy(
                    update={
                        "domain": domain or organization.domain,
                        "aliases": list(
                            dict.fromkeys([*organization.aliases, *payload.aliases])
                        ),
                        "employee_count": payload.employee_count
                        if payload.employee_count is not None
                        else organization.employee_count,
                    }
                )
            )
            people: list[Person] = []
            dossier_contacts: list[ContactCandidate] = []
            for raw in payload.decision_makers[:3]:
                name = str(raw.get("name") or "").strip()
                nested_payload = ContactPayload.model_validate(
                    {
                        "name": name,
                        "organization": organization.canonical_name,
                        "linkedin": raw.get("linkedin") or "",
                        "email": raw.get("email") or "",
                        "phone": raw.get("phone") or "",
                        "sources": raw.get("sources") or [],
                    }
                )
                nested_evidence = _evidence(
                    nested_payload.sources,
                    f"Supports the current role and contact details for {name}.",
                )
                person_evidence = nested_evidence or evidence
                if not name or not person_evidence:
                    continue
                person = Person(
                    person_id=person_id(name, organization.organization_id),
                    organization_id=organization.organization_id,
                    name=name,
                    title=str(raw.get("title") or "").strip(),
                    scope=str(raw.get("scope") or "").strip(),
                    evidence=person_evidence,
                )
                self.state.save_person(person)
                people.append(person)
                if not self.verifier or not nested_evidence or not any(
                    (nested_payload.email, nested_payload.phone, nested_payload.linkedin)
                ):
                    continue
                verification = self.verifier.verify(
                    email=nested_payload.email,
                    phone=nested_payload.phone,
                    linkedin=nested_payload.linkedin,
                    organization_domain=domain or organization.domain,
                )
                for event in self.events_by_org.get(organization.organization_id, []):
                    dossier_contacts.append(
                        ContactCandidate(
                            contact_candidate_id=stable_uuid(
                                "contact-candidate",
                                event.lead_event_id,
                                person.person_id,
                                "model-dossier",
                                stable_hash(
                                    nested_payload.email,
                                    nested_payload.phone,
                                    nested_payload.linkedin,
                                ),
                            ),
                            run_id=event.run_id,
                            lead_event_id=event.lead_event_id,
                            organization_id=organization.organization_id,
                            person_id=person.person_id,
                            person_name=person.name,
                            title=person.title,
                            email=verification.email,
                            phone=verification.phone,
                            linkedin=verification.linkedin,
                            provider="model-dossier",
                            verification_status=verification.status,
                            verification_reason=verification.reason,
                            evidence=nested_evidence,
                        )
                    )
            for contact in select_best(dossier_contacts):
                self.state.save_contact(contact)
            self.state.record_provider_attempt(
                attempt_id=attempt_id,
                run_id=self.artifacts.run_id,
                stage="decision-makers",
                provider="model",
                target_type="organization",
                target_id=organization.organization_id,
                status="completed",
                token_usage=usage,
                request_artifact_path=request["path"],
                response_artifact_path=response["path"],
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            return people, None
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
            review = ReviewItem(
                review_id=stable_uuid(
                    "review", self.artifacts.run_id, "decision-makers", organization.organization_id
                ),
                run_id=self.artifacts.run_id,
                stage="decision-makers",
                record_type="organization",
                record_id=organization.organization_id,
                reason_code="model_contract_invalid",
                validation_errors=[f"{type(exc).__name__}:{exc}"],
                raw_artifact_path=locals().get("response", {}).get("path", ""),
                retry_count=attempt_number,
            )
            self.state.add_review(review)
            self.state.record_provider_attempt(
                attempt_id=attempt_id,
                run_id=self.artifacts.run_id,
                stage="decision-makers",
                provider="model",
                target_type="organization",
                target_id=organization.organization_id,
                status="review",
                request_artifact_path=request["path"],
                response_artifact_path=review.raw_artifact_path,
                error={"type": type(exc).__name__, "message": str(exc)},
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            return [], review


class ContactResearchService:
    def __init__(
        self,
        state: StateStore,
        artifacts: ArtifactStore,
        model: str,
        verifier: ContactVerifier,
        call_model: ModelCall | None = None,
    ):
        self.state = state
        self.artifacts = artifacts
        self.model = model
        self.verifier = verifier
        self.call_model = call_model or _default_model_call

    def research(
        self,
        people: list[Person],
        organizations: list[Organization],
        events: list[LeadEvent],
        attempts: int = 2,
    ) -> tuple[list[ContactCandidate], list[ReviewItem]]:
        org_by_id = {item.organization_id: item for item in organizations}
        events_by_org: dict[str, list[LeadEvent]] = {}
        for event in events:
            events_by_org.setdefault(event.organization_id, []).append(event)
        active_event_ids = {event.lead_event_id for event in events}
        person_ids = {person.person_id for person in people}
        all_run_contacts = self.state.contacts_for_run(self.artifacts.run_id)
        candidates = [
            contact
            for contact in all_run_contacts
            if contact.lead_event_id in active_event_ids
            and contact.person_id in person_ids
        ]
        covered = {
            (contact.lead_event_id, contact.person_id)
            for contact in candidates
            if contact.selected
            and contact.email
            and contact.verification_status != VerificationStatus.REJECTED
        }
        reviews: list[ReviewItem] = []
        completed_people = self.state.completed_provider_target_ids(
            self.artifacts.run_id, "contacts", "person"
        )
        for person in people:
            organization = org_by_id.get(person.organization_id)
            if not organization:
                continue
            if person.person_id in completed_people and not any(
                c.person_id == person.person_id and c.email
                and c.verification_status != VerificationStatus.REJECTED for c in all_run_contacts
            ):
                # A completed no-email search in this run is still a completed
                # Grok attempt. A resume proceeds to Treg without repeating it.
                continue
            person_events = events_by_org.get(person.organization_id, [])
            if person_events and all(
                (event.lead_event_id, person.person_id) in covered
                for event in person_events
            ):
                continue
            payload = None
            last_review = None
            for attempt in range(1, attempts + 1):
                payload, last_review = self._research_one(person, organization, attempt)
                if payload is not None:
                    break
            if last_review:
                reviews.append(last_review)
            if payload is None:
                continue
            evidence = _evidence(payload.sources, f"Supports contact details for {person.name}.")
            if not evidence or not any((payload.email, payload.phone, payload.linkedin)):
                continue
            verification = self.verifier.verify(
                email=payload.email,
                phone=payload.phone,
                linkedin=payload.linkedin,
                organization_domain=organization.domain,
            )
            for event in person_events:
                contact = ContactCandidate(
                    contact_candidate_id=stable_uuid(
                        "contact-candidate",
                        event.lead_event_id,
                        person.person_id,
                        "model",
                        stable_hash(payload.email, payload.phone, payload.linkedin),
                    ),
                    run_id=event.run_id,
                    lead_event_id=event.lead_event_id,
                    organization_id=person.organization_id,
                    person_id=person.person_id,
                    person_name=person.name,
                    title=person.title,
                    email=verification.email,
                    phone=verification.phone,
                    linkedin=verification.linkedin,
                    provider="model",
                    verification_status=verification.status,
                    verification_reason=verification.reason,
                    evidence=evidence,
                )
                candidates.append(contact)
        selected = select_best(candidates)
        for contact in selected:
            self.state.save_contact(contact)
        return selected, reviews

    def _research_one(
        self, person: Person, organization: Organization, attempt_number: int
    ) -> tuple[ContactPayload | None, ReviewItem | None]:
        attempt_number = self.state.next_provider_attempt_number(
            self.artifacts.run_id, "contacts", "person", person.person_id
        )
        attempt_id = stable_uuid(
            "attempt",
            self.artifacts.run_id,
            person.person_id,
            "contact",
            attempt_number,
        )
        prompt = CONTACT_PROMPT.format(
            person_id=person.person_id,
            name=person.name,
            organization=organization.canonical_name,
            location=organization.location,
        )
        request = self.artifacts.write_raw(
            "contacts", f"{attempt_id}-request.json", {"model": self.model, "prompt": prompt}
        )
        started = datetime.now(timezone.utc).isoformat()
        try:
            text, usage = self.call_model(self.model, prompt, [{"type": "web_search"}])
            response = self.artifacts.write_raw_text("contacts", f"{attempt_id}-response.txt", text)
            payload = ContactPayload.model_validate(_parse_json(text))
            if payload.name and normalize_text(payload.name) != normalize_text(person.name):
                raise ValueError("returned contact name does not match target person")
            if payload.organization:
                expected = normalize_text(organization.canonical_name)
                returned = normalize_text(payload.organization)
                if expected not in returned and returned not in expected:
                    raise ValueError("returned contact organization does not match target")
            self.state.record_provider_attempt(
                attempt_id=attempt_id,
                run_id=self.artifacts.run_id,
                stage="contacts",
                provider="model",
                target_type="person",
                target_id=person.person_id,
                status="completed",
                token_usage=usage,
                request_artifact_path=request["path"],
                response_artifact_path=response["path"],
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            return payload, None
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
            review = ReviewItem(
                review_id=stable_uuid("review", self.artifacts.run_id, "contacts", person.person_id),
                run_id=self.artifacts.run_id,
                stage="contacts",
                record_type="person",
                record_id=person.person_id,
                reason_code="model_contract_invalid",
                validation_errors=[f"{type(exc).__name__}:{exc}"],
                raw_artifact_path=locals().get("response", {}).get("path", ""),
                retry_count=attempt_number,
            )
            self.state.add_review(review)
            self.state.record_provider_attempt(
                attempt_id=attempt_id,
                run_id=self.artifacts.run_id,
                stage="contacts",
                provider="model",
                target_type="person",
                target_id=person.person_id,
                status="review",
                request_artifact_path=request["path"],
                response_artifact_path=review.raw_artifact_path,
                error={"type": type(exc).__name__, "message": str(exc)},
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            return None, review


def _evidence(values: list[EvidencePayload | str], default_supports: str) -> list[Evidence]:
    out: list[Evidence] = []
    for value in values:
        if isinstance(value, str):
            url, supports = value, default_supports
        else:
            url, supports = value.url, value.supports or default_supports
        try:
            out.append(Evidence(url=url, supports=supports, provider="web"))
        except ValidationError:
            continue
    return out


def _parse_json(text: str) -> object:
    cleaned = re.sub(r"<<ccr:[^>]+>>", "", str(text or ""))
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        raise ValueError("model response did not contain a JSON object")
    return json.loads(match.group())


def _default_model_call(model: str, prompt: str, tools: list[dict]) -> tuple[str, dict]:
    import llm

    return llm.call(
        model,
        prompt,
        tools=tools,
        text_format="json_object",
        with_usage=True,
    )
