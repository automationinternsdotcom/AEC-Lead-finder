"""Build a deterministic, non-authorizing preview of the next five review rows."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .send_gate import EXPECTED_ADDRESS, EXPECTED_SIGNATURE, REQUIRED_PROPOSAL_SENTENCE

FIXED_EST = ZoneInfo("Etc/GMT+5")
EXPECTED_SEND_AT = datetime(2026, 9, 16, 8, tzinfo=FIXED_EST)


def build_next_five_preview(
    review_export: dict[str, Any],
    *,
    first_send_at: datetime = EXPECTED_SEND_AT,
) -> dict[str, Any]:
    """Return a HOLD-only preview; never creates approval or provider input."""
    candidates = list(review_export.get("candidates") or [])
    if int(review_export.get("selection_count") or 0) != 5 or len(candidates) != 5:
        raise ValueError("review export must contain exactly five selected candidates")
    if first_send_at.tzinfo is None or first_send_at.astimezone(FIXED_EST).hour != 8:
        raise ValueError("first_send_at must be 08:00 fixed EST")
    blocker = str(review_export.get("approval_blocker") or "").strip()
    status = str(review_export.get("approval_status") or "HOLD").strip() or "HOLD"
    rows: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        why_line = str(candidate.get("why_line") or "").strip()
        property_name = str(candidate.get("reviewed_property") or "").strip()
        if not why_line or not property_name:
            raise ValueError(f"candidate {index} lacks reviewed property or why-line")
        rows.append(
            {
                "ordinal": index,
                "prospect_id": str(candidate.get("prospect_id") or ""),
                "recipient_email": str(candidate.get("email") or "").strip().casefold(),
                "first_name": str(candidate.get("first_name") or "").strip(),
                "company": str(candidate.get("company") or "").strip(),
                "property_name": property_name,
                "subject_a": str(candidate.get("subject_a") or ""),
                "subject_b": str(candidate.get("subject_b") or ""),
                "why_line": why_line,
                "source_url": str(candidate.get("article_url") or ""),
                "source_support": str(candidate.get("article_support") or ""),
                "scheduled_at": first_send_at.isoformat(),
                "sender_name": "Jordan Whitehurst",
                "signature_expected": "\n".join(EXPECTED_SIGNATURE),
                "address_expected": EXPECTED_ADDRESS,
                "proposal_sentence_required": REQUIRED_PROPOSAL_SENTENCE,
                "delivery_proof_required": True,
                "approval_check": str(candidate.get("approval_check") or "HOLD"),
            }
        )
    return {
        "artifact": "next-five-preview",
        "campaign_id": str(review_export.get("campaign_id") or ""),
        "created_at": datetime.now(tz=ZoneInfo("UTC")).isoformat(),
        "approval_status": "HOLD",
        "source_approval_status": status,
        "approval_blocker": blocker or "delivery proof and explicit operator approval required",
        "provider_action": False,
        "authorizes_send": False,
        "first_send_at": first_send_at.isoformat(),
        "cadence_policy": {
            "current_step_only": True,
            "step_0_delay_days": 0,
            "step_1_after_actual_prior_send_days": 7,
            "step_2_after_actual_prior_send_days": 14,
            "schedule_timezone": "Etc/GMT+5",
            "daily_cap_total": 5,
        },
        "footer_conflict_preserved": "footer/address" in blocker.casefold() or "address" in blocker.casefold(),
        "candidates": rows,
        "next_action": "Reconcile the live footer, capture exact bodyText and bodyHtml received proof, then create a literal_frozen approval batch.",
    }
