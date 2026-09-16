import hashlib
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from integration.config import ActivationBlocked
from integration.database import Database
from integration.models import ApprovalBatch
from integration.send_gate import (
    EXPECTED_ADDRESS,
    _FooterMarkupParser,
    _mapping_hash,
    build_frozen_manifest,
    validate_received_render_evidence,
    validate_live_literal_campaign,
    validate_frozen_manifest,
)


EASTERN = ZoneInfo("Etc/GMT+5")


def _messages(count=1, first_send_at=None):
    first_send_at = first_send_at or datetime(2026, 9, 16, 8, tzinfo=EASTERN)
    rows = []
    for recipient_number in range(count):
        recipient_id = f"recipient-{recipient_number}"
        for step, delay in enumerate((0, 7, 14)):
            property_name = f"Property {recipient_number}"
            fact = f"{property_name} opened in Phoenix"
            why = f"Hi Jane — {fact}."
            body = (
                f"{why}\n\n"
                "This is a commercial message from Aether Facility Services.\n\n"
                "Happy to provide a proposal when you're ready.\n\n"
                "Jordan Whitehurst, Partner\n"
                "Aether Facility Services, LLC\n"
                "O: (602) 612-6393\n"
                "M: (813) 992-0858\n"
                f"{EXPECTED_ADDRESS}\n\n"
                "Unsubscribe: https://example.com/unsubscribe"
            )
            rows.append(
                {
                    "sequence_id": f"sequence-{recipient_number}",
                    "recipient_id": recipient_id,
                    "recipient_email": f"jane{recipient_number}@example.com",
                    "first_name": "Jane",
                    "company": f"Company {recipient_number}",
                    "property_name": property_name,
                    "sender_name": "Jordan Whitehurst",
                    "sender_email": "jordan@aetherclean.com",
                    "step_index": step,
                    "scheduled_at": first_send_at + timedelta(days=delay),
                    "subject": f"Jane, about {property_name}",
                    "body_text": body,
                    "body_html": f"<p>{body.replace(chr(10), '<br>')}</p>",
                    "why_line": why,
                    "source_facts": [fact],
                    "source_urls": ["https://example.com/source"],
                    "source_facts_reviewed": True,
                }
            )
    return rows


def _manifest(count=1):
    return build_frozen_manifest(
        campaign_id="campaign-1",
        campaign_manifest_hash="campaign-hash",
        messages=[row for row in _messages(count) if row["step_index"] == 0],
        first_send_at=datetime(2026, 9, 16, 8, tzinfo=EASTERN),
    )


def test_literal_manifest_is_hash_complete_and_valid():
    manifest = _manifest()
    result = validate_frozen_manifest(
        manifest,
        expected_campaign_id="campaign-1",
        expected_campaign_manifest_hash="campaign-hash",
        expected_sequence_ids={"sequence-0"},
    )
    assert result["message_count"] == 1
    assert result["route"] == "literal_frozen"


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("sender_name", "Aether Facility Services", "sender name"),
        ("subject", "Jane, about Company 0", "actual property"),
        ("body_text", "{{firstName}}", "template token"),
        ("body_text", "Sent by Codex on Jon Schack's behalf.", "disclosure"),
        ("body_text", "bad body", "initial body"),
    ],
)
def test_literal_manifest_rejects_unsafe_payload(field, value, error):
    rows = [row for row in _messages() if row["step_index"] == 0]
    rows[0][field] = value
    if field == "property_name":
        rows[0]["subject"] = "Jane, about Property 0"
    manifest = build_frozen_manifest(
        campaign_id="campaign-1",
        campaign_manifest_hash="campaign-hash",
        messages=rows,
        first_send_at=datetime(2026, 9, 16, 8, tzinfo=EASTERN),
    )
    with pytest.raises(ActivationBlocked, match=error):
        validate_frozen_manifest(manifest)


def test_company_branding_inside_property_name_is_not_overexcluded():
    row = [item for item in _messages() if item["step_index"] == 0][0]
    row["company"] = "Acme"
    row["property_name"] = "Acme Peoria Plant Expansion"
    row["subject"] = "Jane, about Acme Peoria Plant Expansion"
    row["body_text"] = row["body_text"].replace("Property 0", row["property_name"])
    row["body_html"] = row["body_html"].replace("Property 0", row["property_name"])
    row["why_line"] = row["why_line"].replace("Property 0", row["property_name"])
    row["source_facts"] = [row["source_facts"][0].replace("Property 0", row["property_name"])]
    manifest = build_frozen_manifest(
        campaign_id="campaign-1", campaign_manifest_hash="campaign-hash", messages=[row],
        first_send_at=datetime(2026, 9, 16, 8, tzinfo=EASTERN),
    )
    assert validate_frozen_manifest(manifest)["status"] == "verified"


def test_reviewed_property_context_allows_concise_body_wording():
    row = [item for item in _messages() if item["step_index"] == 0][0]
    row["property_name"] = "Amkor Peoria Plant Expansion"
    row["company"] = "Amkor"
    row["subject"] = "Jane, about Amkor Peoria Plant Expansion"
    row["body_text"] = row["body_text"].replace("Property 0", "Amkor is expanding its Peoria semiconductor plant")
    row["body_html"] = row["body_html"].replace("Property 0", "Amkor is expanding its Peoria semiconductor plant")
    row["why_line"] = row["why_line"].replace("Property 0", "Amkor is expanding its Peoria semiconductor plant")
    row["source_facts"] = [row["source_facts"][0].replace("Property 0", "Amkor is expanding its Peoria semiconductor plant")]
    row["property_context"] = "Peoria semiconductor plant"
    row["property_context_reviewed"] = True
    manifest = build_frozen_manifest(
        campaign_id="campaign-1", campaign_manifest_hash="campaign-hash", messages=[row],
        first_send_at=datetime(2026, 9, 16, 8, tzinfo=EASTERN),
    )
    assert validate_frozen_manifest(manifest)["status"] == "verified"


def test_provider_rendered_canary_never_authorizes_send():
    manifest = build_frozen_manifest(
        campaign_id="campaign-1",
        campaign_manifest_hash="campaign-hash",
        messages=[row for row in _messages() if row["step_index"] == 0],
        approved_route="provider_rendered_canary",
        first_send_at=datetime(2026, 9, 16, 8, tzinfo=EASTERN),
    )
    with pytest.raises(ActivationBlocked, match="canaries are evidence only"):
        validate_frozen_manifest(manifest)


def test_unrelated_workspace_address_is_rejected_from_approved_signature():
    rows = [row for row in _messages() if row["step_index"] == 0]
    rows[0]["body_text"] = rows[0]["body_text"].replace(
        "Unsubscribe:", "2390 E Camelback Blvd Ste 130, Phoenix, AZ 85016\nUnsubscribe:"
    )
    manifest = build_frozen_manifest(
        campaign_id="campaign-1", campaign_manifest_hash="campaign-hash", messages=rows,
        first_send_at=datetime(2026, 9, 16, 8, tzinfo=EASTERN),
    )
    with pytest.raises(ActivationBlocked, match="conflicting physical address"):
        validate_frozen_manifest(manifest)


def test_followups_use_actual_prior_send_time_and_fresh_current_step():
    row = [item for item in _messages() if item["step_index"] == 1][0]
    prior = datetime(2026, 9, 9, 8, tzinfo=EASTERN)
    row.update(
        prior_sent_at=prior,
        prior_message_id="provider-message-0",
        scheduled_at=prior + timedelta(days=7),
        why_line="",
        source_facts=[],
        source_urls=[],
    )
    manifest = build_frozen_manifest(
        campaign_id="campaign-1", campaign_manifest_hash="campaign-hash", messages=[row],
        first_send_at=datetime(2026, 9, 16, 8, tzinfo=EASTERN),
    )
    assert validate_frozen_manifest(manifest)["message_count"] == 1
    row["scheduled_at"] = prior + timedelta(days=14)
    manifest = build_frozen_manifest(
        campaign_id="campaign-1", campaign_manifest_hash="campaign-hash", messages=[row],
        first_send_at=datetime(2026, 9, 23, 8, tzinfo=EASTERN),
    )
    with pytest.raises(ActivationBlocked, match="7-day follow-up"):
        validate_frozen_manifest(manifest)


def test_followup_rounds_prior_0803_send_to_next_fixed_0800_slot():
    row = [item for item in _messages() if item["step_index"] == 1][0]
    prior = datetime(2026, 9, 9, 8, 3, tzinfo=EASTERN)
    row.update(
        prior_sent_at=prior,
        prior_message_id="provider-message-0",
        scheduled_at=datetime(2026, 9, 17, 8, tzinfo=EASTERN),
        why_line="",
        source_facts=[],
        source_urls=[],
    )
    manifest = build_frozen_manifest(
        campaign_id="campaign-1", campaign_manifest_hash="campaign-hash", messages=[row],
        first_send_at=datetime(2026, 9, 17, 8, tzinfo=EASTERN),
    )
    assert validate_frozen_manifest(manifest)["status"] == "verified"


def test_prior_send_binding_requires_actual_database_record(tmp_path):
    db = Database(tmp_path / "sales.sqlite")
    prior = datetime(2026, 9, 9, 8, 3, tzinfo=EASTERN)
    assert not db.has_sent_message("provider-message-0", "recipient-0", prior)
    db.record_sent_message("provider-message-0", "hash-0", "recipient-0", prior, provider="gmail")
    assert db.has_sent_message("provider-message-0", "recipient-0", prior)
    assert not db.has_sent_message("provider-message-0", "recipient-1", prior)


def test_activation_release_window_allows_heartbeat_jitter_but_not_early_or_late():
    manifest = _manifest()
    with pytest.raises(ActivationBlocked, match="release window"):
        validate_frozen_manifest(
            manifest,
            now=datetime(2026, 9, 16, 12, 59, tzinfo=UTC),
            require_future=True,
        )
    assert validate_frozen_manifest(
        manifest,
        now=datetime(2026, 9, 16, 13, 0, 1, tzinfo=UTC),
        require_future=True,
    )["status"] == "verified"
    with pytest.raises(ActivationBlocked, match="release window"):
        validate_frozen_manifest(
            manifest,
            now=datetime(2026, 9, 16, 14, 0, tzinfo=UTC),
            require_future=True,
        )


def test_total_daily_cap_is_not_per_recipient():
    # Three messages per day for each recipient would be six sends on each
    # day only if steps were scheduled together; shift all rows to same date
    # to prove the aggregate cap rather than a per-recipient allowance.
    rows = [row for row in _messages(count=6) if row["step_index"] == 0]
    for row in rows:
        row["scheduled_at"] = datetime(2026, 9, 16, 8, tzinfo=EASTERN)
    manifest = build_frozen_manifest(
        campaign_id="campaign-1", campaign_manifest_hash="campaign-hash", messages=rows,
        first_send_at=datetime(2026, 9, 16, 8, tzinfo=EASTERN),
    )
    with pytest.raises(ActivationBlocked, match="daily send cap"):
        validate_frozen_manifest(manifest)


def test_database_persists_and_binds_the_frozen_approval_to_recipient(tmp_path):
    from tests.test_sales_handoff_v2 import _seed

    db = Database(tmp_path / "sales.sqlite")
    _seed(db)
    db.update_recipient(
        "recipient-1", verification_status="valid", warmy_prospect_id="prospect-1"
    )
    rows = [row for row in _messages() if row["step_index"] == 0]
    for row in rows:
        row.update(sequence_id="sequence-1", recipient_id="recipient-1", recipient_email="jane1@acme.example")
    manifest = build_frozen_manifest(
        campaign_id="campaign-1", campaign_manifest_hash="campaign-hash", messages=rows,
        first_send_at=datetime(2026, 9, 16, 8, tzinfo=EASTERN),
    )
    now = datetime(2026, 9, 16, 12, 30, tzinfo=UTC)
    db.save_approval_batch(
        ApprovalBatch(
            batch_id="batch-1",
            campaign_id="campaign-1",
            campaign_manifest_hash="campaign-hash",
            sequence_ids=["sequence-1"],
            merge_hashes={"sequence-1": "merge-sequence-1"},
            maximum_recipient_count=1,
            approved_by="operator",
            approved_at=now,
            expires_at=now + timedelta(hours=1),
            render_manifest=manifest,
        )
    )
    assert db.valid_frozen_send_approval("campaign-1", "campaign-hash")["status"] == "verified"
    assert not db.valid_approval_for_sequence(
        "sequence-1",
        campaign_id="campaign-1",
        campaign_manifest_hash="campaign-hash",
        require_rendered=True,
    )


def test_received_evidence_requires_both_provider_mime_bodies(tmp_path):
    db = Database(tmp_path / "sales.sqlite")
    from tests.test_sales_handoff_v2 import _seed

    _seed(db)
    db.update_recipient("recipient-1", verification_status="valid", warmy_prospect_id="prospect-1")
    row = [item for item in _messages() if item["step_index"] == 0][0]
    row.update(sequence_id="sequence-1", recipient_id="recipient-1", recipient_email="jane1@acme.example", mailbox_id="mailbox-1")
    manifest = build_frozen_manifest(
        campaign_id="campaign-1", campaign_manifest_hash="campaign-hash", messages=[row],
        first_send_at=datetime(2026, 9, 16, 8, tzinfo=EASTERN),
    )
    now = datetime(2026, 9, 16, 12, 30, tzinfo=UTC)
    db.save_approval_batch(ApprovalBatch(
        batch_id="batch-1", campaign_id="campaign-1", campaign_manifest_hash="campaign-hash",
        sequence_ids=["sequence-1"], merge_hashes={"sequence-1": "merge-sequence-1"},
        maximum_recipient_count=1, approved_by="operator", approved_at=now,
        expires_at=now + timedelta(hours=24), render_manifest=manifest,
    ))
    message = manifest.messages[0]
    evidence = {
        "campaign_id": "campaign-1", "manifest_hash": manifest.manifest_hash,
        "observed_at": now.isoformat(), "source": "computer_use",
        "source_reference": "warmy-ui-message-1", "reviewed_by": "trusted-reviewer",
        "dynamic_merge_route": False,
        "used_literal_payload": True,
        "footer_policy": {
            "status": "approved", "mode": "none", "address": EXPECTED_ADDRESS,
            "unsubscribe_domain": "example.com", "reviewed_by": "fixture-reviewer",
            "reviewed_at": now.isoformat(),
        },
        "samples": [{
            "message_hash": message.content_hash, "step_index": 0,
            "sender_name": message.sender_name, "received_message_id": "message-1",
            "sender_email": message.sender_email, "recipient_id": message.recipient_id,
            "mailbox_id": message.mailbox_id or "mailbox-1",
            "actual_to": message.recipient_email,
            "expected_subject": message.subject, "actual_subject": message.subject,
            "expected_body_text": message.body_text, "actual_body_text": message.body_text,
            "expected_body_html": message.body_html, "actual_body_html": message.body_html,
        }],
    }
    db.save_render_evidence("campaign-1", evidence)
    assert db.valid_frozen_send_approval(
        "campaign-1", "campaign-hash", now=datetime(2026, 9, 16, 13, 0, 1, tzinfo=UTC), require_future=True
    )["status"] == "verified"
    evidence["samples"][0]["actual_body_html"] = None
    with pytest.raises(ActivationBlocked, match="approved literal payload"):
        validate_received_render_evidence(evidence, manifest)


def test_received_evidence_requires_explicit_footer_policy():
    manifest = _manifest()
    message = manifest.messages[0]
    now = datetime.now(UTC)
    evidence = {
        "campaign_id": manifest.campaign_id, "manifest_hash": manifest.manifest_hash,
        "observed_at": now.isoformat(), "source": "gmail", "source_reference": "synthetic-gmail",
        "reviewed_by": "fixture-reviewer", "dynamic_merge_route": False,
        "used_literal_payload": True,
        "samples": [{
            "message_hash": message.content_hash, "step_index": message.step_index,
            "received_message_id": "synthetic-message", "sender_name": message.sender_name,
            "sender_email": message.sender_email, "recipient_id": message.recipient_id,
            "mailbox_id": message.mailbox_id or "synthetic-mailbox", "actual_to": message.recipient_email,
            "expected_subject": message.subject, "actual_subject": message.subject,
            "expected_body_text": message.body_text, "actual_body_text": message.body_text,
            "expected_body_html": message.body_html, "actual_body_html": message.body_html,
        }],
    }
    with pytest.raises(ActivationBlocked, match="footer policy"):
        validate_received_render_evidence(evidence, manifest)


def test_synthetic_reviewed_provider_footer_binds_both_mime_parts():
    manifest = _manifest()
    message = manifest.messages[0]
    now = datetime.now(UTC)
    footer_address = "2390 E Camelback Rd Ste 130, Phoenix, AZ 85016"
    footer_domain = "reviewed.example"
    actual_text = message.body_text + f"\n\nUnsubscribe: https://{footer_domain}/unsubscribe/jane\n{footer_address}"
    actual_html = message.body_html + (
        f'<p>Unsubscribe &middot; <a href="https://{footer_domain}/unsubscribe/jane">Unsubscribe</a></p>'
        f'<div style="margin-top:16px;font-size:12px;line-height:1.5;color:#767676;">{footer_address}</div>'
    )
    evidence = {
        "campaign_id": manifest.campaign_id, "manifest_hash": manifest.manifest_hash,
        "observed_at": now.isoformat(), "source": "gmail", "source_reference": "synthetic-gmail",
        "reviewed_by": "fixture-reviewer", "dynamic_merge_route": False,
        "used_literal_payload": True,
        "footer_policy": {
            "status": "approved", "mode": "provider_appended", "address": footer_address,
            "unsubscribe_domain": footer_domain, "reviewed_by": "fixture-reviewer",
            "reviewed_at": now.isoformat(),
        },
        "samples": [{
            "message_hash": message.content_hash, "step_index": message.step_index,
            "received_message_id": "synthetic-message", "sender_name": message.sender_name,
            "sender_email": message.sender_email, "recipient_id": message.recipient_id,
            "mailbox_id": message.mailbox_id or "synthetic-mailbox", "actual_to": message.recipient_email,
            "expected_subject": message.subject, "actual_subject": message.subject,
            "expected_body_text": message.body_text, "actual_body_text": actual_text,
            "expected_body_html": message.body_html, "actual_body_html": actual_html,
        }],
    }
    assert validate_received_render_evidence(evidence, manifest)["status"] == "verified"
    # Warmy's production-shaped receipt appends the reviewed footer only to
    # HTML; plaintext remains the frozen literal body.
    evidence["footer_policy"]["mode"] = "provider_html_only"
    evidence["samples"][0]["actual_body_text"] = message.body_text
    assert validate_received_render_evidence(evidence, manifest)["status"] == "verified"
    evidence["samples"][0]["actual_body_html"] = actual_html.replace(
        "https://example.com/unsubscribe", "https://example.com/changed", 1
    )
    with pytest.raises(ActivationBlocked, match="main HTML"):
        validate_received_render_evidence(evidence, manifest)
    evidence["samples"][0]["actual_body_html"] = actual_html + '<img src="https://reviewed.example/tracker.gif">'
    with pytest.raises(ActivationBlocked, match="unapproved element"):
        validate_received_render_evidence(evidence, manifest)
    evidence["samples"][0]["actual_body_html"] = actual_html + "<!-- reviewed footer comment -->"
    with pytest.raises(ActivationBlocked, match="unapproved comment"):
        validate_received_render_evidence(evidence, manifest)
    evidence["samples"][0]["actual_body_html"] = actual_html + '<div style="display:none"></div>'
    with pytest.raises(ActivationBlocked, match="hidden or tracking"):
        validate_received_render_evidence(evidence, manifest)
    evidence["samples"][0]["actual_body_html"] = actual_html + '<a href="https://reviewed.example/second">Unsubscribe</a>'
    with pytest.raises(ActivationBlocked, match="exactly one unsubscribe link"):
        validate_received_render_evidence(evidence, manifest)
    evidence["samples"][0]["actual_body_html"] = actual_html
    evidence["footer_policy"]["mode"] = "provider_appended"
    evidence["samples"][0]["actual_body_text"] = actual_text
    evidence["samples"][0]["actual_body_text"] = actual_text.replace(footer_address, "2390 E Camelback Blvd, Phoenix, AZ 85016")
    with pytest.raises(ActivationBlocked, match="footer"):
        validate_received_render_evidence(evidence, manifest)
    evidence["samples"][0]["actual_body_text"] = actual_text + "\n[[WSY_OPT_OUT]]"
    with pytest.raises(ActivationBlocked, match="WSY_OPT_OUT"):
        validate_received_render_evidence(evidence, manifest)
    evidence["samples"][0]["actual_body_text"] = actual_text.replace(
        "Unsubscribe:", "Warmy promotional footer\nUnsubscribe:"
    )
    with pytest.raises(ActivationBlocked, match="main body"):
        validate_received_render_evidence(evidence, manifest)


def test_internal_diagnostic_binds_jon_delivery_and_exact_disclosure():
    manifest = _manifest()
    message = manifest.messages[0]
    now = datetime.now(UTC)
    disclosure = "Sent by Codex on Jon Schack's behalf."
    footer_domain = "reviewed.example"
    footer_address = "2390 E Camelback Rd Ste 130, Phoenix, AZ 85016"
    production_url = "https://example.com/unsubscribe"
    unsubscribe_url = f"https://jon.example/unsubscribe/jane"
    transformed_text = message.body_text.replace(production_url, unsubscribe_url, 1)
    transformed_html = message.body_html.replace(production_url, unsubscribe_url, 1)
    actual_text = transformed_text
    actual_html = transformed_html + (
        f"<p><a href=\"https://{footer_domain}/unsubscribe/jane\">Unsubscribe</a></p>"
        f"<div>{footer_address}</div>"
    )
    evidence = {
        "campaign_id": manifest.campaign_id, "manifest_hash": manifest.manifest_hash,
        "observed_at": now.isoformat(), "source": "gmail", "source_reference": "synthetic-gmail",
        "reviewed_by": "fixture-reviewer", "dynamic_merge_route": False,
        "used_literal_payload": True,
        "footer_policy": {
            "status": "approved", "mode": "provider_html_only", "address": footer_address,
            "unsubscribe_domain": footer_domain, "reviewed_by": "fixture-reviewer",
            "reviewed_at": now.isoformat(),
        },
        "samples": [{
            "message_hash": message.content_hash, "step_index": message.step_index,
            "received_message_id": "synthetic-jon-message", "sender_name": message.sender_name,
            "sender_email": message.sender_email, "recipient_id": message.recipient_id,
            "mailbox_id": message.mailbox_id or "synthetic-mailbox", "actual_to": "jon@automationinterns.com",
            "diagnostic": True, "target_recipient_email": message.recipient_email,
            "production_unsubscribe_url": production_url,
            "recipient_specific_unsubscribe_url": unsubscribe_url,
            "jon_provider_prospect_id": "jon-test-prospect",
            "unsubscribe_token_provider_prospect_id": "jon-test-prospect",
            "unsubscribe_binding": {
                "message_hash": message.content_hash, "production_url": production_url,
                "jon_url": unsubscribe_url, "target_email": message.recipient_email,
                "mapping_hash": _mapping_hash(message.content_hash, production_url, unsubscribe_url, message.recipient_email),
            },
            "expected_subject": message.subject, "actual_subject": message.subject,
            "expected_body_text": message.body_text, "actual_body_text": actual_text,
            "expected_body_html": message.body_html, "actual_body_html": actual_html,
        }],
    }
    assert validate_received_render_evidence(evidence, manifest)["status"] == "verified"
    evidence["samples"][0]["unsubscribe_binding"]["mapping_hash"] = "tampered"
    with pytest.raises(ActivationBlocked, match="mapping hash"):
        validate_received_render_evidence(evidence, manifest)
    evidence["samples"][0]["unsubscribe_binding"]["mapping_hash"] = _mapping_hash(
        message.content_hash, production_url, unsubscribe_url, message.recipient_email
    )
    evidence["samples"][0]["actual_body_text"] = transformed_text + "\n" + disclosure
    with pytest.raises(ActivationBlocked, match="disclosure"):
        validate_received_render_evidence(evidence, manifest)


def test_footer_markup_parser_boundaries():
    valid = (
        '<p>Unsubscribe &middot; <a href="https://reviewed.example/u">Unsubscribe</a></p>'
        '<div style="margin-top:16px;color:#767676;">2120 W Encanto Blvd, Phoenix, AZ 85009</div>'
    )
    parser = _FooterMarkupParser("reviewed.example")
    parser.feed(valid)
    parser.close()
    assert len(parser.hrefs) == 1
    for bad_markup, reason in (
        ('<!-- tracking -->', "unapproved comment"),
        ('<div style="display:none"></div>', "hidden or tracking"),
        ('<img src="https://reviewed.example/pixel">', "unapproved element"),
    ):
        parser = _FooterMarkupParser("reviewed.example")
        with pytest.raises(ActivationBlocked, match=reason):
            parser.feed(bad_markup)
    parser = _FooterMarkupParser("reviewed.example")
    parser.feed(valid + '<a href="https://reviewed.example/second">Unsubscribe</a>')
    parser.close()
    assert len(parser.hrefs) == 2  # caller rejects this as not exactly one link


def test_authorized_literal_route_passes_exact_live_readback_then_rejects_drift():
    message = _manifest().messages[0].model_copy(update={"provider_prospect_id": "prospect-1"})
    snapshot = {
        "prospect_ids": ["prospect-1"],
        "mailbox_ids": ["mailbox-1"],
        "recipient_email": message.recipient_email,
        "sender_name": message.sender_name,
        "sender_email": message.sender_email,
        "variant_count": 1,
        "subject": message.subject,
        "body_text": message.body_text,
        "body_html": message.body_html,
        "schedule_date": message.scheduled_at.astimezone(EASTERN).date().isoformat(),
        "schedule_timezone": "Etc/GMT+5",
        "window_start_hour": 8,
        "window_end_hour": 9,
        "route": "literal_frozen",
    }
    evidence = {
        "campaign_id": "campaign-1",
        "source": "computer_use",
        "source_reference": "warmy-ui-campaign-1",
        "reviewed_by": "trusted-reviewer",
        "observed_at": datetime.now(UTC).isoformat(),
        "snapshot": snapshot,
        "snapshot_hash": hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(),
    }
    campaign = {
        "id": "campaign-1", "status": "paused", "mailboxIds": ["mailbox-1"],
        "prospectIds": ["prospect-1"],
        "senderName": message.sender_name, "senderEmail": message.sender_email,
        "timezone": "Etc/GMT+5", "sendingWindowStart": 8, "dailySendLimit": 5,
        "stopOnReply": True, "stopOnBounce": True, "stopOnUnsubscribe": True,
        "steps": [{
            "subject": message.subject, "bodyText": message.body_text,
            "bodyHtml": message.body_html,
            "delayDays": 0,
        }],
    }
    assert validate_live_literal_campaign(
        campaign, message, expected_campaign_id="campaign-1", expected_mailbox_ids={"mailbox-1"},
        live_read_evidence=evidence,
    )["status"] == "verified"
    campaign["steps"][0]["bodyHtml"] = None
    with pytest.raises(ActivationBlocked, match="exact bodyText and bodyHtml"):
        validate_live_literal_campaign(
            campaign, message, expected_campaign_id="campaign-1", expected_mailbox_ids={"mailbox-1"},
            live_read_evidence=evidence,
        )


def test_daily_reservation_is_global_and_retry_idempotent(tmp_path):
    db = Database(tmp_path / "sales.sqlite")
    first_rows = [row for row in _messages(count=5) if row["step_index"] == 0]
    first = build_frozen_manifest(
        campaign_id="campaign-a", campaign_manifest_hash="hash-a", messages=first_rows,
        first_send_at=datetime(2026, 9, 16, 8, tzinfo=EASTERN),
    )
    assert db.reserve_frozen_sends("campaign-a", first)["reserved"] == 5
    assert db.reserve_frozen_sends("campaign-a", first)["reserved"] == 0
    second_row = [row for row in _messages(count=6) if row["step_index"] == 0][-1]
    second = build_frozen_manifest(
        campaign_id="campaign-b", campaign_manifest_hash="hash-b", messages=[second_row],
        first_send_at=datetime(2026, 9, 16, 8, tzinfo=EASTERN),
    )
    with pytest.raises(ActivationBlocked, match="global daily send reservation cap"):
        db.reserve_frozen_sends("campaign-b", second)


def test_daily_reservation_binds_recipient_sequence_step_and_campaign(tmp_path):
    db = Database(tmp_path / "sales.sqlite")
    row = [row for row in _messages() if row["step_index"] == 0][0]
    first = build_frozen_manifest(
        campaign_id="campaign-a", campaign_manifest_hash="hash-a", messages=[row],
        first_send_at=datetime(2026, 9, 16, 8, tzinfo=EASTERN),
    )
    assert db.reserve_frozen_sends("campaign-a", first)["reserved"] == 1
    changed = dict(row)
    changed["subject"] = "Jane, about Property 0 — updated"
    second = build_frozen_manifest(
        campaign_id="campaign-b", campaign_manifest_hash="hash-b", messages=[changed],
        first_send_at=datetime(2026, 9, 16, 8, tzinfo=EASTERN),
    )
    with pytest.raises(ActivationBlocked, match="already reserved"):
        db.reserve_frozen_sends("campaign-b", second)
    same_payload_new_campaign = build_frozen_manifest(
        campaign_id="campaign-c", campaign_manifest_hash="hash-c", messages=[row],
        first_send_at=datetime(2026, 9, 16, 8, tzinfo=EASTERN),
    )
    with pytest.raises(ActivationBlocked, match="different campaign"):
        db.reserve_frozen_sends("campaign-c", same_payload_new_campaign)
