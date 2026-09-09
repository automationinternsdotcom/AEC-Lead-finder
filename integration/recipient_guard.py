"""Durable, exact-scope recipient guards shared by recovery and delivery.

Identity exclusions and review holds apply only to one company/email pair. They
must never become a global suppression unless the mailbox itself is objectively
invalid or the recipient separately opts out.
"""

from __future__ import annotations

from dataclasses import dataclass


IDENTITY_EXCLUSION = "identity-exclusion"
ACCURACY_REVIEW_HOLD = "accuracy-review-hold"


@dataclass(frozen=True)
class RecipientGuard:
    identity_exclusion: dict | None = None
    review_hold: dict | None = None

    @property
    def hard_failure(self) -> bool:
        return self.identity_exclusion is not None

    @property
    def reasons(self) -> tuple[str, ...]:
        reasons = []
        if self.identity_exclusion is not None:
            reasons.append("company_identity_mismatch")
        if self.review_hold is not None:
            reasons.append("accuracy_review_hold")
        return tuple(reasons)


def guard_key(kind: str, company_id: str, email: str) -> str:
    if kind not in {IDENTITY_EXCLUSION, ACCURACY_REVIEW_HOLD}:
        raise ValueError(f"unknown recipient guard kind: {kind}")
    return f"{kind}:{company_id}:{email.strip().casefold()}"


def inspect_recipient_guard(db, company_id: str, email: str) -> RecipientGuard:
    return RecipientGuard(
        identity_exclusion=db.get_state(guard_key(IDENTITY_EXCLUSION, company_id, email)),
        review_hold=db.get_state(guard_key(ACCURACY_REVIEW_HOLD, company_id, email)),
    )


def set_recipient_guard(db, kind: str, company_id: str, email: str, evidence: dict) -> None:
    if not evidence or not evidence.get("reason") or not evidence.get("evidence"):
        raise ValueError("recipient guard requires reason and evidence")
    db.set_state(guard_key(kind, company_id, email), evidence)
