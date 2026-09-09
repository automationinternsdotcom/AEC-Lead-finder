from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from integration.api import create_app
from integration.campaign import COPY_PLACEHOLDER, campaign_manifest_hash, load_campaign
from integration.config import ActivationBlocked, Settings
from integration.database import Database
from integration.handoff import HANDOFF_PROTOCOL_VERSION, handoff_content_hash
from integration.memory import MemoryDatabase
from integration.models import (
    CompanySync,
    EligibilityStatus,
    EventRole,
    LeadEventSync,
    MappingRecord,
    ReplyDisposition,
    SalesHandoff,
    VerificationStatus,
)
from integration.providers import GmailClient, PipedriveClient, WarmyClient
from integration.scout_bridge import contacts_from_csv, enqueue_contacts
from integration.security import (
    SignatureError,
    issue_unsubscribe_token,
    verify_unsubscribe_token,
    verify_warmy_signature,
)
from integration.worker import run_once
from integration.workflows import SalesWorkflows

ROOT = Path(__file__).resolve().parent.parent


def _load_scout_pipeline(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scout"))
    spec = importlib.util.spec_from_file_location(
        "aether_v2_pipeline_entrypoint", ROOT / "scout" / "pipeline.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_scout_pipeline_script_can_load_its_cross_package_handoff_imports():
    import subprocess

    result = subprocess.run(
        ["uv", "run", "scout/pipeline.py", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--apollo-go" in result.stdout


class FakeWarmy:
    def __init__(self):
        self.created = []
        self.updated = []
        self.enrolled = []
        self.suppressed = []
        self.unenrolled = []

    def create_prospect(self, payload, operation_key):
        self.created.append(payload)
        return {"data": {"id": "prospect-1"}}

    def update_prospect(self, prospect_id, payload, operation_key):
        self.updated.append((prospect_id, payload))
        return {"data": {"id": prospect_id}}

    def verify_email(self, email, operation_key):
        return {"data": {"status": "valid"}}

    def get_campaign(self, campaign_id):
        return {
            "data": {
                "id": campaign_id,
                "status": "running",
                "mailboxIds": [f"mailbox-{index}" for index in range(1, 7)],
                "dailySendLimit": 150,
                "steps": [{"stepIndex": index} for index in range(4)],
                "stopOnReply": True,
                "stopOnBounce": True,
                "stopOnUnsubscribe": True,
            }
        }

    def enroll(self, campaign_id, prospect_ids, operation_key):
        self.enrolled.append((campaign_id, prospect_ids))
        return {"data": {"enrolled": 1}}

    def suppress(self, emails, reason, operation_key):
        self.suppressed.append((emails, reason))
        return {"data": {"suppressed": len(emails)}}

    def unenroll(self, campaign_id, emails, operation_key):
        self.unenrolled.append((campaign_id, emails))
        return {"data": {"unenrolled": len(emails)}}

    def close(self):
        pass


class FakePipedrive:
    def __init__(self):
        self.people_updates = []
        self.lead_updates = []
        self.deal_updates = []
        self.archived = []
        self.activities = []

    def find_organization(self, name):
        return None

    def create_organization(self, name, owner_id, location):
        return 101

    def find_person(self, email):
        return None

    def create_person(self, name, email, organization_id, owner_id, custom_fields):
        return 202

    def find_lead_by_outreach_id(self, outreach_id):
        return None

    def create_lead(self, title, person_id, organization_id, owner_id, custom_fields):
        return "lead-303"

    def update_person(self, person_id, fields):
        self.people_updates.append((person_id, fields))
        return {}

    def update_lead(self, lead_id, fields):
        self.lead_updates.append((lead_id, fields))
        return {}

    def update_deal(self, deal_id, fields):
        self.deal_updates.append((deal_id, fields))
        return {}

    def archive_lead(self, lead_id):
        self.archived.append(lead_id)
        return {}

    def add_lead_activity(self, lead_id, subject, owner_id, note=""):
        self.activities.append((lead_id, subject, owner_id, note))
        return 404

    def close(self):
        pass


class FakeGmail:
    def __init__(self):
        self.forwarded = []

    def find_message(self, mailbox, message_ref):
        assert message_ref == "<message@example.com>"
        return "gmail-message-1"

    def forward_message(self, mailbox, message_id, to):
        self.forwarded.append((mailbox, message_id, to))
        return {"id": "forward-1"}

    def get_message(self, mailbox, message_id, format="raw"):
        return {"threadId": "thread-1", "payload": {"headers": []}}


def enabled_settings(**changes):
    deal_fields = {
        key: f"{key}_field"
        for key in (
            "aether_lead_event_id",
            "aether_outreach_id",
            "aether_contact_candidate_id",
            "warmy_prospect_id",
            "outreach_state",
            "reply_disposition",
            "reply_received_at",
            "unsubscribe_url",
            "article_url",
            "date_posted",
            "canonical_company_id",
            "event_role",
            "outreach_sequence_id",
        )
    }
    person_fields = {
        key: f"{key}_field"
        for key in (
            "aether_person_id",
            "verification_status",
            "suppressed",
            "suppression_reason",
            "unsubscribe_url",
        )
    }
    values = {
        "provider_writes_enabled": True,
        "warmy_enrollment_enabled": True,
        "campaign_start_enabled": True,
        "pipedrive_automation_ready": True,
        "email_templates_approved": True,
        "postal_address": "123 Main St, Phoenix, AZ 85001",
        "warmy_campaign_id": "campaign-1",
        "warmy_campaign_manifest_hash": campaign_manifest_hash(
            FakeWarmy().get_campaign("campaign-1")
        ),
        "warmy_api_key": "warmy-key",
        "warmy_webhook_secret": "warmy-webhook-secret",
        "warmy_mailbox_ids": tuple(f"mailbox-{index}" for index in range(1, 7)),
        "warmy_mailbox_emails": {
            f"mailbox-{index}": f"sender-{index}@aetherclean.com"
            for index in range(1, 7)
        },
        "unsubscribe_secret": "unsubscribe-secret",
        "public_base_url": "https://sales.example.com",
        "pipedrive_api_token": "pipedrive-token",
        "pipedrive_domain": "aether",
        "pipedrive_webhook_user": "hook-user",
        "pipedrive_webhook_password": "hook-pass",
        "pipedrive_deal_fields": deal_fields,
        "pipedrive_person_fields": person_fields,
        "pipedrive_person_enum_values": {
            "verification_status.pending": "201",
            "verification_status.valid": "202",
            "verification_status.invalid": "203",
            "verification_status.catch_all": "204",
            "verification_status.unknown": "205",
            "suppressed.yes": "206",
            "suppressed.no": "207",
        },
        "pipedrive_reply_disposition_values": {
            str(index): value
            for index, value in enumerate(
                (
                    "pending_review",
                    "positive",
                    "negative",
                    "out_of_office",
                    "unsubscribe",
                    "other",
                ),
                start=1,
            )
        },
        "gmail_service_account_json": "{}",
        "gmail_monitored_mailboxes": tuple(
            f"sender-{index}@aetherclean.com" for index in range(1, 7)
        ),
    }
    values.update(changes)
    return Settings(**values)


def contact_payload(outreach_id="outreach-1", email="person@example.com"):
    return {
        "run_id": "run-1",
        "lead_event_id": "event-1",
        "organization_id": "org-1",
        "person_id": "person-1",
        "outreach_id": outreach_id,
        "source_contact_candidate_id": "candidate-1",
        "source_verification_status": "verified",
        "organization_name": "Example Builders",
        "person_name": "Pat Person",
        "email": email,
        "location": "Phoenix, AZ",
        "event": "New project",
        "why_line": (
            "Hi Pat just wanted to reach out since I saw on the news that Example "
            "Builders started a new project in Phoenix. Is there any chance we could "
            "stay in touch regarding your future janitorial needs?"
        ),
    }


def test_signature_replay_and_unsubscribe_tokens_are_safe():
    body = b'{"type":"reply.received"}'
    timestamp = 1_700_000_000_000
    digest = hmac.new(
        b"secret", str(timestamp).encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    verify_warmy_signature(
        body, f"t={timestamp},v1={digest}", "", "secret", now_ms=timestamp
    )
    with pytest.raises(SignatureError, match="replay window"):
        verify_warmy_signature(
            body, f"t={timestamp},v1={digest}", "", "secret", now_ms=timestamp + 300_001
        )

    first, token_id = issue_unsubscribe_token("secret", "Person@Example.com")
    second, _ = issue_unsubscribe_token("secret", "person@example.com")
    assert first == second
    assert verify_unsubscribe_token(first, "secret") == token_id
    with pytest.raises(SignatureError):
        verify_unsubscribe_token(first + "x", "secret")


def test_webhooks_are_authenticated_and_deduplicated():
    settings = Settings(
        warmy_webhook_secret="warmy-secret",
        pipedrive_webhook_user="hook-user",
        pipedrive_webhook_password="hook-pass",
        unsubscribe_secret="unsubscribe-secret",
    )
    db = MemoryDatabase()
    app = create_app(settings, db=db, workflows=SalesWorkflows(settings, db))
    client = TestClient(app)
    body = json.dumps({"type": "reply.received", "data": {}}).encode()
    timestamp = 1_700_000_000_000
    digest = hmac.new(
        b"warmy-secret", str(timestamp).encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    headers = {
        "X-Warmy-Event-Id": "event-1",
        "X-Warmy-Event-Type": "reply.received",
        "X-Warmy-Timestamp": str(timestamp),
        "X-Warmy-Signature": f"t={timestamp},v1={digest}",
    }
    # Freeze verification time by using a current timestamp for the actual request.
    import time

    current = int(time.time() * 1000)
    headers["X-Warmy-Timestamp"] = str(current)
    headers["X-Warmy-Signature"] = "t={},v1={}".format(
        current,
        hmac.new(
            b"warmy-secret", str(current).encode() + b"." + body, hashlib.sha256
        ).hexdigest(),
    )
    assert client.post("/webhooks/warmy", content=body, headers=headers).json()[
        "accepted"
    ]
    duplicate = client.post("/webhooks/warmy", content=body, headers=headers).json()
    assert duplicate == {"accepted": False, "duplicate": True}
    queued = db.work["warmy:event:event-1"]
    assert queued.payload["event_type"] == "reply.received"
    assert client.post("/webhooks/pipedrive", json={}).status_code == 401


def test_signature_logo_asset_is_publicly_served():
    settings = Settings(unsubscribe_secret="unsubscribe-secret")
    db = MemoryDatabase()
    app = create_app(settings, db=db, workflows=SalesWorkflows(settings, db))
    response = TestClient(app).get("/assets/aether-signature-logo.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_warmy_header_event_type_is_preserved_for_header_only_envelope():
    settings = Settings(
        warmy_webhook_secret="warmy-secret",
        unsubscribe_secret="unsubscribe-secret",
    )
    db = MemoryDatabase()
    client = TestClient(
        create_app(settings, db=db, workflows=SalesWorkflows(settings, db))
    )
    body = json.dumps({"data": {"prospectId": "prospect-1"}}).encode()
    import time

    timestamp = int(time.time() * 1000)
    signature = hmac.new(
        b"warmy-secret", str(timestamp).encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    response = client.post(
        "/webhooks/warmy",
        content=body,
        headers={
            "X-Warmy-Event-Id": "event-header-only",
            "X-Warmy-Event-Type": "reply.received",
            "X-Warmy-Timestamp": str(timestamp),
            "X-Warmy-Signature": f"t={timestamp},v1={signature}",
        },
    )
    assert response.status_code == 200
    assert db.work["warmy:event:event-header-only"].payload["event_type"] == (
        "reply.received"
    )


def test_unsubscribe_endpoint_is_stable_and_idempotent():
    settings = Settings(unsubscribe_secret="unsubscribe-secret")
    db = MemoryDatabase()
    workflows = SalesWorkflows(
        settings, db, warmy=FakeWarmy(), pipedrive=FakePipedrive()
    )
    app = create_app(settings, db=db, workflows=workflows)
    token, token_id = issue_unsubscribe_token(
        settings.unsubscribe_secret, "person@example.com"
    )
    db.store_unsubscribe_token(token_id, "person@example.com")
    client = TestClient(app)
    assert client.get("/unsubscribe", params={"t": token}).status_code == 200
    assert "person@example.com" not in db.suppressions
    assert client.post("/unsubscribe", params={"t": token}).status_code == 200
    assert db.suppressions["person@example.com"]["reason"] == "unsubscribe"
    assert len([key for key in db.work if key.startswith("suppression:")]) == 1


def test_contact_sync_verification_and_enrollment_are_idempotent():
    db = MemoryDatabase()
    warmy = FakeWarmy()
    pipedrive = FakePipedrive()
    workflows = SalesWorkflows(enabled_settings(), db, warmy=warmy, pipedrive=pipedrive)
    workflows.sync_contact(contact_payload())
    mapping = db.get_mapping(outreach_id="outreach-1")
    assert mapping.pipedrive_organization_id == 101
    assert mapping.pipedrive_person_id == 202
    assert mapping.pipedrive_lead_id == "lead-303"
    assert mapping.warmy_prospect_id == "prospect-1"
    assert mapping.why_line.startswith("Hi Pat ")
    assert warmy.created[0]["unsubscribe_url"].startswith(
        "https://sales.example.com/unsubscribe"
    )
    assert "warmy:verify:outreach-1:person@example.com" in db.work

    workflows.verify_contact(
        {"outreach_id": "outreach-1", "email": "person@example.com"}
    )
    assert (
        db.get_mapping(outreach_id="outreach-1").verification_status
        == VerificationStatus.VALID
    )
    workflows.enroll_contact({"outreach_id": "outreach-1"})
    workflows.enroll_contact({"outreach_id": "outreach-1"})
    assert warmy.enrolled == [("campaign-1", ["prospect-1"])]


def test_warmy_prospect_is_reused_by_normalized_email():
    db = MemoryDatabase()
    warmy = FakeWarmy()
    workflows = SalesWorkflows(
        enabled_settings(), db, warmy=warmy, pipedrive=FakePipedrive()
    )
    workflows.sync_contact(contact_payload("outreach-1", "same@example.com"))
    second = contact_payload("outreach-2", "SAME@example.com")
    second.update(lead_event_id="event-2", source_contact_candidate_id="candidate-2")
    workflows.sync_contact(second)
    assert len(warmy.created) == 1
    assert db.get_mapping(outreach_id="outreach-1").warmy_prospect_id == "prospect-1"
    assert db.get_mapping(outreach_id="outreach-2").warmy_prospect_id == "prospect-1"


def test_corrected_contact_revision_updates_mapping_and_requeues(tmp_path):
    header = (
        "business_name,person,email,lead_event_id,organization_id,person_id,"
        "contact_candidate_id,verification_status,provider,run_id\n"
    )
    first_path = tmp_path / "first.csv"
    first_path.write_text(
        header + "Acme,Alex One,old@example.com,event-1,org-1,person-1,"
        "candidate-old,unknown,model,run-1\n",
        encoding="utf-8",
    )
    second_path = tmp_path / "second.csv"
    second_path.write_text(
        header + "Acme,Alex One,new@example.com,event-1,org-1,person-1,"
        "candidate-new,verified,apollo,run-1\n",
        encoding="utf-8",
    )
    db = MemoryDatabase()
    assert enqueue_contacts(db, first_path, "run-1") == (1, 1)
    assert enqueue_contacts(db, second_path, "run-1") == (1, 1)
    assert len([key for key in db.work if key.startswith("scout:contact:")]) == 2

    workflows = SalesWorkflows(
        enabled_settings(), db, warmy=FakeWarmy(), pipedrive=FakePipedrive()
    )
    workflows.sync_contact(contacts_from_csv(first_path, "run-1")[0].model_dump())
    workflows.sync_contact(contacts_from_csv(second_path, "run-1")[0].model_dump())
    mapping = db.get_mapping(
        outreach_id=contacts_from_csv(first_path, "run-1")[0].outreach_id
    )
    assert mapping.email == "new@example.com"
    assert mapping.source_contact_candidate_id == "candidate-new"
    assert mapping.source_verification_status == "verified"
    assert mapping.source_provider == "apollo"
    assert db.is_suppressed("old@example.com")


def test_existing_pipedrive_person_is_reconciled_with_unsubscribe_fields():
    class ExistingPersonPipedrive(FakePipedrive):
        def find_person(self, email):
            return 909

    db = MemoryDatabase()
    pipedrive = ExistingPersonPipedrive()
    SalesWorkflows(
        enabled_settings(), db, warmy=FakeWarmy(), pipedrive=pipedrive
    ).sync_contact(contact_payload())
    person_id, fields = pipedrive.people_updates[0]
    assert person_id == 909
    assert fields["aether_person_id_field"] == "person-1"
    assert fields["unsubscribe_url_field"].startswith("https://sales.example.com/")
    assert fields["suppressed_field"] == 207


def test_suppression_updates_every_mapping_for_an_email():
    db = MemoryDatabase()
    for index in (1, 2):
        db.upsert_mapping(
            MappingRecord(
                outreach_id=f"outreach-{index}",
                email="same@example.com",
                pipedrive_person_id=22,
                pipedrive_lead_id=f"lead-{index}",
                pipedrive_deal_id=100 + index,
                warmy_campaign_id="campaign-1",
            )
        )
    warmy, pipedrive = FakeWarmy(), FakePipedrive()
    workflows = SalesWorkflows(enabled_settings(), db, warmy=warmy, pipedrive=pipedrive)
    workflows.sync_suppression(
        {"email": "same@example.com", "reason": "unsubscribe", "source": "link"}
    )
    assert warmy.suppressed == [(["same@example.com"], "unsubscribe")]
    assert warmy.unenrolled == [("campaign-1", ["same@example.com"])]
    assert {item[0] for item in pipedrive.lead_updates} == {"lead-1", "lead-2"}
    assert {item[0] for item in pipedrive.deal_updates} == {101, 102}


def test_warmy_reply_is_forwarded_and_creates_jordan_review_task():
    db = MemoryDatabase()
    db.upsert_mapping(
        MappingRecord(
            outreach_id="outreach-1",
            email="person@example.com",
            warmy_prospect_id="prospect-1",
            pipedrive_lead_id="lead-1",
        )
    )
    warmy, pipedrive, gmail = FakeWarmy(), FakePipedrive(), FakeGmail()
    settings = enabled_settings(
        warmy_mailbox_emails={"mailbox-1": "sender@aetherclean.com"}
    )
    workflows = SalesWorkflows(
        settings, db, warmy=warmy, pipedrive=pipedrive, gmail=gmail
    )
    workflows.handle_warmy_event(
        {
            "event_id": "event-1",
            "type": "reply.received",
            "data": {
                "prospectId": "prospect-1",
                "prospectEmail": "person@example.com",
                "mailboxId": "mailbox-1",
                "messageId": "<message@example.com>",
                "subject": "Re: Project",
                "receivedAt": "2026-08-30T15:00:00Z",
            },
        }
    )
    assert gmail.forwarded == [
        ("sender@aetherclean.com", "gmail-message-1", "jw@aetherclean.com")
    ]
    assert pipedrive.activities[0][0:3] == ("lead-1", "Review Warmy reply", 11380767)
    mapping = db.get_mapping(outreach_id="outreach-1")
    assert mapping.reply_disposition == ReplyDisposition.PENDING_REVIEW
    assert mapping.gmail_thread_id == "thread-1"


def test_warmy_reply_creates_review_task_when_gmail_forwarding_is_deferred():
    db = MemoryDatabase()
    db.upsert_mapping(
        MappingRecord(
            outreach_id="outreach-1",
            email="person@example.com",
            warmy_prospect_id="prospect-1",
            pipedrive_lead_id="lead-1",
        )
    )
    pipedrive = FakePipedrive()
    workflows = SalesWorkflows(
        enabled_settings(
            gmail_reply_forwarding_enabled=False,
            gmail_service_account_json="",
            gmail_monitored_mailboxes=(),
        ),
        db,
        warmy=FakeWarmy(),
        pipedrive=pipedrive,
    )
    workflows.handle_warmy_event(
        {
            "event_id": "event-1",
            "type": "reply.received",
            "data": {
                "prospectId": "prospect-1",
                "prospectEmail": "person@example.com",
                "mailboxId": "mailbox-1",
                "subject": "Re: Project",
                "receivedAt": "2026-08-30T15:00:00Z",
            },
        }
    )
    assert "available in the WarmySender Inbox" in pipedrive.activities[0][3]
    assert db.get_mapping(outreach_id="outreach-1").gmail_thread_id is None


def test_warmy_event_for_foreign_campaign_is_ignored_before_mapping_lookup():
    db = MemoryDatabase()

    def unexpected_lookup(*args, **kwargs):
        raise AssertionError("foreign campaign event must not inspect local mappings")

    db.get_state = unexpected_lookup
    db.get_mapping = unexpected_lookup
    db.get_mappings_by_email = unexpected_lookup
    workflows = SalesWorkflows(
        enabled_settings(warmy_campaign_id="campaign-1"),
        db,
        warmy=FakeWarmy(),
        pipedrive=FakePipedrive(),
    )

    workflows.handle_warmy_event(
        {
            "event_id": "foreign-reply-1",
            "type": "reply.received",
            "data": {
                "campaignId": "campaign-other",
                "prospectId": "prospect-other",
                "prospectEmail": "other@example.com",
            },
        }
    )

    assert not db.suppressions


def test_positive_conversion_is_blocked_until_pipedrive_automation_is_ready():
    db = MemoryDatabase()
    db.upsert_mapping(
        MappingRecord(
            outreach_id="outreach-1",
            email="person@example.com",
            pipedrive_lead_id="lead-1",
        )
    )
    workflows = SalesWorkflows(enabled_settings(pipedrive_automation_ready=False), db)
    with pytest.raises(ActivationBlocked, match="PIPEDRIVE_AUTOMATION_READY"):
        workflows.convert_lead({"outreach_id": "outreach-1"})


@pytest.mark.parametrize(
    ("option_id", "expected", "work_key"),
    [
        ("2", ReplyDisposition.POSITIVE, "pipedrive:convert:lead-1"),
        (
            "3",
            ReplyDisposition.NEGATIVE,
            "suppression:person@example.com:negative_reply",
        ),
    ],
)
def test_pipedrive_v2_nested_dispositions_are_applied(option_id, expected, work_key):
    db = MemoryDatabase()
    db.upsert_mapping(
        MappingRecord(
            outreach_id="outreach-1",
            email="person@example.com",
            pipedrive_lead_id="lead-1",
        )
    )
    workflows = SalesWorkflows(
        enabled_settings(), db, warmy=FakeWarmy(), pipedrive=FakePipedrive()
    )
    field_key = workflows.settings.pipedrive_deal_fields["reply_disposition"]
    workflows.handle_pipedrive_event(
        {
            "data": {
                "id": "lead-1",
                "custom_fields": {field_key: {"type": "enum", "id": int(option_id)}},
            },
            "meta": {"entity": "lead", "entity_id": "lead-1"},
        }
    )
    assert db.get_mapping(outreach_id="outreach-1").reply_disposition == expected
    assert work_key in db.work


def test_enrollment_requires_complete_activation_and_live_campaign():
    db = MemoryDatabase()
    db.upsert_mapping(
        MappingRecord(
            outreach_id="outreach-1",
            email="person@example.com",
            warmy_prospect_id="prospect-1",
            verification_status=VerificationStatus.VALID,
        )
    )
    workflows = SalesWorkflows(
        enabled_settings(campaign_start_enabled=False),
        db,
        warmy=FakeWarmy(),
        pipedrive=FakePipedrive(),
    )
    with pytest.raises(ActivationBlocked, match="CAMPAIGN_START_ENABLED"):
        workflows.enroll_contact({"outreach_id": "outreach-1"})


def test_scout_bridge_emits_one_stable_job_per_person(tmp_path):
    csv_path = tmp_path / "contacts.csv"
    csv_path.write_text(
        "business_name,event,location,date_posted,person,email,link,lead_event_id,"
        "organization_id,person_id,contact_candidate_id,verification_status,"
        "verification_reason,provider,run_id,record_status\n"
        "Acme,Project,Phoenix,2026-08-29,Alex One,alex@example.com,https://example.com/a,"
        "event-1,org-1,person-1,candidate-model-1,verified,mx_valid,model,run-1,valid\n"
        "Acme,Project,Phoenix,2026-08-29,Blair Two,blair@example.com,https://example.com/a,"
        "event-1,org-1,person-2,candidate-apollo-1,unknown,syntax_valid,apollo,run-1,valid\n",
        encoding="utf-8",
    )
    first = contacts_from_csv(csv_path, "run-1")
    second = contacts_from_csv(csv_path, "run-1")
    assert len(first) == 2
    assert [item.outreach_id for item in first] == [item.outreach_id for item in second]
    assert [item.source_contact_candidate_id for item in first] == [
        "candidate-model-1",
        "candidate-apollo-1",
    ]
    assert [item.is_primary for item in first] == [True, False]
    db = MemoryDatabase()
    assert enqueue_contacts(db, csv_path, "run-1") == (2, 2)
    assert enqueue_contacts(db, csv_path, "run-1") == (0, 2)


def test_v2_outreach_identity_survives_contact_enrichment_changes(tmp_path):
    header = (
        "business_name,event,location,date_posted,person,email,link,lead_event_id,"
        "organization_id,person_id,contact_candidate_id,verification_status,run_id\n"
    )
    first_path = tmp_path / "first.csv"
    first_path.write_text(
        header
        + "Acme,Project,Phoenix,2026-08-29,Alex One,old@example.com,https://example.com/a,"
        "event-1,org-1,person-1,candidate-model,unknown,run-1\n",
        encoding="utf-8",
    )
    second_path = tmp_path / "second.csv"
    second_path.write_text(
        header
        + "Acme,Project,Phoenix,2026-08-29,Alex One,new@example.com,https://example.com/a,"
        "event-1,org-1,person-1,candidate-apollo,verified,run-1\n",
        encoding="utf-8",
    )
    first = contacts_from_csv(first_path, "run-1")[0]
    second = contacts_from_csv(second_path, "run-1")[0]
    assert first.outreach_id == second.outreach_id
    assert first.source_contact_candidate_id != second.source_contact_candidate_id
    assert first.email != second.email


def test_v2_contacts_reject_a_mismatched_run_id(tmp_path):
    path = tmp_path / "contacts.csv"
    path.write_text(
        "business_name,person,email,lead_event_id,organization_id,person_id,run_id\n"
        "Acme,Alex One,alex@example.com,event-1,org-1,person-1,another-run\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="does not match"):
        contacts_from_csv(path, "run-1")


def test_v2_pipeline_handoff_uses_exported_path_and_real_run_id(monkeypatch):
    module = _load_scout_pipeline(monkeypatch)
    calls = []
    result = SimpleNamespace(
        run_id="1d1cb1d5-6212-4bd2-82f4-9c4dc26be7f0",
        paths={"sales_handoff": "/persistent/results/2026-08-30/sales_handoff.json"},
    )
    module.enqueue_sales_handoff(
        result,
        run=lambda args, **kwargs: calls.append((args, kwargs)),
    )
    args, kwargs = calls[0]
    assert args[:6] == [
        "uv",
        "run",
        "--project",
        str(ROOT),
        "python",
        "-m",
    ]
    assert args[-2:] == [
        "enqueue-handoff",
        "/persistent/results/2026-08-30/sales_handoff.json",
    ]
    assert kwargs == {"cwd": ROOT, "check": True}


def test_v2_pipeline_handoff_runs_in_project_environment(tmp_path, monkeypatch):
    module = _load_scout_pipeline(monkeypatch)
    handoff_path = tmp_path / "sales_handoff.json"
    handoff = SalesHandoff(
        schema_version=1,
        protocol_version=HANDOFF_PROTOCOL_VERSION,
        run_id="run-live",
        companies=[
            CompanySync(company_id="company-1", canonical_name="Acme")
        ],
        lead_events=[
            LeadEventSync(
                run_id="run-live",
                lead_event_id="event-1",
                company_id="company-1",
                organization_name="Acme",
                event_role=EventRole.ANCHOR,
                event="Opened a Phoenix location",
                score=88,
                confidence="high",
                record_status="valid",
                actionable_route=True,
                crm_eligible=True,
            )
        ],
        recipients=[],
        sequences=[],
        content_hash="pending",
    )
    handoff = handoff.model_copy(update={"content_hash": handoff_content_hash(handoff)})
    handoff_path.write_text(handoff.model_dump_json(indent=2), encoding="utf-8")
    database_path = tmp_path / "handoff.sqlite"
    monkeypatch.setenv("AETHER_SALES_DB_PATH", str(database_path))
    module.enqueue_sales_handoff(
        SimpleNamespace(
            run_id="run-live", paths={"sales_handoff": str(handoff_path)}
        )
    )
    claimed = Database(database_path).claim_work("test", 10, 60)
    assert len(claimed) == 1
    assert claimed[0].kind == "scout.lead.sync"
    assert claimed[0].payload["lead_event"]["lead_event_id"] == "event-1"


def test_campaign_manifest_cannot_pass_with_placeholder_copy(tmp_path):
    source = ROOT / "config" / "aether_campaign.yaml.example"
    settings = enabled_settings(
        email_templates_approved=True,
        postal_address="123 Main St, Phoenix, AZ 85001",
        warmy_mailbox_ids=tuple(f"mailbox-{index}" for index in range(1, 7)),
    )
    with pytest.raises(ActivationBlocked, match="placeholder"):
        load_campaign(source, settings)

    approved = tmp_path / "campaign.yaml"
    approved.write_text(source.read_text().replace(COPY_PLACEHOLDER, "Approved copy"))
    manifest = load_campaign(approved, settings)
    assert [step.delayDays for step in manifest.steps] == [0, 3, 4, 7]
    assert manifest.mailboxIds == [f"mailbox-{index}" for index in range(1, 7)]
    assert "123 Main St" in manifest.steps[0].bodyText

    missing_postal = tmp_path / "missing-postal.yaml"
    missing_postal.write_text(
        source.read_text()
        .replace(COPY_PLACEHOLDER, "Approved copy")
        .replace("{{AETHER_POSTAL_ADDRESS}}", "Address omitted", 1)
    )
    with pytest.raises(ActivationBlocked, match="postal-address"):
        load_campaign(missing_postal, settings)


def test_sqlite_database_persists_queue_and_service_state(tmp_path):
    path = tmp_path / "sales.sqlite"
    db = Database(path)
    assert db.healthcheck()
    assert db.enqueue_work("test", "dedupe-1", {"value": 1})
    assert not db.enqueue_work("test", "dedupe-1", {"value": 2})

    claimed = db.claim_work("worker-1", 10, 60)
    assert [(item.kind, item.payload, item.attempt_count) for item in claimed] == [
        ("test", {"value": 1}, 1)
    ]
    with db.connection() as conn:
        conn.execute(
            "UPDATE work_items SET last_error='previous failure' WHERE id=?",
            (claimed[0].id,),
        )
    db.complete_work(claimed[0].id, "worker-1")
    assert not db.claim_work("worker-2", 10, 60)
    with db.connection() as conn:
        completed = conn.execute(
            "SELECT status, last_error FROM work_items WHERE id=?",
            (claimed[0].id,),
        ).fetchone()
    assert dict(completed) == {"status": "completed", "last_error": ""}

    db.upsert_mapping(
        MappingRecord(
            outreach_id="outreach-1",
            email="person@example.com",
            pipedrive_lead_id="lead-1",
        )
    )
    db.update_mapping("outreach-1", warmy_prospect_id="prospect-1")
    mapping = Database(path).get_mapping(outreach_id="outreach-1")
    assert mapping.pipedrive_lead_id == "lead-1"
    assert mapping.warmy_prospect_id == "prospect-1"
    db.upsert_mapping(
        MappingRecord(
            outreach_id="outreach-2",
            email="person@example.com",
            warmy_prospect_id="prospect-1",
        )
    )
    assert len(Database(path).get_mappings_by_email("person@example.com")) == 2

    assert db.suppress("Person@Example.com", "manual", "test")
    assert not db.suppress("person@example.com", "unsubscribe", "test")
    assert Database(path).is_suppressed("person@example.com")


def test_sqlite_webhook_and_provider_operation_deduplication(tmp_path):
    db = Database(tmp_path / "sales.sqlite")
    payload = {"type": "reply.received", "data": {}}
    assert db.accept_webhook(
        "warmy", "event-1", "reply.received", payload, "hash", signature_valid=True
    )
    assert not db.accept_webhook(
        "warmy", "event-1", "reply.received", payload, "hash", signature_valid=True
    )
    item = db.claim_work("worker-1", 1, 60)[0]
    assert item.kind == "warmy.event"

    assert db.claim_operation("warmy", "create:contact-1", {"email": "x@y.com"})
    db.complete_operation(
        "warmy",
        "create:contact-1",
        {"data": {"id": "prospect-1"}},
        external_id="prospect-1",
    )
    assert not db.claim_operation("warmy", "create:contact-1", {"email": "x@y.com"})
    operation = db.get_operation("warmy", "create:contact-1")
    assert operation["status"] == "completed"
    assert operation["response_payload"]["data"]["id"] == "prospect-1"

    assert db.claim_operation(
        "warmy", "stale:contact-1", {"email": "x@y.com"}, owner="worker-1"
    )
    with db.connection() as conn:
        conn.execute(
            "UPDATE provider_operations SET lease_until=? WHERE operation_key=?",
            ("2000-01-01T00:00:00+00:00", "stale:contact-1"),
        )
    assert db.claim_operation(
        "warmy", "stale:contact-1", {"email": "x@y.com"}, owner="worker-2"
    )


def test_activation_blocked_work_is_deferred_without_retry_exhaustion():
    class BlockedWorkflows:
        def handle(self, item):
            raise ActivationBlocked("still gated")

        def close(self):
            pass

    db = MemoryDatabase()
    db.enqueue_work("warmy.enroll", "blocked", {"outreach_id": "one"})
    assert (
        run_once(Settings(worker_batch_size=1), db=db, workflows=BlockedWorkflows())
        == 0
    )
    assert not db.completed
    assert db.work["blocked"].attempt_count == 0


def test_provider_clients_use_current_api_contracts_and_write_gate():
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        if request.url.path.endswith("/dealFields"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"field_code": "field-1", "field_name": "Aether Field"},
                },
            )
        return httpx.Response(201, json={"data": {"id": "prospect-1"}})

    transport = httpx.MockTransport(handler)
    disabled_http = httpx.Client(
        transport=transport, base_url="https://warmysender.com/api/v1/"
    )
    disabled = WarmyClient(Settings(provider_writes_enabled=False), disabled_http)
    with pytest.raises(ActivationBlocked):
        disabled.create_prospect(contact_payload(), "operation-1")
    assert not calls

    pipedrive_http = httpx.Client(
        transport=transport, base_url="https://example.pipedrive.com/api/"
    )
    pipedrive = PipedriveClient(
        Settings(provider_writes_enabled=True, pipedrive_domain="example"),
        pipedrive_http,
    )
    field = pipedrive.create_field("dealFields", "Aether Field", "varchar")
    assert field["field_code"] == "field-1"
    assert calls[-1].url.path == "/api/v2/dealFields"
    assert json.loads(calls[-1].content) == {
        "field_name": "Aether Field",
        "field_type": "varchar",
    }


def test_gmail_writes_require_global_provider_gate():
    gmail = object.__new__(GmailClient)
    gmail.settings = Settings(provider_writes_enabled=False)

    with pytest.raises(ActivationBlocked, match="provider writes are disabled"):
        gmail.forward_message("sender@example.com", "message-1", "owner@example.com")
    with pytest.raises(ActivationBlocked, match="provider writes are disabled"):
        gmail.send_text(
            "sender@example.com",
            "owner@example.com",
            "Subject",
            "Body",
        )
