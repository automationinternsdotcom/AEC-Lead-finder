"""Pre-send approval for literal, recipient-specific message payloads.

The provider API can accept a dynamic template and merge fields at send time;
our local process cannot intercept a provider scheduler that is launched in its
own UI or account.  Consequently this gate is intentionally honest: only a
``literal_frozen`` manifest can authorize a provider request made by this
pipeline.  A ``provider_rendered_canary`` is useful evidence for diagnostics,
but it does not authorize a dynamic merge route.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import ActivationBlocked
from .models import FrozenSendManifest, FrozenSendMessage

EXPECTED_SENDER_NAME = "Jordan Whitehurst"
EXPECTED_STEPS = frozenset((0, 1, 2))
EXPECTED_SIGNATURE = (
    "Jordan Whitehurst, Partner",
    "Aether Facility Services, LLC",
    "O: (602) 612-6393",
    "M: (813) 992-0858",
    "2120 W Encanto Blvd, Phoenix, AZ 85009",
)
EXPECTED_ADDRESS = EXPECTED_SIGNATURE[-1]
REQUIRED_PROPOSAL_SENTENCE = "Happy to provide a proposal when you're ready."
INTERNAL_DIAGNOSTIC_RECIPIENT = "jon@automationinterns.com"
# Warmy must not disclose the internal agent or that it acted on Jon's behalf.
# Match only explicit agent-disclosure language; ordinary copy containing a
# company/product named "Codex" remains valid.  The rendered-text helper below
# makes this also catch text split across HTML elements or HTML entities.
_WARMY_DISCLOSURE_RE = re.compile(
    r"(?:\b(?:sent|written|generated|prepared)\s+by\s+codex\b|"
    r"\bon\s+jon\s+schack(?:['’]s)?\s+behalf\b)",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"{{|}}")
_ADDRESS_RE = re.compile(
    r"\b\d{3,6}[ \t]+[A-Za-z][A-Za-z0-9 .'-]{2,80},[ \t]*"
    r"[A-Za-z .'-]+,[ \t]*[A-Z]{2}[ \t]+\d{5}(?:-\d{4})?\b"
)


def _rendered_text(value: str) -> str:
    """Compare semantic HTML content, not source markup/escaping."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _contains_warmy_disclosure(value: Any) -> bool:
    """Detect explicit Codex/agent disclosure in text or rendered HTML."""
    return isinstance(value, str) and bool(_WARMY_DISCLOSURE_RE.search(_rendered_text(value)))


def _semantic_contains(text: str, expected: str) -> bool:
    """Match reviewed prose across harmless paragraph/line-break whitespace."""
    normalize = lambda value: re.sub(r"\s+", " ", html.unescape(value)).strip()
    return normalize(expected) in normalize(text)


def _semantic_contains_folded(text: str, expected: str) -> bool:
    """Case-insensitive semantic match for reviewed property/source prose."""
    normalize = lambda value: re.sub(r"\s+", " ", html.unescape(value)).strip().casefold()
    return normalize(expected) in normalize(text)


def _invalid_scalar(value: Any) -> bool:
    return value is None or str(value).strip().casefold() in {"", "none", "null", "nan", "undefined"}


def _normal_text(value: str) -> str:
    """Normalize transport line endings while preserving literal content."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _urls(value: str) -> list[str]:
    return re.findall(r"https?://[^\s<>'\"]+", value, flags=re.IGNORECASE)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").casefold().rstrip(".")


def _reviewed_footer_policy(policy: Any) -> dict[str, str]:
    """Return an explicit, audited provider-footer decision.

    Warmy appends a footer outside the literal campaign body.  The address is
    intentionally not configured here: accepting a workspace address requires
    a fresh, reviewed decision.  Missing policy therefore remains HOLD.
    """
    if not isinstance(policy, dict) or policy.get("status") != "approved":
        raise ActivationBlocked("provider footer policy is missing or not explicitly approved")
    mode = str(policy.get("mode") or "")
    address = str(policy.get("address") or "").strip()
    domain = str(policy.get("unsubscribe_domain") or "").strip().casefold().rstrip(".")
    if mode not in {"provider_appended", "provider_html_only", "none"} or not address or not domain:
        raise ActivationBlocked("provider footer policy must specify an approved address and unsubscribe domain")
    if not str(policy.get("reviewed_by") or "").strip() or not str(policy.get("reviewed_at") or "").strip():
        raise ActivationBlocked("provider footer policy requires an audited reviewer and review time")
    if not _ADDRESS_RE.fullmatch(address):
        raise ActivationBlocked("provider footer policy address is malformed")
    if mode == "none" and address != EXPECTED_ADDRESS:
        raise ActivationBlocked("no-footer policy must retain the approved Aether signature address")
    return {"mode": mode, "address": address, "unsubscribe_domain": domain}


def _validate_received_bodies(
    approved_message: FrozenSendMessage,
    actual_text: Any,
    actual_html: Any,
    policy: dict[str, str],
    *,
    expected_text_override: str | None = None,
    expected_html_override: str | None = None,
) -> None:
    """Bind both received MIME parts to the frozen main body and reviewed footer.

    This is deliberately a suffix check, not a generic HTML/footer stripper.
    Any text before the provider footer must remain the approved literal body;
    only one explicitly reviewed address/domain may occur in the suffix.
    """
    if not isinstance(actual_text, str) or not isinstance(actual_html, str):
        raise ActivationBlocked("approved literal payload requires non-null text and HTML MIME bodies")
    if "[[wsy_opt_out]]" in actual_text.casefold() or "[[wsy_opt_out]]" in actual_html.casefold():
        raise ActivationBlocked("received evidence contains unresolved [[WSY_OPT_OUT]]")
    if _contains_warmy_disclosure(actual_text) or _contains_warmy_disclosure(actual_html):
        raise ActivationBlocked("Warmy payload must not contain a Codex/on-behalf disclosure")
    if _TOKEN_RE.search(actual_text) or _TOKEN_RE.search(actual_html):
        raise ActivationBlocked("received evidence contains unresolved merge tokens")
    expected_text = _normal_text(expected_text_override if expected_text_override is not None else approved_message.body_text)
    expected_html = _normal_text(expected_html_override if expected_html_override is not None else approved_message.body_html)
    actual_text_norm = _normal_text(actual_text)
    actual_html_norm = _normal_text(actual_html)
    if policy["mode"] == "none":
        if actual_text_norm != expected_text or actual_html_norm != expected_html:
            raise ActivationBlocked("received MIME bodies differ from the approved literal payload")
        return

    if policy["mode"] == "provider_html_only":
        if actual_text_norm != expected_text:
            raise ActivationBlocked("received plaintext differs from the approved literal payload")
        # Warmy changes only the received HTML part on this route.  Compare the
        # literal HTML prefix after CRLF normalization; visible-text equality
        # alone would allow an altered href inside the approved main body.
        if not actual_html_norm.startswith(expected_html):
            raise ActivationBlocked("received MIME main HTML differs from the approved literal payload")
        html_suffix_raw = actual_html_norm[len(expected_html) :]
        html_suffix = re.sub(r"\s+", " ", _rendered_text(html_suffix_raw)).strip()
        _validate_footer_suffix(html_suffix, html_suffix_raw, policy, html_markup=True)
        return

    expected_text_sem = re.sub(r"\s+", " ", expected_text).strip()
    actual_text_sem = re.sub(r"\s+", " ", actual_text_norm).strip()
    if not actual_text_sem.startswith(expected_text_sem):
        raise ActivationBlocked("received MIME main body differs from the approved literal payload")
    text_suffix = actual_text_sem[len(expected_text_sem) :].strip()
    if not text_suffix:
        raise ActivationBlocked("received provider footer is missing")
    # The older synthetic provider-appended mode is retained for test and
    # historical evidence, but its HTML prefix is still literal, not merely
    # visually equivalent.
    if not actual_html_norm.startswith(expected_html):
        raise ActivationBlocked("received MIME main HTML differs from the approved literal payload")
    html_suffix_raw = actual_html_norm[len(expected_html) :]
    html_suffix = re.sub(r"\s+", " ", _rendered_text(html_suffix_raw)).strip()
    _validate_footer_suffix(text_suffix, text_suffix, policy)
    _validate_footer_suffix(html_suffix, html_suffix_raw, policy, html_markup=True)


class _FooterMarkupParser(HTMLParser):
    """Allow only the reviewed unsubscribe/address fragment, never trackers."""

    def __init__(self, domain: str) -> None:
        super().__init__(convert_charrefs=True)
        self.domain = domain
        self.hrefs: list[str] = []

    def _check(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {"p", "a", "div", "br"}:
            raise ActivationBlocked("received HTML footer contains an unapproved element")
        allowed = {"href", "style"} if tag == "a" else {"style"}
        for name, value in attrs:
            if name.casefold() not in allowed:
                raise ActivationBlocked("received HTML footer contains an unapproved attribute")
            if name.casefold() == "style" and re.search(r"url\(|https?://|display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0", value or "", re.I):
                raise ActivationBlocked("received HTML footer contains hidden or tracking content")
            if name.casefold() == "href":
                if not value or _host(value) != self.domain:
                    raise ActivationBlocked("received HTML footer has an unknown unsubscribe domain")
                self.hrefs.append(value)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._check(tag.casefold(), attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._check(tag.casefold(), attrs)

    def handle_comment(self, data: str) -> None:
        raise ActivationBlocked("received HTML footer contains an unapproved comment")


def _validate_footer_suffix(suffix: str, raw_suffix: str, policy: dict[str, str], *, html_markup: bool = False) -> None:
    if not suffix:
        raise ActivationBlocked("received provider footer is missing")
    addresses = _ADDRESS_RE.findall(suffix)
    if addresses != [policy["address"]]:
        raise ActivationBlocked("received provider footer has an unknown or conflicting physical address")
    if "unsubscribe" not in suffix.casefold():
        raise ActivationBlocked("received provider footer is missing an unsubscribe control")
    residual = re.sub(r"https?://[^\s<>'\"]+", "", suffix, flags=re.IGNORECASE)
    residual = residual.replace(policy["address"], "")
    residual = re.sub(r"unsubscribe", "", residual, flags=re.IGNORECASE)
    residual = re.sub(r"[\s:,.|()\-–—·•]+", "", residual)
    if residual:
        raise ActivationBlocked("received provider footer contains unknown text")
    if html_markup:
        parser = _FooterMarkupParser(policy["unsubscribe_domain"])
        parser.feed(raw_suffix)
        parser.close()
        if len(parser.hrefs) != 1:
            raise ActivationBlocked("received HTML footer must contain exactly one unsubscribe link")
    raw_hosts = {_host(url) for url in _urls(raw_suffix)}
    if raw_hosts != {policy["unsubscribe_domain"]}:
        raise ActivationBlocked("received provider footer has an unknown unsubscribe domain")


def _approved_unsubscribe_url(message: FrozenSendMessage) -> str:
    candidates = [url.rstrip(".,)") for url in _urls(message.body_text)]
    if not candidates:
        candidates = [url.rstrip(".,)") for url in _urls(message.body_html)]
    if not candidates:
        raise ActivationBlocked("approved message has no unsubscribe URL to bind")
    return candidates[-1]


def _mapping_hash(message_hash: str, production_url: str, jon_url: str, target_email: str) -> str:
    return hashlib.sha256(
        _canonical(
            {
                "message_hash": message_hash,
                "production_url": production_url,
                "jon_url": jon_url,
                "target_email": target_email,
                "actual_to": INTERNAL_DIAGNOSTIC_RECIPIENT,
            }
        )
    ).hexdigest()


def _validate_diagnostic_binding(sample: dict[str, Any], message: FrozenSendMessage) -> dict[str, str] | None:
    diagnostic = bool(sample.get("diagnostic", False))
    actual_to = str(sample.get("actual_to") or "").strip().casefold()
    if diagnostic:
        if actual_to != INTERNAL_DIAGNOSTIC_RECIPIENT:
            raise ActivationBlocked("internal diagnostic must be delivered to the Jon test recipient")
        if str(sample.get("target_recipient_email") or "").strip().casefold() != message.recipient_email.strip().casefold():
            raise ActivationBlocked("internal diagnostic target is not bound to the approved recipient")
        if str(sample.get("codex_disclosure") or "").strip() or _contains_warmy_disclosure(sample.get("actual_body_text")) or _contains_warmy_disclosure(sample.get("actual_body_html")):
            raise ActivationBlocked("Warmy diagnostic must not contain a Codex/on-behalf disclosure")
        production_url = str(sample.get("production_unsubscribe_url") or "").strip()
        jon_url = str(sample.get("recipient_specific_unsubscribe_url") or "").strip()
        if (
            production_url != _approved_unsubscribe_url(message)
            or production_url not in message.body_html
            or not jon_url
            or jon_url == production_url
        ):
            raise ActivationBlocked("internal diagnostic unsubscribe URL mapping is not bound to the approved body")
        binding = sample.get("unsubscribe_binding")
        if not isinstance(binding, dict) or binding.get("production_url") != production_url or binding.get("jon_url") != jon_url:
            raise ActivationBlocked("internal diagnostic unsubscribe mapping is malformed")
        if binding.get("message_hash") != message.content_hash or binding.get("target_email") != message.recipient_email:
            raise ActivationBlocked("internal diagnostic unsubscribe mapping is bound to the wrong recipient")
        if binding.get("mapping_hash") != _mapping_hash(message.content_hash, production_url, jon_url, message.recipient_email):
            raise ActivationBlocked("internal diagnostic unsubscribe mapping hash mismatch")
        jon_provider_id = str(sample.get("jon_provider_prospect_id") or "").strip()
        token_provider_id = str(sample.get("unsubscribe_token_provider_prospect_id") or "").strip()
        if not jon_provider_id or token_provider_id != jon_provider_id:
            raise ActivationBlocked("internal diagnostic unsubscribe URL is not bound to the Jon test prospect")
        actual_text = str(sample.get("actual_body_text") or "")
        actual_html = str(sample.get("actual_body_html") or "")
        if jon_url not in actual_text or jon_url not in actual_html or production_url in actual_text or production_url in actual_html:
            raise ActivationBlocked("internal diagnostic MIME bodies did not apply the Jon unsubscribe URL substitution")
        return {
            "expected_text": message.body_text.replace(production_url, jon_url, 1),
            "expected_html": message.body_html.replace(production_url, jon_url, 1),
        }
    if actual_to and actual_to != message.recipient_email.strip().casefold():
        raise ActivationBlocked("received message recipient differs from frozen recipient")
    return None


def _validate_signature_addresses(body: str) -> None:
    """Check only the signature/footer region; property prose may cite addresses."""
    semantic = re.sub(r"\s+", " ", _rendered_text(body)).strip()
    marker = semantic.find(EXPECTED_SIGNATURE[0])
    if marker < 0:
        raise ActivationBlocked("approved Aether signature is incomplete")
    signature_tail = semantic[marker:]
    if _ADDRESS_RE.findall(signature_tail) != [EXPECTED_ADDRESS]:
        raise ActivationBlocked("message contains a missing, duplicate, or conflicting physical address")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=lambda item: item.isoformat() if isinstance(item, datetime) else str(item),
    ).encode("utf-8")


def _cadence_slot(prior: datetime, days: int, schedule_zone: ZoneInfo) -> datetime:
    """Return the first fixed-08:00 slot on the calendar-eligible date.

    Cadence requires the full N*24-hour elapsed interval, then rounds forward
    to the next fixed 08:00 slot. A prior 08:03 send therefore cannot use an
    08:00 slot that is three minutes early.
    """
    local_prior = prior.astimezone(schedule_zone)
    slot = datetime.combine(
        local_prior.date() + timedelta(days=days),
        time(8, 0),
        tzinfo=schedule_zone,
    )
    minimum = prior.astimezone(schedule_zone) + timedelta(days=days)
    if slot < minimum:
        slot += timedelta(days=1)
    return slot


def message_content_hash(message: FrozenSendMessage | dict[str, Any]) -> str:
    """Hash all send-critical fields, excluding the supplied hash itself."""
    raw = message.model_dump(mode="json") if isinstance(message, FrozenSendMessage) else dict(message)
    raw.pop("content_hash", None)
    return hashlib.sha256(_canonical(raw)).hexdigest()


def render_manifest_hash(manifest: FrozenSendManifest | dict[str, Any]) -> str:
    """Hash the full frozen artifact, excluding its self-reported hash."""
    raw = manifest.model_dump(mode="json") if isinstance(manifest, FrozenSendManifest) else dict(manifest)
    raw.pop("manifest_hash", None)
    return hashlib.sha256(_canonical(raw)).hexdigest()


def build_frozen_manifest(
    *,
    campaign_id: str,
    campaign_manifest_hash: str,
    messages: list[FrozenSendMessage | dict[str, Any]],
    first_send_at: datetime | None = None,
    approved_route: str = "literal_frozen",
    daily_send_cap: int = 5,
    schedule_timezone: str = "Etc/GMT+5",
) -> FrozenSendManifest:
    """Create a hash-complete artifact without contacting a provider."""
    frozen_messages: list[FrozenSendMessage] = []
    for item in messages:
        value = item.model_dump(mode="json") if isinstance(item, FrozenSendMessage) else dict(item)
        value.pop("content_hash", None)
        normalized = FrozenSendMessage.model_validate({**value, "content_hash": "pending"})
        value = normalized.model_dump(mode="json")
        value["content_hash"] = message_content_hash(value)
        frozen_messages.append(FrozenSendMessage.model_validate(value))
    value = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "campaign_manifest_hash": campaign_manifest_hash,
        "route": approved_route,
        "daily_send_cap": daily_send_cap,
        "schedule_timezone": schedule_timezone,
        "first_send_at": first_send_at,
        "messages": [item.model_dump(mode="json") for item in frozen_messages],
    }
    value["manifest_hash"] = render_manifest_hash(value)
    return FrozenSendManifest.model_validate(value)


def validate_received_render_evidence(
    evidence: dict[str, Any],
    manifest: FrozenSendManifest | dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify a real received message, including both provider MIME bodies.

    This proves the provider's literal route for the approved payload shape;
    it is not a substitute for checking the final send payload itself.
    """
    try:
        value = FrozenSendManifest.model_validate(manifest)
        observed = datetime.fromisoformat(str(evidence["observed_at"]))
        samples = list(evidence["samples"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ActivationBlocked("received render evidence is malformed") from exc
    current = now or datetime.now(UTC)
    if observed.tzinfo is None or not timedelta(0) <= current - observed <= timedelta(hours=24):
        raise ActivationBlocked("received render evidence must be fresh and timezone-aware")
    if evidence.get("campaign_id") != value.campaign_id or evidence.get("manifest_hash") != value.manifest_hash:
        raise ActivationBlocked("received render evidence belongs to a different frozen manifest")
    if (
        evidence.get("source") not in {"warmysender_mcp", "warmysender_api", "computer_use", "gmail"}
        or not str(evidence.get("source_reference") or "").strip()
        or not str(evidence.get("reviewed_by") or "").strip()
    ):
        raise ActivationBlocked("received render evidence requires a supported source reference")
    if evidence.get("dynamic_merge_route") is not False or evidence.get("used_literal_payload") is not True:
        raise ActivationBlocked("received evidence must prove the literal frozen route")
    footer_policy = _reviewed_footer_policy(evidence.get("footer_policy"))
    approved = {item.content_hash: item for item in value.messages}
    covered_steps: set[int] = set()
    covered_hashes: set[str] = set()
    for sample in samples:
        try:
            message_hash = str(sample["message_hash"])
            if message_hash in covered_hashes:
                raise ValueError
            approved_message = approved[message_hash]
            if not sample.get("received_message_id") or sample.get("sender_name") != EXPECTED_SENDER_NAME:
                raise ValueError
            if sample.get("sender_email") != approved_message.sender_email:
                raise ValueError
            if sample.get("recipient_id") != approved_message.recipient_id:
                raise ValueError
            if not sample.get("mailbox_id") or (approved_message.mailbox_id and sample.get("mailbox_id") != approved_message.mailbox_id):
                raise ValueError
            if not str(sample.get("actual_to") or "").strip():
                raise ValueError
            diagnostic_binding = _validate_diagnostic_binding(sample, approved_message)
            expected_subject = sample["expected_subject"]
            actual_subject = sample["actual_subject"]
            if (
                expected_subject != approved_message.subject
                or not isinstance(actual_subject, str)
                or actual_subject != expected_subject
                or not actual_subject.strip()
                or _TOKEN_RE.search(actual_subject)
            ):
                raise ValueError
            if sample["expected_body_text"] != approved_message.body_text or sample["expected_body_html"] != approved_message.body_html:
                raise ValueError
            _validate_received_bodies(
                approved_message,
                sample["actual_body_text"],
                sample["actual_body_html"],
                footer_policy,
                expected_text_override=diagnostic_binding["expected_text"] if diagnostic_binding else None,
                expected_html_override=diagnostic_binding["expected_html"] if diagnostic_binding else None,
            )
            if sample.get("step_index") != approved_message.step_index:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise ActivationBlocked("received render evidence does not match the approved literal payload") from exc
        covered_hashes.add(message_hash)
        covered_steps.add(approved_message.step_index)
    if not covered_hashes or covered_hashes != set(approved) or covered_steps != {item.step_index for item in value.messages}:
        raise ActivationBlocked("received render evidence lacks current-step coverage")
    return {
        "status": "verified",
        "campaign_id": value.campaign_id,
        "manifest_hash": value.manifest_hash,
        "observed_at": observed.isoformat(),
        "samples": len(samples),
        "source": evidence["source"],
    }


def validate_live_literal_campaign(
    campaign: dict[str, Any],
    message: FrozenSendMessage | dict[str, Any],
    *,
    expected_campaign_id: str | None = None,
    expected_mailbox_ids: set[str] | None = None,
    live_read_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail closed unless the live provider campaign is the frozen route.

    A local hash alone cannot prevent a separate dynamic legacy campaign from
    being started. This readback therefore requires one literal step, one
    audience entry, both body MIME fields, sender identity, and mailbox IDs.
    """
    value = FrozenSendMessage.model_validate(message)
    attestation = validate_live_read_evidence(
        live_read_evidence,
        value,
        expected_campaign_id=expected_campaign_id,
    )
    data = campaign.get("data") if isinstance(campaign, dict) else None
    data = data if isinstance(data, dict) else campaign
    live_campaign_id = str(data.get("id") or data.get("campaignId") or "")
    if expected_campaign_id and live_campaign_id != expected_campaign_id:
        raise ActivationBlocked("live campaign identity does not match frozen route")
    if str(data.get("status") or "").casefold() not in {"draft", "paused"}:
        raise ActivationBlocked("live campaign must be draft or paused")
    if data.get("timezone") != "Etc/GMT+5":
        raise ActivationBlocked("live campaign must use fixed EST (Etc/GMT+5)")
    if int(data.get("sendingWindowStart") or -1) != 8:
        raise ActivationBlocked("live campaign must begin its sending window at 08:00 fixed EST")
    try:
        daily_send_limit = int(data.get("dailySendLimit") or 0)
    except (TypeError, ValueError) as exc:
        raise ActivationBlocked("live campaign daily send limit is malformed") from exc
    if not 1 <= daily_send_limit <= 5:
        raise ActivationBlocked("live campaign daily send limit must be between one and five")
    for stop_name in ("stopOnReply", "stopOnBounce", "stopOnUnsubscribe"):
        if data.get(stop_name) is not True:
            raise ActivationBlocked(f"live campaign {stop_name} must be enabled")
    steps = data.get("steps")
    if not isinstance(steps, list) or len(steps) != 1:
        raise ActivationBlocked("live literal send route must contain exactly one step")
    step = steps[0]
    if step.get("subject") != value.subject:
        raise ActivationBlocked("live campaign subject differs from frozen payload")
    if "bodyText" not in step or "bodyHtml" not in step or step.get("bodyText") != value.body_text or step.get("bodyHtml") != value.body_html:
        raise ActivationBlocked("live campaign must expose exact bodyText and bodyHtml")
    if int(step.get("delayDays") or 0) != 0:
        raise ActivationBlocked("dedicated literal campaign step delay must be zero")
    if value.step_index == 0 and (
        not _semantic_contains_folded(str(step.get("bodyText") or ""), value.property_name)
        and not value.property_context_reviewed
    ):
        raise ActivationBlocked("initial live body must contain the reviewed property context")
    if value.step_index == 0 and value.property_context_reviewed and (
        not value.property_context
        or not _semantic_contains_folded(str(step.get("bodyText") or ""), value.property_context)
        or not _semantic_contains_folded(_rendered_text(str(step.get("bodyHtml") or "")), value.property_context)
    ):
        raise ActivationBlocked("initial live body must contain the reviewed property context")
    # Warmy API readback does not reliably expose campaign membership or
    # mailbox selection. Those fields must come from a fresh UI attestation,
    # never from local approval data or an unfiltered prospect listing.
    if not value.provider_prospect_id or attestation["prospect_ids"] != [value.provider_prospect_id]:
        raise ActivationBlocked("live campaign audience differs from frozen recipient")
    sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
    sender_name = data.get("senderName") or data.get("fromName") or sender.get("name")
    sender_email = data.get("senderEmail") or data.get("fromEmail") or sender.get("email")
    if sender_name and sender_name != value.sender_name:
        raise ActivationBlocked("live campaign sender differs from frozen sender")
    if sender_email and str(sender_email).strip().casefold() != value.sender_email:
        raise ActivationBlocked("live campaign sender differs from frozen sender")
    if expected_mailbox_ids is None or set(attestation["mailbox_ids"]) != {str(item) for item in expected_mailbox_ids}:
        raise ActivationBlocked("live campaign mailbox set differs from frozen approval")
    if attestation["sender_name"] != value.sender_name or attestation["sender_email"] != value.sender_email:
        raise ActivationBlocked("live UI readback sender differs from frozen sender")
    if attestation["subject"] != value.subject or attestation["body_text"] != value.body_text or attestation["body_html"] != value.body_html:
        raise ActivationBlocked("live UI readback body differs from frozen payload")
    if attestation["schedule_date"] != value.scheduled_at.astimezone(ZoneInfo("Etc/GMT+5")).date().isoformat():
        raise ActivationBlocked("live UI readback schedule date differs from frozen approval")
    if attestation["window_start_hour"] != 8 or attestation["window_end_hour"] != 9:
        raise ActivationBlocked("live UI readback sending window differs from frozen approval")
    if attestation["scheduled_at"] is not None:
        scheduled_local = attestation["scheduled_at"].astimezone(ZoneInfo("Etc/GMT+5"))
        if scheduled_local.date().isoformat() != attestation["schedule_date"] or not (8 <= scheduled_local.hour < 9):
            raise ActivationBlocked("live UI computed next-send time is outside the approved window")
    return {"status": "verified", "route": "literal_frozen", "step_index": value.step_index, "recipient_id": value.recipient_id}


def validate_live_read_evidence(
    evidence: dict[str, Any] | None,
    message: FrozenSendMessage | dict[str, Any],
    *,
    expected_campaign_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate the normalized fresh UI read required for hidden Warmy fields.

    The snapshot hash binds the operator's normalized browser read to the
    campaign. It is evidence, not authorization by itself: the caller must
    still compare the provider API status/settings/step and the frozen body.
    """
    if not isinstance(evidence, dict):
        raise ActivationBlocked("fresh UI live-read evidence is required")
    try:
        observed = datetime.fromisoformat(str(evidence["observed_at"]))
        snapshot = dict(evidence["snapshot"])
        snapshot_hash = str(evidence["snapshot_hash"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ActivationBlocked("live UI read evidence is malformed") from exc
    current = now or datetime.now(UTC)
    if evidence.get("source") not in {"computer_use", "warmysender_ui"}:
        raise ActivationBlocked("live-read evidence must identify the Warmy UI source")
    if not str(evidence.get("source_reference") or "").strip() or not str(evidence.get("reviewed_by") or "").strip():
        raise ActivationBlocked("live-read evidence requires an audited source reference and reviewer")
    if observed.tzinfo is None or not timedelta(0) <= current - observed <= timedelta(hours=24):
        raise ActivationBlocked("live UI read evidence must be fresh and timezone-aware")
    if expected_campaign_id and str(evidence.get("campaign_id")) != expected_campaign_id:
        raise ActivationBlocked("live UI read evidence belongs to a different campaign")
    if hashlib.sha256(_canonical(snapshot)).hexdigest() != snapshot_hash:
        raise ActivationBlocked("live UI read evidence snapshot hash mismatch")
    value = FrozenSendMessage.model_validate(message)
    try:
        prospect_ids = [str(item) for item in snapshot["prospect_ids"]]
        mailbox_ids = [str(item) for item in snapshot["mailbox_ids"]]
        scheduled_at_raw = snapshot.get("scheduled_at")
        scheduled_at = datetime.fromisoformat(str(scheduled_at_raw)) if scheduled_at_raw else None
        schedule_date = str(snapshot.get("schedule_date") or (scheduled_at.date().isoformat() if scheduled_at else ""))
        normalized = {
            "prospect_ids": prospect_ids,
            "mailbox_ids": mailbox_ids,
            "recipient_email": str(snapshot["recipient_email"]).strip().casefold(),
            "sender_name": str(snapshot["sender_name"]),
            "sender_email": str(snapshot["sender_email"]).strip().casefold(),
            "variant_count": int(snapshot["variant_count"]),
            "subject": str(snapshot["subject"]),
            "body_text": str(snapshot["body_text"]),
            "body_html": str(snapshot["body_html"]),
            "scheduled_at": scheduled_at,
            "schedule_date": schedule_date,
            "schedule_timezone": str(snapshot["schedule_timezone"]),
            "window_start_hour": int(snapshot["window_start_hour"]),
            "window_end_hour": int(snapshot["window_end_hour"]),
            "route": str(snapshot["route"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ActivationBlocked("live UI read evidence snapshot lacks required fields") from exc
    if (
        len(normalized["prospect_ids"]) != 1
        or not normalized["prospect_ids"][0]
        or not normalized["mailbox_ids"]
        or normalized["recipient_email"] != value.recipient_email
        or normalized["variant_count"] != 1
        or normalized["route"] != "literal_frozen"
        or normalized["sender_name"] != EXPECTED_SENDER_NAME
        or normalized["sender_email"] != value.sender_email
        or normalized["subject"] != value.subject
        or normalized["body_text"] != value.body_text
        or normalized["body_html"] != value.body_html
        or normalized["body_html"] == ""
        or not normalized["schedule_date"]
        or normalized["schedule_timezone"] != "Etc/GMT+5"
        or normalized["window_start_hour"] != 8
        or normalized["window_end_hour"] != 9
        or (normalized["scheduled_at"] is not None and normalized["scheduled_at"].tzinfo is None)
    ):
        raise ActivationBlocked("live UI read evidence does not match the frozen literal route")
    return normalized


def validate_frozen_manifest(
    manifest: FrozenSendManifest | dict[str, Any],
    *,
    expected_campaign_id: str | None = None,
    expected_campaign_manifest_hash: str | None = None,
    expected_sequence_ids: set[str] | None = None,
    now: datetime | None = None,
    require_future: bool = False,
) -> dict[str, Any]:
    """Validate the complete approval contract before a send handoff.

    ``require_future`` is used at activation time; building an artifact for a
    review queue may use historical or already-due timestamps.
    """
    try:
        value = FrozenSendManifest.model_validate(manifest)
    except Exception as exc:  # pydantic's detailed error is not needed at this boundary
        raise ActivationBlocked(f"frozen send manifest is malformed: {exc}") from exc
    if value.route != "literal_frozen":
        raise ActivationBlocked(
            "provider-rendered canaries are evidence only and cannot authorize a dynamic merge route"
        )
    if expected_campaign_id and value.campaign_id != expected_campaign_id:
        raise ActivationBlocked("frozen send manifest campaign ID mismatch")
    if expected_campaign_manifest_hash and value.campaign_manifest_hash != expected_campaign_manifest_hash:
        raise ActivationBlocked("frozen send manifest campaign hash mismatch")
    if value.manifest_hash != render_manifest_hash(value):
        raise ActivationBlocked("frozen send manifest hash mismatch")
    if value.schedule_timezone != "Etc/GMT+5":
        raise ActivationBlocked("send schedule must use fixed EST (Etc/GMT+5)")
    try:
        schedule_zone = ZoneInfo(value.schedule_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ActivationBlocked("frozen send manifest has an unknown schedule timezone") from exc
    if value.first_send_at is not None:
        if value.first_send_at.tzinfo is None:
            raise ActivationBlocked("first_send_at must be timezone-aware")
        local_first = value.first_send_at.astimezone(schedule_zone)
        if local_first.hour != 8 or local_first.utcoffset() != timedelta(hours=-5):
            raise ActivationBlocked("first send must be scheduled at 08:00 fixed EST")
    elif require_future:
        raise ActivationBlocked("first_send_at is required for activation approval")
    if expected_sequence_ids is not None:
        found = {message.sequence_id for message in value.messages}
        if found != expected_sequence_ids:
            raise ActivationBlocked("frozen send manifest recipient/sequence coverage mismatch")

    by_recipient: dict[str, list[FrozenSendMessage]] = defaultdict(list)
    seen_keys: set[tuple[str, int]] = set()
    now = now or datetime.now(UTC)
    daily_counts: dict[str, int] = defaultdict(int)
    for message in value.messages:
        key = (message.recipient_id, message.step_index)
        if key in seen_keys:
            raise ActivationBlocked("frozen send manifest contains duplicate recipient/step entries")
        seen_keys.add(key)
        if message.sender_name != EXPECTED_SENDER_NAME:
            raise ActivationBlocked("sender name must be exactly Jordan Whitehurst")
        if any(
            _invalid_scalar(item)
            for item in (
                message.sequence_id,
                message.recipient_id,
                message.recipient_email,
                message.first_name,
                message.company,
                message.property_name,
                message.sender_name,
                message.sender_email,
            )
        ):
            raise ActivationBlocked("message contains an unresolved scalar placeholder")
        if message.content_hash != message_content_hash(message):
            raise ActivationBlocked(f"message hash mismatch for {message.recipient_id} step {message.step_index}")
        for text_name, text in (("subject", message.subject), ("body_text", message.body_text), ("body_html", message.body_html)):
            if not text.strip():
                raise ActivationBlocked(f"blank {text_name} for {message.recipient_id} step {message.step_index}")
            if _TOKEN_RE.search(text) or "TODO_APPROVED_COPY" in text or "AETHER_POSTAL_ADDRESS" in text:
                raise ActivationBlocked(f"unresolved template token in {text_name} for {message.recipient_id} step {message.step_index}")
            if _contains_warmy_disclosure(text):
                raise ActivationBlocked(f"Warmy payload must not contain a Codex/on-behalf disclosure in {text_name}")
        if message.property_name not in message.subject:
            raise ActivationBlocked("subject must contain the actual property name")
        if message.property_context and _invalid_scalar(message.property_context):
            raise ActivationBlocked("property context contains an unresolved scalar placeholder")
        rendered_html = _rendered_text(message.body_html)
        if message.step_index == 0:
            property_in_body = _semantic_contains_folded(message.body_text, message.property_name) and _semantic_contains_folded(rendered_html, message.property_name)
            if not property_in_body:
                if not message.property_context_reviewed or not message.property_context or not _semantic_contains_folded(message.body_text, message.property_context) or not _semantic_contains_folded(rendered_html, message.property_context):
                    raise ActivationBlocked("initial body must contain the reviewed property context")
            if not _semantic_contains(message.body_text, REQUIRED_PROPOSAL_SENTENCE) or not _semantic_contains(rendered_html, REQUIRED_PROPOSAL_SENTENCE):
                raise ActivationBlocked("initial body must contain the approved proposal sentence")
            if not message.why_line.strip() or not _semantic_contains(message.body_text, message.why_line) or not _semantic_contains(rendered_html, message.why_line):
                raise ActivationBlocked("personalized why line is missing from the literal body")
            if not message.source_facts_reviewed:
                raise ActivationBlocked("source facts require explicit review before activation")
            if not message.source_facts or not all(fact.strip() and _semantic_contains(message.body_text, fact) and _semantic_contains(rendered_html, fact) for fact in message.source_facts):
                raise ActivationBlocked("source facts are missing from the literal body")
            if not message.source_urls or any(
                not url.strip() or not url.strip().lower().startswith(("https://", "http://"))
                for url in message.source_urls
            ):
                raise ActivationBlocked("source URLs are required for each initial message")
        if message.first_name not in message.body_text:
            raise ActivationBlocked("personalized body must contain the recipient first name")
        if "unsubscribe" not in message.body_text.casefold() or "unsubscribe" not in message.body_html.casefold():
            raise ActivationBlocked("unsubscribe control is missing from the literal body")
        for part in EXPECTED_SIGNATURE:
            if part not in message.body_text or part not in message.body_html:
                raise ActivationBlocked("approved Aether signature is incomplete")
        # A why-line may legitimately mention a property's address.  Only the
        # approved signature region is required to contain exactly the Aether
        # address; provider-appended footer addresses are checked separately
        # from independently reviewed received MIME evidence.
        _validate_signature_addresses(message.body_text)
        _validate_signature_addresses(message.body_html)
        if message.scheduled_at.tzinfo is None:
            raise ActivationBlocked("scheduled_at must be timezone-aware")
        local = message.scheduled_at.astimezone(schedule_zone)
        if local.hour != 8 or local.utcoffset() != timedelta(hours=-5):
            raise ActivationBlocked("messages must be scheduled at 08:00 fixed EST")
        daily_counts[local.date().isoformat()] += 1
        if daily_counts[local.date().isoformat()] > value.daily_send_cap:
            raise ActivationBlocked("frozen send manifest exceeds the total daily send cap")
        by_recipient[message.recipient_id].append(message)

    if not by_recipient:
        raise ActivationBlocked("frozen send manifest has no messages")
    step_set = {message.step_index for message in value.messages}
    if len(step_set) != 1 or not step_set <= EXPECTED_STEPS:
        raise ActivationBlocked("a frozen approval must contain one current step (0, 1, or 2)")
    for recipient_id, messages_for_recipient in by_recipient.items():
        if len(messages_for_recipient) != 1:
            raise ActivationBlocked(f"recipient {recipient_id} has duplicate current-step messages")
        message = messages_for_recipient[0]
        if message.step_index == 1:
            if message.prior_sent_at is None or not message.prior_message_id:
                raise ActivationBlocked("follow-up requires actual prior-send evidence")
            if message.scheduled_at != _cadence_slot(message.prior_sent_at, 7, schedule_zone):
                raise ActivationBlocked(f"recipient {recipient_id} has invalid 7-day follow-up cadence")
        elif message.step_index == 2:
            if message.prior_sent_at is None or not message.prior_message_id:
                raise ActivationBlocked("follow-up requires actual prior-send evidence")
            if message.scheduled_at != _cadence_slot(message.prior_sent_at, 14, schedule_zone):
                raise ActivationBlocked(f"recipient {recipient_id} has invalid 14-day follow-up cadence")
        if value.first_send_at is not None and message.scheduled_at < value.first_send_at:
            raise ActivationBlocked("message is earlier than the approved first_send_at")
    if require_future:
        local_now = now.astimezone(schedule_zone)
        for message in value.messages:
            local_scheduled = message.scheduled_at.astimezone(schedule_zone)
            # The provider may be released during the approved 08:00–09:00
            # fixed-EST window, including a heartbeat a few seconds after
            # 08:00. Outside that same-day window, hold rather than activating
            # early or allowing a stale/next-day campaign to start.
            if (
                local_now.date() != local_scheduled.date()
                or not (8 <= local_now.hour < 9)
                or local_now < local_scheduled
            ):
                raise ActivationBlocked("frozen send manifest is outside its approved 08:00-09:00 release window")
    return {
        "status": "verified",
        "manifest_hash": value.manifest_hash,
        "campaign_id": value.campaign_id,
        "recipient_count": len(by_recipient),
        "message_count": len(value.messages),
        "daily_send_cap": value.daily_send_cap,
        "schedule_timezone": value.schedule_timezone,
        "route": value.route,
    }
