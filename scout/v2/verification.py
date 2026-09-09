"""Deterministic contact normalization, verification, ranking, and caching."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import dns.exception
import dns.resolver

from .contracts import ContactCandidate, VerificationStatus
from .ids import normalize_text, stable_hash
from .state import StateStore


EMAIL_RE = re.compile(r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$", re.I)
DISPOSABLE_DOMAINS = {
    "10minutemail.com",
    "discard.email",
    "guerrillamail.com",
    "mailinator.com",
    "sharklasers.com",
    "tempmail.com",
    "yopmail.com",
}


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: VerificationStatus
    reason: str
    email: str = ""
    phone: str = ""
    linkedin: str = ""


class ContactVerifier:
    def __init__(
        self,
        state: StateStore,
        mx_lookup: Callable[[str], bool | None] | None = None,
        http_verify: Callable[[str], bool | None] | None = None,
        cache_days: int = 7,
    ):
        self.state = state
        self.mx_lookup = mx_lookup or lookup_mx
        self.http_verify = http_verify
        self.cache_days = cache_days

    def verify(
        self,
        *,
        email: str = "",
        phone: str = "",
        linkedin: str = "",
        organization_domain: str = "",
        domain_aliases: tuple[str, ...] = (),
    ) -> VerificationResult:
        normalized_email = email.strip().casefold()
        normalized_phone = normalize_phone(phone)
        normalized_linkedin = normalize_linkedin(linkedin)
        if email and not normalized_email:
            return VerificationResult(VerificationStatus.REJECTED, "email_empty_after_normalization")
        if normalized_email:
            syntax_error = email_rejection_reason(normalized_email)
            if syntax_error:
                return VerificationResult(
                    VerificationStatus.REJECTED,
                    syntax_error,
                    email=normalized_email,
                    phone=normalized_phone,
                    linkedin=normalized_linkedin,
                )
            domain = normalized_email.rsplit("@", 1)[1]
            organization_mismatch = not email_domain_matches_organization(
                domain, organization_domain, domain_aliases
            )
            mx = self._cached_check("mx", domain, lambda: self.mx_lookup(domain))
            if mx is False:
                return VerificationResult(
                    VerificationStatus.REJECTED,
                    "email_domain_has_no_mx",
                    email=normalized_email,
                    phone=normalized_phone,
                    linkedin=normalized_linkedin,
                )
            http = None
            if self.http_verify:
                http = self._cached_check(
                    "http_email", normalized_email, lambda: self.http_verify(normalized_email)
                )
                if http is False:
                    return VerificationResult(
                        VerificationStatus.REJECTED,
                        "external_email_verifier_rejected",
                        email=normalized_email,
                        phone=normalized_phone,
                        linkedin=normalized_linkedin,
                    )
            if http is True:
                status = VerificationStatus.VERIFIED
                reason = (
                    "external_email_verifier_valid_organization_mismatch"
                    if organization_mismatch
                    else "external_email_verifier_valid"
                )
            elif mx is True:
                status = VerificationStatus.UNKNOWN
                reason = (
                    "email_domain_organization_mismatch_mx_valid"
                    if organization_mismatch
                    else "domain_mx_valid_mailbox_unverified"
                )
            else:
                status = VerificationStatus.UNKNOWN
                reason = (
                    "email_domain_organization_mismatch_verification_unknown"
                    if organization_mismatch
                    else "email_syntax_valid_verification_unknown"
                )
            return VerificationResult(
                status,
                reason,
                email=normalized_email,
                phone=normalized_phone,
                linkedin=normalized_linkedin,
            )
        if phone and not normalized_phone:
            return VerificationResult(VerificationStatus.REJECTED, "phone_invalid", phone=phone.strip())
        if linkedin and not normalized_linkedin:
            return VerificationResult(
                VerificationStatus.REJECTED, "linkedin_invalid", linkedin=linkedin.strip()
            )
        if normalized_phone or normalized_linkedin:
            return VerificationResult(
                VerificationStatus.UNKNOWN,
                "sourced_contact_method_external_verification_unavailable",
                phone=normalized_phone,
                linkedin=normalized_linkedin,
            )
        return VerificationResult(VerificationStatus.REJECTED, "no_valid_contact_method")

    def _cached_check(self, kind: str, value: str, check: Callable[[], bool | None]) -> bool | None:
        key = stable_hash(kind, value)
        cached = self.state.get_verification_cache(key)
        if cached:
            if cached["status"] == "true":
                return True
            if cached["status"] == "false":
                return False
            return None
        try:
            result = check()
            error = ""
        except Exception as exc:
            result = None
            error = f"{type(exc).__name__}:{exc}"
        expires = (datetime.now(timezone.utc) + timedelta(days=self.cache_days)).isoformat()
        status = "true" if result is True else "false" if result is False else "unknown"
        self.state.set_verification_cache(key, kind, value, status, {"error": error}, expires)
        return result


def email_rejection_reason(
    email: str,
    organization_domain: str = "",
    domain_aliases: tuple[str, ...] = (),
) -> str:
    """Return only hard address failures.

    Organization-domain mismatch is intentionally a soft quality signal. A
    current-employer address wins when available, but a syntactically valid
    address with mail service remains eligible as the last-resort fallback.
    """
    if not EMAIL_RE.fullmatch(email):
        return "email_syntax_invalid"
    domain = email.rsplit("@", 1)[1].casefold()
    if domain in DISPOSABLE_DOMAINS:
        return "email_domain_disposable"
    return ""


def email_domain_matches_organization(
    email_domain: str,
    organization_domain: str = "",
    domain_aliases: tuple[str, ...] = (),
) -> bool:
    expected = {
        value.casefold().removeprefix("www.")
        for value in (organization_domain, *domain_aliases)
        if value
    }
    if not expected:
        return True
    domain = email_domain.casefold().removeprefix("www.")
    return domain in expected or any(domain.endswith(f".{item}") for item in expected)


def is_organization_mismatch(candidate: ContactCandidate) -> bool:
    return "organization_mismatch" in candidate.verification_reason


def normalize_phone(value: str) -> str:
    raw = str(value or "").strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"+1{digits}"
    if 11 <= len(digits) <= 15:
        return f"+{digits}"
    return ""


def normalize_linkedin(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    host = (parts.hostname or "").casefold()
    if parts.scheme not in {"http", "https"} or not (
        host == "linkedin.com" or host.endswith(".linkedin.com")
    ):
        return ""
    path = parts.path.rstrip("/")
    if not path.startswith("/in/"):
        return ""
    return urlunsplit(("https", "www.linkedin.com", path, "", ""))


def lookup_mx(domain: str) -> bool | None:
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5.0)
        return bool(list(answers))
    except dns.resolver.NXDOMAIN:
        return False
    except dns.resolver.NoAnswer:
        return False
    except (dns.resolver.NoNameservers, dns.exception.Timeout):
        return None


def contact_rank(candidate: ContactCandidate) -> tuple[int, int, int, int, int, str]:
    verification = {
        VerificationStatus.VERIFIED: 2,
        VerificationStatus.UNKNOWN: 1,
        VerificationStatus.REJECTED: 0,
    }[candidate.verification_status]
    return (
        int(bool(candidate.email)),
        int(not is_organization_mismatch(candidate)),
        verification,
        int(bool(candidate.phone)),
        len(candidate.evidence),
        candidate.contact_candidate_id,
    )


def select_best(candidates: list[ContactCandidate]) -> list[ContactCandidate]:
    by_target: dict[tuple[str, str], list[ContactCandidate]] = {}
    for candidate in candidates:
        by_target.setdefault((candidate.lead_event_id, candidate.person_id), []).append(candidate)
    selected: list[ContactCandidate] = []
    for group in by_target.values():
        eligible = [
            candidate
            for candidate in group
            if candidate.verification_status != VerificationStatus.REJECTED
        ]
        winner = max(eligible, key=contact_rank) if eligible else None
        selected.extend(
            candidate.model_copy(update={"selected": candidate == winner}) for candidate in group
        )
    return selected
