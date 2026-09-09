"""Safety and identity tests for the typed Scout-to-sales boundary."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from integration.campaign import campaign_manifest_hash
from integration.cli import _fallback_address_plan, _sole_email_role_plan
from integration.config import ActivationBlocked, Settings
from integration.database import Database
from integration.handoff import (
    HANDOFF_PROTOCOL_VERSION,
    enqueue_handoff,
    handoff_content_hash,
    ingest_handoff,
    load_handoff,
)
from integration.legacy_reconcile import (
    SWVP_EVENT_ID,
    SWVP_LEGACY_ORGANIZATION_ID,
    apply_legacy_swvp_local,
    legacy_swvp_plan,
)
from integration.models import (
    ApprovalBatch,
    CompanySync,
    EligibilityStatus,
    EventRole,
    LeadEventSync,
    MappingRecord,
    OutreachSequenceSync,
    RecipientSync,
    SalesHandoff,
)
from integration.workflows import SalesWorkflows


def _company() -> CompanySync:
    return CompanySync(
        company_id="company-1",
        canonical_name="Acme Marketplace",
        domain="acme.example",
        aliases=["Acme"],
        legacy_ids=["organization-old"],
    )


def _event(event_id: str = "event-1") -> LeadEventSync:
    return LeadEventSync(
        run_id="run-1",
        lead_event_id=event_id,
        company_id="company-1",
        organization_name="Acme Marketplace",
        event_role=EventRole.ANCHOR,
        event="Opened a Phoenix marketplace",
        location="Phoenix",
        date_posted="2026-08-30",
        article_url="https://example.com/acme",
        score=88,
        confidence="high",
        record_status="valid",
        actionable_route=True,
        crm_eligible=True,
    )


def _recipient(
    recipient_id: str = "recipient-1", email: str = "jane@acme.example"
) -> RecipientSync:
    return RecipientSync(
        recipient_id=recipient_id,
        company_id="company-1",
        person_id=f"person-{recipient_id}",
        contact_candidate_id=f"contact-{recipient_id}",
        full_name="Jane Manager",
        first_name="Jane",
        title="General Manager",
        scope="Phoenix",
        email=email,
        source_provider="web",
        source_verification_status="verified",
        role_score=92,
        rank=1,
        primary=True,
    )


def _sequence(
    sequence_id: str = "sequence-1",
    recipient_id: str = "recipient-1",
    campaign_protocol: str = "recipient-outreach-v4",
) -> OutreachSequenceSync:
    return OutreachSequenceSync(
        sequence_id=sequence_id,
        run_id="run-1",
        company_id="company-1",
        campaign_protocol=campaign_protocol,
        anchor_lead_event_id="event-1",
        primary_recipient_id=recipient_id,
        why_template_key="opening",
        why_slots={"property": "acme marketplace", "location": "Phoenix"},
        why_sources=["https://example.com/acme"],
        why_confidence="high",
        company_why_line="Hi [first name] — Saw acme marketplace opened in Phoenix.",
        personalized_why_line="Hi Jane — Saw acme marketplace opened in Phoenix.",
        merge_snapshot={
            "firstName": "Jane",
            "company": "Acme Marketplace",
            "whyLine": "Hi Jane — Saw acme marketplace opened in Phoenix.",
            "unsubscribeUrl": "__integration_generated__",
        },
        merge_hash=f"merge-{sequence_id}",
        eligibility_status=EligibilityStatus.READY,
    )


def _handoff() -> SalesHandoff:
    value = SalesHandoff(
        schema_version=1,
        protocol_version=HANDOFF_PROTOCOL_VERSION,
        run_id="run-1",
        companies=[_company()],
        lead_events=[_event()],
        recipients=[_recipient()],
        sequences=[_sequence()],
        content_hash="pending",
    )
    return value.model_copy(update={"content_hash": handoff_content_hash(value)})


class FakePipedrive:
    def __init__(self):
        self.created_leads = []
        self.updated_leads = []
        self.created_people = []
        self.activities = []

    def find_organization(self, name):
        return None

    def create_organization(self, name, owner_id, location):
        return 101

    def find_lead_by_event_id(self, event_id):
        return None

    def create_lead(self, title, person_id, organization_id, owner_id, fields):
        self.created_leads.append((title, person_id, organization_id, fields))
        return "lead-1"

    def update_lead(self, lead_id, fields):
        self.updated_leads.append((lead_id, fields))
        return {}

    def find_person(self, email):
        return None

    def create_person(self, name, email, organization_id, owner_id, fields):
        self.created_people.append((name, email, organization_id, fields))
        return 201

    def update_person(self, person_id, fields):
        return {}

    def add_lead_activity(self, lead_id, subject, owner_id, note=""):
        self.activities.append((lead_id, subject, owner_id, note))
        return 301

    def close(self):
        pass


class FakeWarmy:
    def __init__(self, verification="valid", campaign=None):
        self.verification = verification
        self.calls = []
        self.campaign = campaign or {}

    def verify_email(self, email, operation_key):
        self.calls.append(("verify", email))
        return {"data": {"status": self.verification}}

    def find_prospect_by_email(self, email):
        self.calls.append(("find", email))
        return None

    def create_prospect(self, payload, operation_key):
        self.calls.append(("create", payload["email"]))
        return {"data": {"id": "prospect-1"}}

    def update_prospect(self, prospect_id, payload, operation_key):
        self.calls.append(("update", payload["email"]))
        return {"data": {"id": prospect_id}}

    def get_campaign(self, campaign_id):
        return {"data": self.campaign}

    def enroll(self, campaign_id, prospect_ids, operation_key):
        self.calls.append(("enroll", prospect_ids[0]))
        return {"data": {"enrolled": 1}}

    def close(self):
        pass


class FakeGmail:
    def __init__(self):
        self.forwarded = []

    def find_message(self, mailbox, message_ref):
        assert message_ref == "<reply@example.com>"
        return "gmail-message-1"

    def forward_message(self, mailbox, message_id, recipient):
        self.forwarded.append((mailbox, message_id, recipient))
        return {"id": "forward-1"}

    def get_message(self, mailbox, message_id, format="raw"):
        return {"threadId": "gmail-thread-1"}


def _seed(db: Database, *, sequence_count: int = 1) -> None:
    db.upsert_company(_company())
    db.update_company("company-1", pipedrive_organization_id=101)
    db.upsert_lead_event(_event())
    db.update_lead_event("event-1", pipedrive_lead_id="lead-1")
    for number in range(1, sequence_count + 1):
        recipient_id = f"recipient-{number}"
        db.upsert_recipient(
            _recipient(recipient_id, f"jane{number}@acme.example")
        )
        db.save_sequence(
            _sequence(
                f"sequence-{number}",
                recipient_id,
                f"recipient-outreach-v4-test-{number}",
            )
        )


def _write_handoff(path, handoff: SalesHandoff) -> None:
    path.write_text(handoff.model_dump_json(indent=2), encoding="utf-8")


def test_handoff_hash_rejects_tampering_and_ingestion_is_idempotent(tmp_path):
    path = tmp_path / "sales_handoff.json"
    handoff = _handoff()
    _write_handoff(path, handoff)
    assert load_handoff(path).run_id == "run-1"

    db = Database(tmp_path / "sales.sqlite")
    first = enqueue_handoff(db, path)
    second = enqueue_handoff(db, path)
    assert first["lead_jobs"] == first["sequence_jobs"] == 1
    assert second["lead_jobs"] == second["sequence_jobs"] == 0

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["lead_events"][0]["score"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="content hash mismatch"):
        load_handoff(path)


def test_exact_handoff_replay_preserves_provider_derived_sequence_state(tmp_path):
    path = tmp_path / "sales_handoff.json"
    handoff = _handoff()
    _write_handoff(path, handoff)
    db = Database(tmp_path / "sales.sqlite")

    first = enqueue_handoff(db, path)
    assert first["sequence_jobs"] == 1

    runtime_payload = _sequence().model_dump(mode="json")
    runtime_payload["merge_snapshot"]["unsubscribeUrl"] = (
        "https://sales.example.com/unsubscribe?t=signed"
    )
    runtime_payload["merge_hash"] = "provider-derived-merge"
    db.update_sequence(
        "sequence-1",
        eligibility_status="blocked",
        eligibility_reasons=["warmy_verification_catch_all"],
        merge_hash="provider-derived-merge",
        payload=runtime_payload,
    )

    replay = enqueue_handoff(db, path)
    assert replay["sequence_jobs"] == 0
    sequence = db.get_sequence("sequence-1")
    assert sequence["eligibility_status"] == "blocked"
    assert sequence["eligibility_reasons"] == ["warmy_verification_catch_all"]
    assert sequence["merge_hash"] == "provider-derived-merge"
    assert (
        sequence["payload"]["merge_snapshot"]["unsubscribeUrl"]
        == "https://sales.example.com/unsubscribe?t=signed"
    )


def test_changed_sequence_merge_hash_refreshes_draft_and_enqueues_new_sync(tmp_path):
    path = tmp_path / "sales_handoff.json"
    first_handoff = _handoff()
    _write_handoff(path, first_handoff)
    db = Database(tmp_path / "sales.sqlite")
    assert enqueue_handoff(db, path)["sequence_jobs"] == 1

    changed_sequence = _sequence().model_copy(
        update={
            "personalized_why_line": "Hi Jane — Saw the expanded Phoenix marketplace.",
            "merge_snapshot": {
                **_sequence().merge_snapshot,
                "whyLine": "Hi Jane — Saw the expanded Phoenix marketplace.",
            },
            "merge_hash": "changed-merge-hash",
        }
    )
    changed = first_handoff.model_copy(
        update={"sequences": [changed_sequence], "content_hash": "pending"}
    )
    changed = changed.model_copy(update={"content_hash": handoff_content_hash(changed)})
    _write_handoff(path, changed)

    result = enqueue_handoff(db, path)
    assert result["sequence_jobs"] == 1
    sequence = db.get_sequence("sequence-1")
    assert sequence["merge_hash"] == "changed-merge-hash"
    assert sequence["payload"]["personalized_why_line"] == (
        "Hi Jane — Saw the expanded Phoenix marketplace."
    )


def test_non_draft_sequence_snapshot_is_fully_immutable_on_save(tmp_path):
    db = Database(tmp_path / "sales.sqlite")
    _seed(db)
    original = db.get_sequence("sequence-1")
    db.update_sequence(
        "sequence-1",
        approval_state="approved",
        eligibility_status="ready",
        approval_batch_id="approval-1",
    )
    incoming = _sequence().model_copy(
        update={
            "personalized_why_line": "This must not replace approved copy.",
            "eligibility_status": EligibilityStatus.BLOCKED,
            "eligibility_reasons": ["later_handoff_replay"],
        }
    )

    db.save_sequence(incoming)

    preserved = db.get_sequence("sequence-1")
    assert preserved["approval_state"] == "approved"
    assert preserved["approval_batch_id"] == "approval-1"
    assert preserved["eligibility_status"] == "ready"
    assert preserved["eligibility_reasons"] == []
    assert preserved["payload"] == original["payload"]


def test_ingest_rejects_multiple_ready_sequences_for_normalized_email(tmp_path):
    original = _handoff()
    second_company = _company().model_copy(
        update={"company_id": "company-2", "canonical_name": "Second Company"}
    )
    second_event = _event("event-2").model_copy(
        update={"company_id": "company-2", "organization_name": "Second Company"}
    )
    second_recipient = _recipient(
        "recipient-2", " JANE@ACME.EXAMPLE "
    ).model_copy(update={"company_id": "company-2"})
    second_sequence = _sequence("sequence-2", "recipient-2").model_copy(
        update={
            "company_id": "company-2",
            "anchor_lead_event_id": "event-2",
        }
    )
    handoff = original.model_copy(
        update={
            "companies": [*original.companies, second_company],
            "lead_events": [*original.lead_events, second_event],
            "recipients": [*original.recipients, second_recipient],
            "sequences": [*original.sequences, second_sequence],
            "content_hash": "hand-authored",
        }
    )
    db = Database(tmp_path / "sales.sqlite")

    with pytest.raises(
        ValueError,
        match="multiple READY sequences for normalized email jane@acme.example",
    ):
        ingest_handoff(db, handoff)

    assert db.get_company("company-1") is None


def test_pipedrive_lead_is_created_without_a_person(tmp_path):
    db = Database(tmp_path / "sales.sqlite")
    db.upsert_company(_company())
    db.upsert_lead_event(_event())
    pipedrive = FakePipedrive()
    workflows = SalesWorkflows(
        Settings(provider_writes_enabled=True), db, pipedrive=pipedrive
    )
    workflows.sync_lead_event({"lead_event": _event().model_dump(mode="json")})
    assert pipedrive.created_leads[0][1] is None
    assert db.get_lead_event("event-1")["pipedrive_lead_id"] == "lead-1"


def test_pipedrive_same_name_does_not_steal_other_company_mapping(tmp_path):
    db = Database(tmp_path / "sales.sqlite")
    db.upsert_company(_company())
    other = _company().model_copy(update={
        "company_id": "franchisee", "domain": "franchise.example",
        "aliases": [], "legacy_ids": [],
    })
    db.upsert_company(other)
    db.update_company("franchisee", pipedrive_organization_id=99)
    db.upsert_lead_event(_event())
    pipedrive = FakePipedrive()
    pipedrive.find_organization = lambda name: 99
    workflows = SalesWorkflows(
        Settings(provider_writes_enabled=True), db, pipedrive=pipedrive
    )
    workflows.sync_lead_event({"lead_event": _event().model_dump(mode="json")})
    assert db.get_company("franchisee")["pipedrive_organization_id"] == 99
    assert db.get_company("company-1")["pipedrive_organization_id"] == 101
    assert pipedrive.created_leads[0][2] == 101


def test_invalid_verification_blocks_prospect_creation(tmp_path):
    db = Database(tmp_path / "sales.sqlite")
    _seed(db)
    warmy = FakeWarmy("invalid")
    workflows = SalesWorkflows(
        Settings(
            provider_writes_enabled=True,
            public_base_url="https://sales.example.com",
            unsubscribe_secret="secret",
        ),
        db,
        warmy=warmy,
        pipedrive=FakePipedrive(),
    )
    workflows.sync_sequence(
        {
            "sequence": _sequence().model_dump(mode="json"),
            "recipient": _recipient().model_dump(mode="json"),
        }
    )
    assert warmy.calls == [("verify", "jane@acme.example")]
    assert db.get_sequence("sequence-1")["eligibility_status"] == "blocked"


@pytest.mark.parametrize("verification", ["catch_all", "unknown"])
def test_uncertain_verification_creates_fallback_prospect(tmp_path, verification):
    db = Database(tmp_path / "sales.sqlite")
    _seed(db)
    warmy = FakeWarmy(verification)
    workflows = SalesWorkflows(
        Settings(
            provider_writes_enabled=True,
            public_base_url="https://sales.example.com",
            unsubscribe_secret="secret",
        ),
        db,
        warmy=warmy,
        pipedrive=FakePipedrive(),
    )
    workflows.sync_sequence(
        {
            "sequence": _sequence().model_dump(mode="json"),
            "recipient": _recipient().model_dump(mode="json"),
        }
    )

    assert warmy.calls == [
        ("verify", "jane@acme.example"),
        ("find", "jane@acme.example"),
        ("create", "jane@acme.example"),
    ]
    assert db.get_sequence("sequence-1")["eligibility_status"] == "ready"
    assert db.get_recipient(recipient_id="recipient-1")["verification_status"] == verification


@pytest.mark.parametrize("verification", ["catch_all", "unknown"])
def test_fallback_reconciliation_only_requeues_unsuppressed_address_blocks(
    tmp_path, verification
):
    db = Database(tmp_path / "sales.sqlite")
    _seed(db)
    db.update_recipient("recipient-1", verification_status=verification)
    db.update_sequence(
        "sequence-1",
        eligibility_status="blocked",
        eligibility_reasons=[f"warmy_verification_{verification}"],
    )

    plan = _fallback_address_plan(db)
    assert [item["sequence_id"] for item in plan] == ["sequence-1"]
    assert plan[0]["sequence"]["eligibility_status"] == "ready"
    assert plan[0]["sequence"]["eligibility_reasons"] == []

    db.suppress(
        "jane1@acme.example",
        "manual",
        "test",
    )
    assert _fallback_address_plan(db) == []


def test_fallback_reconciliation_keeps_the_only_low_role_catch_all_address(tmp_path):
    db = Database(tmp_path / "sales.sqlite")
    _seed(db)
    recipient = _recipient().model_copy(update={"role_score": 5})
    db.upsert_recipient(recipient)
    db.update_recipient("recipient-1", verification_status="catch_all")
    db.update_sequence(
        "sequence-1",
        eligibility_status="blocked",
        eligibility_reasons=["warmy_verification_catch_all"],
    )

    plan = _fallback_address_plan(db)
    assert [item["sequence_id"] for item in plan] == ["sequence-1"]
    assert "sole_email_role_fallback" in plan[0]["recipient"]["selection_rationale"]


def test_low_role_primary_is_allowed_when_it_is_the_only_found_address(tmp_path):
    db = Database(tmp_path / "sales.sqlite")
    _seed(db)
    recipient = _recipient().model_copy(update={"role_score": 10})
    db.upsert_recipient(recipient)
    warmy = FakeWarmy("valid")
    workflows = SalesWorkflows(
        Settings(
            provider_writes_enabled=True,
            public_base_url="https://sales.example.com",
            unsubscribe_secret="secret",
        ),
        db,
        warmy=warmy,
        pipedrive=FakePipedrive(),
    )

    workflows.sync_sequence(
        {
            "sequence": _sequence().model_dump(mode="json"),
            "recipient": recipient.model_dump(mode="json"),
        }
    )

    assert db.get_sequence("sequence-1")["eligibility_status"] == "ready"
    assert ("create", "jane@acme.example") in warmy.calls


def test_low_role_primary_stays_blocked_when_another_address_exists(tmp_path):
    db = Database(tmp_path / "sales.sqlite")
    _seed(db, sequence_count=2)
    recipient = _recipient().model_copy(update={"role_score": 10})
    db.upsert_recipient(recipient)
    warmy = FakeWarmy("valid")
    workflows = SalesWorkflows(
        Settings(provider_writes_enabled=True),
        db,
        warmy=warmy,
        pipedrive=FakePipedrive(),
    )

    workflows.sync_sequence(
        {
            "sequence": _sequence().model_dump(mode="json"),
            "recipient": recipient.model_dump(mode="json"),
        }
    )

    assert db.get_sequence("sequence-1")["eligibility_status"] == "blocked"
    assert warmy.calls == []


def test_role_fallback_reconciliation_requires_one_address_or_passing_score(tmp_path):
    db = Database(tmp_path / "sales.sqlite")
    _seed(db)
    recipient = _recipient().model_copy(update={"role_score": 10})
    db.upsert_recipient(recipient)
    db.update_sequence(
        "sequence-1",
        eligibility_status="blocked",
        eligibility_reasons=["recipient_role_score_below_70"],
    )

    plan = _sole_email_role_plan(db)
    assert [item["sequence_id"] for item in plan] == ["sequence-1"]
    assert "sole_email_role_fallback" in plan[0]["recipient"]["selection_rationale"]

    db.upsert_recipient(_recipient("recipient-2", "other@acme.example"))
    assert _sole_email_role_plan(db) == []


def test_mx_precheck_reaches_authoritative_warmy_verification(tmp_path):
    db = Database(tmp_path / "sales.sqlite")
    _seed(db)
    warmy = FakeWarmy("valid")
    workflows = SalesWorkflows(
        Settings(
            provider_writes_enabled=True,
            public_base_url="https://sales.example.com",
            unsubscribe_secret="secret",
        ),
        db,
        warmy=warmy,
        pipedrive=FakePipedrive(),
    )
    recipient = _recipient().model_copy(
        update={
            "source_verification_status": "unknown",
            "source_verification_reason": "domain_mx_valid_mailbox_unverified",
        }
    )
    workflows.sync_sequence(
        {
            "sequence": _sequence().model_dump(mode="json"),
            "recipient": recipient.model_dump(mode="json"),
        }
    )
    assert warmy.calls[0] == ("verify", "jane@acme.example")
    assert db.get_sequence("sequence-1")["eligibility_status"] == "ready"


def _campaign() -> dict:
    return {
        "id": "campaign-1",
        "name": "Aether outreach",
        "description": "Aether AEC evergreen outreach",
        "channel": "email",
        "timezone": "America/Phoenix",
        "dailySendLimit": 150,
        "sendingWindowStart": 8,
        "sendingWindowEnd": 16,
        "scheduleDays": [1, 2, 3, 4, 5],
        "stopOnReply": True,
        "stopOnBounce": True,
        "stopOnUnsubscribe": True,
        "trackOpens": False,
        "trackClicks": False,
        "mailboxIds": [f"mailbox-{number}" for number in range(1, 7)],
        "steps": [{"stepIndex": number} for number in range(4)],
        "status": "paused",
    }


def _activation_settings(campaign: dict) -> Settings:
    deal_keys = {
        "aether_lead_event_id",
        "aether_outreach_id",
        "aether_contact_candidate_id",
        "canonical_company_id",
        "event_role",
        "outreach_sequence_id",
        "warmy_prospect_id",
        "outreach_state",
        "reply_disposition",
        "reply_received_at",
        "unsubscribe_url",
        "article_url",
        "date_posted",
    }
    person_keys = {
        "aether_person_id",
        "verification_status",
        "suppressed",
        "suppression_reason",
        "unsubscribe_url",
    }
    mailboxes = tuple(f"mailbox-{number}" for number in range(1, 7))
    return Settings(
        provider_writes_enabled=True,
        warmy_enrollment_enabled=True,
        campaign_start_enabled=True,
        pipedrive_automation_ready=True,
        email_templates_approved=True,
        postal_address="123 Main St, Phoenix, AZ 85001",
        public_base_url="https://sales.example.com",
        unsubscribe_secret="secret",
        warmy_api_key="key",
        warmy_webhook_secret="webhook",
        warmy_campaign_id="campaign-1",
        warmy_campaign_manifest_hash=campaign_manifest_hash(campaign),
        warmy_mailbox_ids=mailboxes,
        warmy_mailbox_emails={item: f"{item}@aetherclean.com" for item in mailboxes},
        pipedrive_api_token="token",
        pipedrive_domain="aether",
        pipedrive_webhook_user="user",
        pipedrive_webhook_password="password",
        pipedrive_deal_fields={key: f"field-{key}" for key in deal_keys},
        pipedrive_person_fields={key: f"field-{key}" for key in person_keys},
        pipedrive_person_enum_values={
            "verification_status.pending": "1",
            "verification_status.valid": "2",
            "verification_status.invalid": "3",
            "verification_status.catch_all": "4",
            "verification_status.unknown": "5",
            "suppressed.yes": "6",
            "suppressed.no": "7",
        },
        pipedrive_reply_disposition_values={
            value: value
            for value in (
                "pending_review",
                "positive",
                "negative",
                "out_of_office",
                "unsubscribe",
                "other",
            )
        },
        gmail_reply_forwarding_enabled=False,
    )


def test_approval_batch_releases_only_the_named_sequence(tmp_path):
    db = Database(tmp_path / "sales.sqlite")
    _seed(db, sequence_count=2)
    for number in (1, 2):
        db.update_recipient(
            f"recipient-{number}",
            verification_status="valid",
            warmy_prospect_id=f"prospect-{number}",
        )
    campaign = _campaign()
    settings = replace(_activation_settings(campaign), campaign_start_enabled=False)
    campaign.pop("mailboxIds")
    assert settings.campaign_enrollment_ready
    assert not settings.campaign_activation_ready
    now = datetime.now(UTC)
    db.save_approval_batch(
        ApprovalBatch(
            batch_id="batch-canary",
            campaign_id="campaign-1",
            campaign_manifest_hash=settings.warmy_campaign_manifest_hash,
            sequence_ids=["sequence-1"],
            merge_hashes={"sequence-1": "merge-sequence-1"},
            maximum_recipient_count=1,
            approved_by="test-operator",
            approved_at=now,
            expires_at=now + timedelta(hours=1),
        )
    )
    warmy = FakeWarmy(campaign=campaign)
    workflows = SalesWorkflows(
        settings, db, warmy=warmy, pipedrive=FakePipedrive()
    )
    workflows.enroll_sequence({"sequence_id": "sequence-1"})
    with pytest.raises(ActivationBlocked, match="approval batch"):
        workflows.enroll_sequence({"sequence_id": "sequence-2"})
    assert ("enroll", "prospect-1") in warmy.calls
    assert ("enroll", "prospect-2") not in warmy.calls
    assert db.get_state(
        "warmy:campaign:mailbox-verification:campaign-1"
    ) == {
        "status": "not_returned",
        "requires_ui_check": True,
        "reason": "Warmy readback omitted mailboxIds/mailboxes; verify in the UI",
    }


@pytest.mark.parametrize("status", ["scheduled", "running"])
def test_sequence_enrollment_rejects_sendable_campaign_status_without_start(
    tmp_path, status
):
    campaign = _campaign()
    campaign["status"] = status
    settings = replace(_activation_settings(campaign), campaign_start_enabled=False)
    workflows = SalesWorkflows(
        settings,
        Database(tmp_path / f"{status}.sqlite"),
        warmy=FakeWarmy(campaign=campaign),
        pipedrive=FakePipedrive(),
    )

    with pytest.raises(ActivationBlocked, match=f"unsafe campaign status {status}"):
        workflows._validate_live_campaign(
            {"data": campaign}, for_enrollment=True
        )


def test_live_campaign_validation_rejects_present_mailbox_mismatch(tmp_path):
    campaign = _campaign()
    campaign["mailboxIds"] = ["mailbox-other"]
    settings = replace(_activation_settings(_campaign()), campaign_start_enabled=False)
    workflows = SalesWorkflows(settings, Database(tmp_path / "mismatch.sqlite"))

    with pytest.raises(ActivationBlocked, match="mailbox set mismatch"):
        workflows._validate_live_campaign(
            {"data": campaign}, for_enrollment=True
        )


@pytest.mark.parametrize("status", ["draft", "running"])
def test_variant_observation_gap_preserves_draft_ingestion_but_blocks_sending(tmp_path, status):
    from integration.variants import load_plan
    campaign = _campaign()
    campaign.update(id=load_plan().campaign_id, status=status)
    settings = replace(_activation_settings(campaign), warmy_campaign_id=campaign["id"], campaign_start_enabled=True)
    workflows = SalesWorkflows(settings, Database(tmp_path / "variants.sqlite"),
                              warmy=FakeWarmy(campaign=campaign), pipedrive=FakePipedrive())
    if status == "draft":
        result = workflows._validate_live_campaign({"data": campaign}, for_enrollment=True)
        assert result["subject_variants"]["status"] == "not_verified"
    else:
        with pytest.raises(ActivationBlocked, match="Variant-capable read required"):
            workflows._validate_live_campaign({"data": campaign}, for_enrollment=True)


def test_typed_sequence_reply_forwards_original_message_to_jordan(tmp_path):
    db = Database(tmp_path / "sales.sqlite")
    _seed(db)
    db.update_recipient("recipient-1", warmy_prospect_id="prospect-1")
    gmail = FakeGmail()
    pipedrive = FakePipedrive()
    workflows = SalesWorkflows(
        Settings(
            gmail_reply_forwarding_enabled=True,
            gmail_forward_to="jw@aetherclean.com",
            warmy_mailbox_emails={"mailbox-1": "sender@aetherclean.com"},
        ),
        db,
        pipedrive=pipedrive,
        gmail=gmail,
    )
    workflows.handle_warmy_event(
        {
            "event_id": "reply-1",
            "event_type": "reply.received",
            "data": {
                "prospectId": "prospect-1",
                "prospectEmail": "jane@acme.example",
                "mailboxId": "mailbox-1",
                "messageId": "<reply@example.com>",
                "subject": "Re: facilities",
            },
        }
    )
    assert gmail.forwarded == [
        ("sender@aetherclean.com", "gmail-message-1", "jw@aetherclean.com")
    ]
    assert "forwarded to Jordan" in pipedrive.activities[0][3]
    assert db.get_state("sequence-gmail-route:sequence-1") == {
        "mailbox": "sender@aetherclean.com",
        "thread_id": "gmail-thread-1",
    }


def test_uncertain_provider_write_reconciles_without_calling_create_twice(tmp_path):
    db = Database(tmp_path / "sales.sqlite")
    workflows = SalesWorkflows(Settings(), db)
    calls = []

    def timed_out():
        calls.append("create")
        raise TimeoutError("provider response was lost")

    with pytest.raises(TimeoutError):
        workflows._operation(
            "provider", "create:one", {"name": "Acme"}, timed_out,
            reconcile=lambda: None,
        )
    assert db.get_operation("provider", "create:one")["status"] == "uncertain"

    result = workflows._operation(
        "provider",
        "create:one",
        {"name": "Acme"},
        lambda: calls.append("unexpected") or {"id": "duplicate"},
        reconcile=lambda: {"id": "remote-1"},
    )
    assert result == {"id": "remote-1"}
    assert calls == ["create"]
    assert db.get_operation("provider", "create:one")["status"] == "completed"


def test_legacy_swvp_reconciliation_is_local_only_and_supersedes_enrollment(tmp_path):
    path = tmp_path / "sales.sqlite"
    db = Database(path)
    contacts = [
        (
            "cmack@swvp.com",
            "Cary Mack",
            "Principal and Co-Managing Partner",
            "outreach-cary",
            "person-cary",
            "contact-cary",
            "lead-cary",
            "prospect-cary",
        ),
        (
            "jmerritt@swvp.com",
            "Justin Merritt",
            "Managing Director and Partner - Southwestern United States",
            "outreach-justin",
            "person-justin",
            "contact-justin",
            "lead-justin",
            "prospect-justin",
        ),
        (
            "mschlossberg@swvp.com",
            "Mark Schlossberg",
            "Principal and Co-Managing Partner",
            "outreach-mark",
            "person-mark",
            "contact-mark",
            "lead-mark",
            "prospect-mark",
        ),
    ]
    for index, (email, name, title, outreach, person, contact, lead, prospect) in enumerate(
        contacts, start=1
    ):
        db.upsert_mapping(
            MappingRecord(
                outreach_id=outreach,
                source_contact_candidate_id=contact,
                source_verification_status="verified",
                source_provider="model",
                email=email,
                lead_event_id=SWVP_EVENT_ID,
                organization_id=SWVP_LEGACY_ORGANIZATION_ID,
                person_id=person,
                pipedrive_organization_id=29131,
                pipedrive_person_id=29220 + index,
                pipedrive_lead_id=lead,
                warmy_prospect_id=prospect,
                verification_status="valid",
            )
        )
        payload = {
            "run_id": "legacy-run",
            "outreach_id": outreach,
            "person_name": name,
            "title": title,
            "event": "Esplanade III office building sale for $86 million",
            "location": "Phoenix Camelback Corridor",
            "date_posted": "2026-08-29",
            "article_url": "https://example.com/esplanade",
            "why_line": (
                f"Hi {name.split()[0]} just wanted to reach out since I saw on the news "
                "that Southwest Value Partners took ownership of Esplanade III in Phoenix."
            ),
        }
        db.enqueue_work("scout.contact.sync", f"legacy-contact-{index}", payload)

    claimed = db.claim_work("seed", 10, 60)
    assert len(claimed) == 3
    for item in claimed:
        db.complete_work(item.id, "seed")
    for index, (_, _, _, outreach, _, _, _, _) in enumerate(contacts, start=1):
        db.enqueue_work(
            "warmy.enroll", f"legacy-enroll-{index}", {"outreach_id": outreach}
        )

    plan = legacy_swvp_plan(path)
    assert plan["primary"]["email"] == "cmack@swvp.com"
    assert plan["provider_writes"] == []
    applied = apply_legacy_swvp_local(path)
    assert applied["provider_writes_performed"] is False
    assert applied["superseded_enrollment_jobs"] == 3
    assert db.get_sequence(applied["sequence_id"])["eligibility_status"] == "review"
    with db.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM work_items WHERE kind='warmy.enroll' AND status='pending'"
        ).fetchone()[0] == 0
