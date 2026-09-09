"""Stable-person research and deterministic contact verification."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scout"))

from v2.artifacts import ArtifactStore  # noqa: E402
from v2.contracts import (  # noqa: E402
    ContactCandidate,
    Evidence,
    LeadEvent,
    Organization,
    Person,
    VerificationStatus,
)
from v2.research import ContactResearchService, DecisionMakerService  # noqa: E402
from v2.state import StateStore  # noqa: E402
from v2.verification import ContactVerifier, select_best  # noqa: E402


def setup(tmp_path):
    store = StateStore(tmp_path / "scout.db")
    store.migrate()
    store.create_run("run-1", "2026-08-28", "2026-08-27")
    artifacts = ArtifactStore(tmp_path / "results", "2026-08-28", "run-1", store)
    evidence = [Evidence(url="https://example.com/story", supports="Source event")]
    organization = Organization(
        organization_id="org-1",
        canonical_name="Acme Marketplace",
        domain="acme.com",
        location="Phoenix, Arizona",
        evidence=evidence,
    )
    store.save_organization(organization)
    return store, artifacts, organization, evidence


def test_verifier_rejects_disposable_and_caches_mx(tmp_path):
    store, _, _, _ = setup(tmp_path)
    calls = []
    verifier = ContactVerifier(store, mx_lookup=lambda domain: calls.append(domain) or True)

    rejected = verifier.verify(email="person@mailinator.com")
    assert rejected.status == VerificationStatus.REJECTED
    assert rejected.reason == "email_domain_disposable"
    first = verifier.verify(email="Person@Acme.com", organization_domain="acme.com")
    second = verifier.verify(email="person@acme.com", organization_domain="acme.com")
    assert first.status == second.status == VerificationStatus.UNKNOWN
    assert first.reason == second.reason == "domain_mx_valid_mailbox_unverified"
    assert calls == ["acme.com"]


def test_former_employer_address_is_soft_fallback_and_current_domain_wins(tmp_path):
    store, _, organization, evidence = setup(tmp_path)
    verifier = ContactVerifier(store, mx_lookup=lambda domain: True)

    former = verifier.verify(
        email="jane@former-employer.com",
        organization_domain=organization.domain,
    )
    assert former.status == VerificationStatus.UNKNOWN
    assert former.reason == "email_domain_organization_mismatch_mx_valid"

    candidates = select_best(
        [
            ContactCandidate(
                contact_candidate_id="former",
                run_id="run-1",
                lead_event_id="event-1",
                organization_id=organization.organization_id,
                person_id="person-1",
                person_name="Jane Manager",
                email=former.email,
                provider="fallback",
                verification_status=VerificationStatus.VERIFIED,
                verification_reason="external_email_verifier_valid_organization_mismatch",
                evidence=evidence,
            ),
            ContactCandidate(
                contact_candidate_id="current",
                run_id="run-1",
                lead_event_id="event-1",
                organization_id=organization.organization_id,
                person_id="person-1",
                person_name="Jane Manager",
                email="jane@acme.com",
                provider="web",
                verification_status=VerificationStatus.UNKNOWN,
                verification_reason="domain_mx_valid_mailbox_unverified",
                evidence=evidence,
            ),
        ]
    )
    selected = next(item for item in candidates if item.selected)
    assert selected.contact_candidate_id == "current"


def test_verifier_retains_sourced_generic_role_mailboxes(tmp_path):
    store, _, _, _ = setup(tmp_path)
    verifier = ContactVerifier(store, mx_lookup=lambda domain: True)

    result = verifier.verify(email="leasing@acme.com", organization_domain="acme.com")

    assert result.status == VerificationStatus.UNKNOWN
    assert result.reason == "domain_mx_valid_mailbox_unverified"


def test_decision_makers_require_sources_and_persist_people(tmp_path):
    store, artifacts, organization, _ = setup(tmp_path)
    payload = {
        "decision_makers": [{"name": "Jane Manager", "title": "General Manager", "scope": "Phoenix"}],
        "employee_count": {"value": "100", "scope": "company", "as_of": "2026"},
        "sources": [{"url": "https://acme.com/team", "supports": "Lists Jane as GM."}],
    }
    service = DecisionMakerService(
        store,
        artifacts,
        "grok-4.3",
        call_model=lambda model, prompt, tools: (json.dumps(payload), {"total_tokens": 50}),
    )
    people, reviews = service.research([organization])

    assert not reviews
    assert people[0].name == "Jane Manager"
    assert store.people("org-1")[0].title == "General Manager"


def test_decision_maker_prompt_prioritizes_authority_and_direct_contacts(tmp_path):
    store, artifacts, organization, _ = setup(tmp_path)
    calls = []
    payload = {"decision_makers": [], "employee_count": None, "sources": []}
    service = DecisionMakerService(
        store,
        artifacts,
        "grok-4.3",
        call_model=lambda model, prompt, tools: calls.append(prompt)
        or (json.dumps(payload), {}),
    )

    service.research([organization])

    prompt = calls[0]
    assert "Known domain: acme.com" in prompt
    assert "property/community manager" in prompt
    assert "facilities manager" in prompt
    assert "verify their relationship" in prompt
    assert "retain a sourced company mailbox" in prompt
    assert "not automatic exclusions" in prompt


def test_valid_empty_decision_maker_result_is_terminal(tmp_path):
    store, artifacts, organization, _ = setup(tmp_path)
    calls = []
    payload = {
        "decision_makers": [],
        "employee_count": None,
        "sources": [],
    }
    service = DecisionMakerService(
        store,
        artifacts,
        "grok-4.3",
        call_model=lambda model, prompt, tools: calls.append(prompt)
        or (json.dumps(payload), {}),
    )

    people, reviews = service.research([organization], attempts=2)

    assert not people
    assert not reviews
    assert len(calls) == 1


def test_company_dossier_projects_contacts_and_skips_person_fallback(tmp_path):
    store, artifacts, organization, evidence = setup(tmp_path)
    event = LeadEvent(
        lead_event_id="event-1",
        run_id="run-1",
        organization_id="org-1",
        primary_candidate_id="candidate-1",
        supporting_candidate_ids=["candidate-1"],
        event="Acme opened a Phoenix facility",
        location="Phoenix, Arizona",
        date_posted=date(2026, 8, 28),
        priority="high",
        evidence=evidence,
    )
    with store.connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            """INSERT INTO v2_lead_events(
                lead_event_id, run_id, organization_id, primary_candidate_id,
                record_status, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'valid', ?, 'now', 'now')""",
            (
                event.lead_event_id,
                event.run_id,
                event.organization_id,
                event.primary_candidate_id,
                event.model_dump_json(),
            ),
        )
        conn.commit()
    payload = {
        "canonical_domain": "acme.com",
        "decision_makers": [
            {
                "name": "Jane Manager",
                "title": "General Manager",
                "scope": "Phoenix",
                "email": "jane@acme.com",
                "phone": "",
                "linkedin": "https://linkedin.com/in/jane-manager",
                "sources": [
                    {
                        "url": "https://acme.com/team",
                        "supports": "Lists Jane's current role and contact details.",
                    }
                ],
            }
        ],
        "employee_count": None,
        "sources": [
            {"url": "https://acme.com/team", "supports": "Acme team page."}
        ],
    }
    verifier = ContactVerifier(store, mx_lookup=lambda domain: True)
    service = DecisionMakerService(
        store,
        artifacts,
        "grok-4.3",
        call_model=lambda model, prompt, tools: (json.dumps(payload), {}),
        verifier=verifier,
        events=[event],
    )

    people, reviews = service.research([organization])

    assert not reviews
    assert [person.name for person in people] == ["Jane Manager"]
    contacts = store.contacts_for_run("run-1")
    assert len(contacts) == 1
    assert contacts[0].provider == "model-dossier"
    assert contacts[0].selected
    assert contacts[0].email == "jane@acme.com"

    fallback_calls = []
    contact_service = ContactResearchService(
        store,
        artifacts,
        "grok-4.3",
        verifier,
        call_model=lambda model, prompt, tools: fallback_calls.append(prompt)
        or ("{}", {}),
    )
    selected, contact_reviews = contact_service.research(
        people, [organization], [event], attempts=2
    )

    assert not contact_reviews
    assert not fallback_calls
    assert len(selected) == 1 and selected[0].selected


def test_contact_research_runs_once_per_person_and_projects_to_events(tmp_path):
    store, artifacts, organization, evidence = setup(tmp_path)
    person = Person(
        person_id="person-1",
        organization_id="org-1",
        name="Jane Manager",
        title="General Manager",
        evidence=evidence,
    )
    store.save_person(person)
    events = [
        LeadEvent(
            lead_event_id=f"event-{index}",
            run_id="run-1",
            organization_id="org-1",
            primary_candidate_id=f"candidate-{index}",
            supporting_candidate_ids=[f"candidate-{index}"],
            event=f"Event {index}",
            location="Phoenix, Arizona",
            date_posted=date(2026, 8, 28),
            priority="high",
            evidence=evidence,
        )
        for index in (1, 2)
    ]
    # The event FK requires discovery candidate rows; disable FK only for this focused service fixture.
    with store.connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        for event in events:
            conn.execute(
                """INSERT INTO v2_lead_events(
                    lead_event_id, run_id, organization_id, primary_candidate_id,
                    record_status, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'valid', ?, 'now', 'now')""",
                (event.lead_event_id, event.run_id, event.organization_id, event.primary_candidate_id, event.model_dump_json()),
            )
        conn.commit()
    calls = []
    payload = {
        "name": "Jane Manager",
        "organization": "Acme Marketplace",
        "email": "jane@acme.com",
        "phone": "",
        "linkedin": "https://linkedin.com/in/jane-manager",
        "sources": [{"url": "https://acme.com/team", "supports": "Lists Jane's contact details."}],
    }
    service = ContactResearchService(
        store,
        artifacts,
        "grok-4.3",
        ContactVerifier(store, mx_lookup=lambda domain: True),
        call_model=lambda model, prompt, tools: calls.append(prompt) or (json.dumps(payload), {}),
    )
    contacts, reviews = service.research([person], [organization], events)

    assert not reviews and len(calls) == 1
    assert len(contacts) == 2
    assert {item.lead_event_id for item in contacts} == {"event-1", "event-2"}
    assert all(item.selected and item.verification_status == VerificationStatus.UNKNOWN for item in contacts)
    assert all(
        item.verification_reason == "domain_mx_valid_mailbox_unverified"
        for item in contacts
    )


def test_mismatched_contact_identity_enters_review(tmp_path):
    store, artifacts, organization, evidence = setup(tmp_path)
    person = Person(
        person_id="person-1",
        organization_id="org-1",
        name="Jane Manager",
        evidence=evidence,
    )
    service = ContactResearchService(
        store,
        artifacts,
        "grok-4.3",
        ContactVerifier(store, mx_lookup=lambda domain: True),
        call_model=lambda model, prompt, tools: (
            json.dumps({"name": "Different Person", "organization": "Acme Marketplace"}),
            {},
        ),
    )
    contacts, reviews = service.research([person], [organization], [], attempts=1)

    assert not contacts
    assert reviews[0].reason_code == "model_contract_invalid"


def test_valid_empty_contact_result_is_terminal(tmp_path):
    store, artifacts, organization, evidence = setup(tmp_path)
    person = Person(
        person_id="person-1",
        organization_id="org-1",
        name="Jane Manager",
        evidence=evidence,
    )
    calls = []
    payload = {
        "name": "Jane Manager",
        "organization": "Acme Marketplace",
        "email": "",
        "phone": "",
        "linkedin": "",
        "sources": [],
    }
    service = ContactResearchService(
        store,
        artifacts,
        "grok-4.3",
        ContactVerifier(store, mx_lookup=lambda domain: True),
        call_model=lambda model, prompt, tools: calls.append(prompt)
        or (json.dumps(payload), {}),
    )

    contacts, reviews = service.research(
        [person], [organization], [], attempts=2
    )

    assert not contacts
    assert not reviews
    assert len(calls) == 1
