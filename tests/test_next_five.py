import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from integration.next_five import build_next_five_preview


def test_review_export_becomes_non_authorizing_exact_five_preview(tmp_path):
    source = json.loads(
        Path("/Users/openclaw/Code/lead-enrichment/data/warmysender/exports/next-five-review-20260916.json").read_text()
    )
    preview = build_next_five_preview(source)
    assert preview["approval_status"] == "HOLD"
    assert preview["provider_action"] is False
    assert preview["authorizes_send"] is False
    assert len(preview["candidates"]) == 5
    assert preview["first_send_at"].endswith("-05:00")
    assert preview["footer_conflict_preserved"] is True


def test_preview_rejects_non_five_selection():
    source = {"selection_count": 1, "candidates": [{}]}
    try:
        build_next_five_preview(source)
    except ValueError as exc:
        assert "exactly five" in str(exc)
    else:
        raise AssertionError("expected invalid selection to be rejected")
