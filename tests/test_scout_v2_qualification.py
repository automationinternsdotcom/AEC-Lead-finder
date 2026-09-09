"""Strict qualification behavior: valid, explicit rejection, and quarantine."""
from __future__ import annotations

import json
import re
import sys
import threading
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scout"))

from v2.artifacts import ArtifactStore  # noqa: E402
from v2.contracts import DiscoveryCandidate, RecordStatus  # noqa: E402
from v2.qualification import BATCH_QUALIFICATION_PROMPT, JudgmentPayload, LEAD_GUIDANCE, QualificationService  # noqa: E402
from v2.state import StateStore  # noqa: E402


def setup(tmp_path):
    store = StateStore(tmp_path / "scout.db")
    store.migrate()
    store.create_run("run-1", "2026-08-28", "2026-08-27")
    store.upsert_source("source-1", "Example", "https://example.com/", "example.com")
    artifacts = ArtifactStore(tmp_path / "results", "2026-08-28", "run-1", store)
    return store, artifacts


def candidate(cid="candidate-1", url="https://example.com/a"):
    return DiscoveryCandidate(
        candidate_id=cid,
        run_id="run-1",
        provider="curated",
        discovered_url=url,
        resolved_url=url,
        canonical_url=url,
        title="Phoenix commercial property opens",
        source_id="source-1",
        source_name="Example",
        source_domain="example.com",
        published_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )


def valid_payload():
    return {
        "qualified": True,
        "business_name": "Acme Marketplace",
        "person": "Jane Manager",
        "event": "Opened a new retail marketplace.",
        "date_posted": "2026-08-28",
        "location": "Phoenix, Arizona",
        "summary": "Acme opened a retail property.",
        "state": "Arizona",
        "priority": "high",
        "property_type": "retail",
        "service_angle": "Aether can serve as a strategic partner.",
        "filter_reason": "A new operating property needs facilities support.",
        "confidence": "high",
    }


def test_qualification_prompt_guidance_names_useful_triggers_and_rejections():
    prompt = BATCH_QUALIFICATION_PROMPT.format(
        window_start="2026-08-27",
        window_end="2026-08-28",
        lead_guidance=LEAD_GUIDANCE,
        candidates="[]",
    )

    assert "permit" in prompt
    assert "rezoning" in prompt
    assert "operator, tenant, owner, property manager" in prompt
    assert "macro market reports" in prompt
    assert "day porter" in prompt


def test_qualified_judgment_requires_date_and_normalizes_numeric_confidence():
    payload = valid_payload()
    payload["date_posted"] = ""
    payload["confidence"] = 85

    with pytest.raises(ValidationError, match="date_posted"):
        JudgmentPayload.model_validate(payload)

    payload["date_posted"] = "Aug 28, 2026"
    judgment = JudgmentPayload.model_validate(payload)
    assert judgment.date_posted == date(2026, 8, 28)
    assert judgment.confidence == "high"


def test_qualification_uses_configured_workers(tmp_path):
    store, artifacts = setup(tmp_path)
    items = [
        candidate(f"candidate-{index}", f"https://example.com/{index}")
        for index in range(6)
    ]
    for item in items:
        store.save_candidate(item)
    barrier = threading.Barrier(3)

    def concurrent_model_call(model, prompt, tools):
        barrier.wait(timeout=2)
        candidate_ids = re.findall(r'"candidate_id": "([^"]+)"', prompt)
        return json.dumps(
            {
                candidate_id: {
                    "qualified": False,
                    "filter_reason": "Not a lead.",
                }
                for candidate_id in candidate_ids
            }
        ), {}

    result = QualificationService(
        store,
        artifacts,
        "grok-4.3",
        call_model=concurrent_model_call,
        workers=3,
        batch_size=2,
    ).qualify(items)

    assert set(result.rejected_candidate_ids) == {
        "candidate-0",
        "candidate-1",
        "candidate-2",
        "candidate-3",
        "candidate-4",
        "candidate-5",
    }


def test_qualification_persists_stable_entities_and_usage(tmp_path):
    store, artifacts = setup(tmp_path)
    item = candidate()
    store.save_candidate(item)
    service = QualificationService(
        store,
        artifacts,
        "grok-4.3",
        call_model=lambda model, prompt, tools: (
            json.dumps({"candidate-1": valid_payload()}),
            {"total_tokens": 123},
        ),
    )
    result = service.qualify([item])

    assert len(result.events) == 1
    assert len(result.people) == 1
    assert store.events_for_run("run-1")[0].supporting_candidate_ids == ["candidate-1"]
    assert store.people()[0].name == "Jane Manager"
    with store.connect() as conn:
        attempt = conn.execute("SELECT status, token_usage_json FROM v2_provider_attempts").fetchone()
    assert attempt["status"] == "completed"
    assert json.loads(attempt["token_usage_json"])["total_tokens"] == 123


def test_explicit_rejection_is_not_review(tmp_path):
    store, artifacts = setup(tmp_path)
    item = candidate()
    store.save_candidate(item)
    service = QualificationService(
        store,
        artifacts,
        "grok-4.3",
        call_model=lambda model, prompt, tools: (
            json.dumps(
                {
                    "candidate-1": {
                        "qualified": False,
                        "filter_reason": "Only macro commentary.",
                    }
                }
            ),
            {},
        ),
    )
    result = service.qualify([item])

    assert result.rejected_candidate_ids == ["candidate-1"]
    assert not result.reviews
    assert store.candidates_for_run("run-1")[0].record_status == RecordStatus.REJECTED


def test_retry_retires_prior_event_when_article_date_is_outside_window(tmp_path):
    store, artifacts = setup(tmp_path)
    item = candidate()
    store.save_candidate(item)
    valid = QualificationService(
        store,
        artifacts,
        "grok-4.3",
        call_model=lambda model, prompt, tools: (
            json.dumps({"candidate-1": valid_payload()}),
            {},
        ),
    )
    assert len(valid.qualify([item]).events) == 1

    outside = valid_payload() | {"date_posted": "2025-12-16"}
    guarded = QualificationService(
        store,
        artifacts,
        "grok-4.3",
        call_model=lambda model, prompt, tools: (
            json.dumps({"candidate-1": outside}),
            {},
        ),
        window_start=date(2026, 6, 1),
        window_end=date(2026, 6, 14),
    )
    result = guarded.qualify([item])

    assert result.rejected_candidate_ids == ["candidate-1"]
    assert store.active_events_for_run("run-1") == []
    stored = store.candidates_for_run("run-1")[0]
    assert stored.record_status == RecordStatus.REJECTED
    assert any(
        error.startswith(
            "qualified_event_date_outside_requested_window:2025-12-16"
        )
        for error in stored.validation_errors
    )


def test_incomplete_model_response_enters_review_without_disappearing(tmp_path):
    store, artifacts = setup(tmp_path)
    item = candidate()
    store.save_candidate(item)
    service = QualificationService(
        store,
        artifacts,
        "grok-4.3",
        call_model=lambda model, prompt, tools: (
            json.dumps(
                {
                    "candidate-1": {
                        "qualified": True,
                        "business_name": "Acme",
                    }
                }
            ),
            {},
        ),
    )
    result = service.qualify([item])

    assert len(result.reviews) == 1
    assert not result.events and not result.rejected_candidate_ids
    stored = store.candidates_for_run("run-1")[0]
    assert stored.record_status == RecordStatus.REVIEW
    assert "qualified result missing fields" in stored.validation_errors[-1]


def test_batch_qualification_isolates_a_missing_candidate_id(tmp_path):
    store, artifacts = setup(tmp_path)
    first = candidate("candidate-1", "https://example.com/a")
    second = candidate("candidate-2", "https://example.com/b")
    store.save_candidate(first)
    store.save_candidate(second)
    service = QualificationService(
        store,
        artifacts,
        "grok-4.3",
        call_model=lambda model, prompt, tools: (
            json.dumps(
                {
                    "candidate-1": {
                        "qualified": False,
                        "filter_reason": "Not a qualifying event.",
                    }
                }
            ),
            {},
        ),
    )

    result = service.qualify([first, second])

    assert result.rejected_candidate_ids == ["candidate-1"]
    assert [review.record_id for review in result.reviews] == ["candidate-2"]
    stored = {item.candidate_id: item for item in store.candidates_for_run("run-1")}
    assert stored["candidate-1"].record_status == RecordStatus.REJECTED
    assert stored["candidate-2"].record_status == RecordStatus.REVIEW
    with store.connect() as conn:
        attempt = conn.execute(
            "SELECT status FROM v2_provider_attempts WHERE target_type='discovery_candidate_batch'"
        ).fetchone()
    assert attempt["status"] == "review"


def test_duplicate_event_preserves_all_candidate_sources(tmp_path):
    store, artifacts = setup(tmp_path)
    first = candidate("candidate-1", "https://example.com/a")
    second = candidate("candidate-2", "https://example.com/b")
    store.save_candidate(first)
    store.save_candidate(second)
    service = QualificationService(
        store,
        artifacts,
        "grok-4.3",
        call_model=lambda model, prompt, tools: (
            json.dumps(
                {
                    "candidate-1": valid_payload(),
                    "candidate-2": valid_payload(),
                }
            ),
            {},
        ),
    )
    service.qualify([first, second])

    events = store.events_for_run("run-1")
    assert len(events) == 1
    assert events[0].supporting_candidate_ids == ["candidate-1", "candidate-2"]
