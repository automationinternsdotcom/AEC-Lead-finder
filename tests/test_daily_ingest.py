from datetime import UTC, date, datetime, timedelta
from dataclasses import replace

import pytest

from integration.config import ActivationBlocked
from integration.daily import next_window, preserve_existing_enrollments
from integration.history import apply_matches
from integration.database import Database
from integration.models import ApprovalBatch
from integration.workflows import SalesWorkflows
from scout.v2.dedup import DedupContractError, remove_redundant_groups, validate_fuzzy_groups
from tests.test_sales_handoff_v2 import (
    _activation_settings, _campaign, _seed, _handoff, FakePipedrive, FakeWarmy,
)


def test_daily_window_catches_up_with_overlap():
    assert next_window({"through": "2026-09-07"}, date(2026, 9, 9)) == ("2026-09-05", "2026-09-08")
    assert next_window({"through": "2026-09-01"}, date(2026, 9, 15))[0] == "2026-08-30"


def test_shared_brand_does_not_conflate_distinct_domains(tmp_path):
    db = Database(tmp_path / "sales.sqlite")
    company = _handoff().companies[0]
    db.upsert_company(company)
    second = company.model_copy(update={"company_id": "corporate", "domain": "corporate.example", "legacy_ids": []})
    db.upsert_company(second)
    assert db.get_company("corporate")["domain"] == "corporate.example"
    assert db.resolve_company_alias("name", company.canonical_name) == company.company_id


def test_shared_parent_domain_does_not_conflate_entities(tmp_path):
    db = Database(tmp_path / "sales.sqlite")
    company = _handoff().companies[0]
    db.upsert_company(company)
    second = company.model_copy(update={"company_id": "county", "canonical_name": "County Government", "aliases": [], "legacy_ids": []})
    db.upsert_company(second)
    assert db.get_company("county")["canonical_name"] == "County Government"
    assert db.resolve_company_alias("domain", company.domain) == company.company_id


def test_redundant_singleton_repair_preserves_coverage_guard():
    groups = [{"kept_id": "a", "member_ids": ["a", "b"]},
              {"kept_id": "a", "member_ids": ["a"]}]
    assert len(validate_fuzzy_groups(["a", "b"], remove_redundant_groups(groups))) == 1
    with pytest.raises(DedupContractError):
        validate_fuzzy_groups(["a", "b", "c"], remove_redundant_groups(groups))
    with pytest.raises(DedupContractError):
        validate_fuzzy_groups(["a", "b", "c"], remove_redundant_groups([
            groups[0], {"kept_id": "b", "member_ids": ["b", "c"]}]))


def test_overlapping_run_preserves_enrolled_sequence_and_recipient(tmp_path):
    db = Database(tmp_path / "sales.sqlite")
    _seed(db, sequence_count=1)
    db.update_sequence("sequence-1", approval_state="enrolled")
    handoff, preserved = preserve_existing_enrollments(db, _handoff())
    assert preserved == 1
    assert not handoff.sequences
    assert not handoff.recipients
    assert not handoff.lead_events


@pytest.mark.parametrize("confidence,expected", [("high", "event-1"), ("medium", "event-new")])
def test_historical_event_reuses_crm_only_for_confirmed_match(tmp_path, confidence, expected):
    db = Database(tmp_path / "sales.sqlite")
    _seed(db, sequence_count=1)
    db.update_lead_event("event-1", pipedrive_lead_id="crm-existing")
    handoff = _handoff()
    handoff = handoff.model_copy(update={
        "lead_events": [handoff.lead_events[0].model_copy(update={"lead_event_id": "event-new"})],
        "sequences": [handoff.sequences[0].model_copy(update={"anchor_lead_event_id": "event-new"})],
    })
    result, reused = apply_matches(db, handoff, {"event-new": {
        "existing_id": "event-1", "same_company": True, "same_event": True, "confidence": confidence}})
    assert result.lead_events[0].lead_event_id == expected
    assert result.sequences[0].anchor_lead_event_id == expected
    assert reused == int(confidence == "high")


@pytest.mark.parametrize("status,allowed", [("draft", True), ("running", False), ("paused", False)])
def test_draft_ingestion_does_not_require_live_reply_automation(tmp_path, status, allowed):
    db = Database(tmp_path / "sales.sqlite")
    _seed(db, sequence_count=1)
    db.update_recipient("recipient-1", verification_status="valid", warmy_prospect_id="prospect-1")
    campaign = _campaign()
    campaign["status"] = status
    settings = replace(_activation_settings(campaign), pipedrive_automation_ready=False, campaign_start_enabled=False)
    assert "PIPEDRIVE_AUTOMATION_READY" in settings.campaign_enrollment_missing()
    assert "PIPEDRIVE_AUTOMATION_READY" not in settings.campaign_enrollment_missing(draft_only=True)
    now = datetime.now(UTC)
    db.save_approval_batch(ApprovalBatch(
        batch_id="batch", campaign_id="campaign-1", campaign_manifest_hash=settings.warmy_campaign_manifest_hash,
        sequence_ids=["sequence-1"], merge_hashes={"sequence-1": "merge-sequence-1"},
        maximum_recipient_count=1, approved_by="test", approved_at=now, expires_at=now + timedelta(hours=1)))
    warmy = FakeWarmy(campaign=campaign)
    workflow = SalesWorkflows(settings, db, warmy=warmy, pipedrive=FakePipedrive())
    if allowed:
        workflow.enroll_sequence({"sequence_id": "sequence-1", "draft_only": True})
        assert ("enroll", "prospect-1") in warmy.calls
    else:
        with pytest.raises(ActivationBlocked, match="draft-only"):
            workflow.enroll_sequence({"sequence_id": "sequence-1", "draft_only": True})
        assert ("enroll", "prospect-1") not in warmy.calls
