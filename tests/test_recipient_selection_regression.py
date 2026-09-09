"""Exercise actual Scout ranking, not only its sort-key helper."""
from types import SimpleNamespace as NS
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scout"))
from v2.outreach import CompanyOutreachService
from v2.contracts import RecordStatus, VerificationStatus


def test_strong_fallback_is_selected_when_current_domain_contact_would_be_blocked():
    service = NS(state=NS(reviews_for_run=lambda _: []), artifacts=NS(run_id="run"))
    profile = NS(company_id="company", organization_ids=["org"], domain="current.example",
                 anchor_lead_event_id="event", relationship_type="owner", outreach_route="direct_facility",
                 relationship_confidence="high", record_status=RecordStatus.VALID,
                 why_line_status="valid", why_confidence="high")
    people = [NS(person_id="weak", name="Jane Smith", title="Assistant", scope="Phoenix"),
              NS(person_id="strong", name="John Doe", title="Facilities Manager", scope="Phoenix")]
    contacts = [NS(selected=True, email=email, person_id=person.person_id, organization_id="org",
                   contact_candidate_id=person.person_id, provider="grok", evidence=["source"],
                   verification_status=VerificationStatus.UNKNOWN, verification_reason="mx_valid")
                for person, email in zip(people, ["jane@current.example", "john@fallback.example"])]
    event = NS(lead_event_id="event", record_status=RecordStatus.VALID, confidence="high")
    rows = CompanyOutreachService._rank_recipients(service, [profile], people, contacts, [event], {"event": 80})
    primary = next(r for r in rows if r.primary)
    assert primary.person_id == "strong"
    assert primary.eligibility_status == "ready"
    assert not primary.eligibility_reasons
