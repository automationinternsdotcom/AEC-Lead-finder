"""Shared, deliberately asymmetric recipient admission policy.

Research quality signals should improve ranking and trigger fallback research;
they must not quietly erase the only reachable contact.  Only objective address
failures, explicit suppression, and evidence-backed identity contradictions are
hard failures.
"""

from __future__ import annotations

from dataclasses import dataclass


SENDABLE_ADDRESS_STATUSES = frozenset({"verified", "valid", "catch_all", "unknown"})
HARD_ADDRESS_STATUSES = frozenset({"rejected", "invalid"})


@dataclass(frozen=True)
class RecipientAssessment:
    hard_failures: tuple[str, ...] = ()
    selection_blocks: tuple[str, ...] = ()
    quality_signals: tuple[str, ...] = ()

    @property
    def eligible(self) -> bool:
        return not self.hard_failures and not self.selection_blocks


def assess_recipient(
    *,
    verification_status: str,
    verification_reason: str = "",
    primary: bool = True,
    role_score: int = 0,
    alternative_email_count: int = 1,
    suppressed: bool = False,
    identity_excluded: bool = False,
) -> RecipientAssessment:
    """Classify facts without turning uncertainty into exclusion.

    ``selection_blocks`` mean another known contact should be selected or more
    verification is required. They are not global suppressions and do not make
    the address intrinsically bad.
    """

    status = str(verification_status or "").strip().casefold()
    reason = str(verification_reason or "").strip().casefold()
    hard: list[str] = []
    selection: list[str] = []
    signals: list[str] = []

    if identity_excluded:
        hard.append("company_identity_mismatch")
    if suppressed:
        hard.append("recipient_suppressed")
    if status in HARD_ADDRESS_STATUSES:
        hard.append("source_email_invalid")
    elif status not in SENDABLE_ADDRESS_STATUSES:
        selection.append("source_email_precheck_not_sufficient")

    if not primary:
        selection.append("recipient_not_primary")
    if role_score < 70:
        signals.append("recipient_role_below_preferred_threshold")
        if alternative_email_count > 1:
            selection.append("recipient_role_score_below_70")
        else:
            signals.append("sole_email_role_fallback")

    if status == "catch_all":
        signals.append("catch_all_address")
    elif status == "unknown":
        signals.append("mailbox_verification_unknown")
    if "organization_mismatch" in reason:
        signals.append("organization_domain_mismatch")

    return RecipientAssessment(
        hard_failures=tuple(dict.fromkeys(hard)),
        selection_blocks=tuple(dict.fromkeys(selection)),
        quality_signals=tuple(dict.fromkeys(signals)),
    )


def current_employer_match(email: str, organization_domain: str) -> bool | None:
    """Return True/False when an org domain is known, otherwise None."""

    email = str(email or "").strip().casefold()
    domain = str(organization_domain or "").strip().casefold().removeprefix("www.")
    if not domain or "@" not in email:
        return None
    email_domain = email.rsplit("@", 1)[1].removeprefix("www.")
    return email_domain == domain or email_domain.endswith("." + domain)


def candidate_rank_key(
    *,
    current_employer: bool | None,
    role_score: int,
    local_scope: bool,
    non_fallback_provider: bool,
    verification_status: str,
    evidence_count: int,
    stable_id: str,
) -> tuple[int, int, int, int, int, int, str]:
    """Sort best-first while retaining every non-invalid candidate."""

    employer_rank = 2 if current_employer is True else 1 if current_employer is None else 0
    verification_rank = {
        "verified": 3,
        "valid": 3,
        "catch_all": 2,
        "unknown": 1,
    }.get(str(verification_status or "").casefold(), 0)
    return (
        -employer_rank,
        -int(role_score),
        -int(local_scope),
        -int(non_fallback_provider),
        -verification_rank,
        -int(evidence_count),
        stable_id,
    )
