"""Shared company-level Aether outreach contract.

Both daily Scout and explicit bulk enrichment use this deterministic renderer.
Models select only a template, supporting event, slots, confidence, and sources;
they never author final outreach copy.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from v2.ids import canonicalize_url, normalize_text
from copy_grammar import review_copy


WHY_LINE_PROTOCOL_VERSION = "recipient-outreach-v4"
WHY_QUESTION_FUTURE = (
    " Is there any chance we could stay in touch regarding your future janitorial needs?"
)
WHY_QUESTION_REVIEW = " Is there any chance you'll be reviewing your janitorial needs?"
WHY_QUESTION_ADDITIONAL_SPACE = (
    " Is there any chance you'll be reviewing your janitorial needs, with the additional space?"
)
WHY_SHORT_REFERENCE_SLOTS = {"company", "project", "project_or_expansion"}
WHY_TEMPLATES = {
    "acquisition": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that {company} took ownership of {property} in {location}." + WHY_QUESTION_FUTURE,
        "slots": ("company", "property", "location"),
        "sendable": True,
    },
    "opening": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that {property} is opening in {location}." + WHY_QUESTION_FUTURE,
        "slots": ("property", "location"),
        "sendable": True,
    },
    "planned_development": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that plans are moving forward for {project} in {location}." + WHY_QUESTION_FUTURE,
        "slots": ("project", "location"),
        "sendable": True,
    },
    "approval": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that {project} received {approval}." + WHY_QUESTION_FUTURE,
        "slots": ("project", "approval"),
        "sendable": True,
    },
    "construction_start": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that construction started on {project} in {location}." + WHY_QUESTION_FUTURE,
        "slots": ("project", "location"),
        "sendable": True,
    },
    "lease_relocation": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that {company} is preparing to occupy {property} in {location}." + WHY_QUESTION_FUTURE,
        "slots": ("company", "property", "location"),
        "sendable": True,
    },
    "site_acquisition": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that {company} acquired {site} in {location}." + WHY_QUESTION_FUTURE,
        "slots": ("company", "site", "location"),
        "sendable": True,
    },
    "expansion": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that {company} is expanding in {location}." + WHY_QUESTION_ADDITIONAL_SPACE,
        "slots": ("company", "location"),
        "sendable": True,
    },
    "funded_facility": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that {funding} is supporting {project_or_expansion}." + WHY_QUESTION_FUTURE,
        "slots": ("funding", "project_or_expansion"),
        "sendable": True,
    },
    "renovation_conversion": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that {property} is being renovated into {new_use}." + WHY_QUESTION_REVIEW,
        "slots": ("property", "new_use"),
        "sendable": True,
    },
    "construction_progress": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that {project} reached {milestone}." + WHY_QUESTION_FUTURE,
        "slots": ("project", "milestone"),
        "sendable": True,
    },
    "completion": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that {project} was completed in {location}." + WHY_QUESTION_FUTURE,
        "slots": ("project", "location"),
        "sendable": True,
    },
    "route_new_owner": {"text": "", "slots": (), "sendable": False},
    "skip_negative": {"text": "", "slots": (), "sendable": False},
    "skip_general": {"text": "", "slots": (), "sendable": False},
}


class WhyLineSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = ""
    template_key: str = ""
    lead_event_id: str = ""
    slots: dict[str, str] = Field(default_factory=dict)
    confidence: str = "low"
    source_urls: list[str] = Field(default_factory=list)
    status: str = "review"
    validation_errors: list[str] = Field(default_factory=list)


def template_catalog() -> str:
    rows = [
        f'- {key}: {template["text"]}'
        for key, template in WHY_TEMPLATES.items()
        if template["sendable"]
    ]
    rows.extend(
        (
            "- route_new_owner: seller, broker, listing, auction, or unverified ownership transition; do not produce copy.",
            "- skip_negative: closure, bankruptcy, lawsuit, stalled, or abandoned project without a verified reopening or reuse; do not produce copy.",
            "- skip_general: market report, portfolio statistic, vendor article, or other signal without a specific property-level trigger; do not produce copy.",
        )
    )
    return "\n".join(rows)


def parse_why_line_selection(
    payload: dict,
    *,
    allowed_event_ids: set[str],
    known_company_names: Iterable[str] = (),
) -> WhyLineSelection:
    raw = payload.get("selection") or {}
    template_key = str(raw.get("template_key") or "").strip()
    lead_event_id = str(raw.get("lead_event_id") or "").strip()
    confidence = str(raw.get("confidence") or "low").casefold()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    sources: list[str] = []
    for value in raw.get("source_urls") or []:
        try:
            sources.append(canonicalize_url(str(value)))
        except ValueError:
            continue
    sources = list(dict.fromkeys(sources))
    template = WHY_TEMPLATES.get(template_key)
    slots_raw = raw.get("slots") or {}
    errors: list[str] = []
    slots: dict[str, str] = {}
    if isinstance(slots_raw, dict):
        for key, value in slots_raw.items():
            slot_key = str(key)
            if slot_key == "company":
                if _word_count(_clean_slot(value, lowercase=False)) > 3:
                    errors.append("why_line_reference_length")
                resolved = _known_company_reference(value, known_company_names)
                slots[slot_key] = resolved
                if not resolved:
                    errors.append("why_line_company_reference")
            elif slot_key == "location":
                resolved = _locality_reference(value)
                slots[slot_key] = resolved
                if not resolved:
                    errors.append("why_line_location")
            else:
                slots[slot_key] = _clean_slot(value)
    if not template:
        errors.append("why_template_unknown")
    if not lead_event_id or lead_event_id not in allowed_event_ids:
        errors.append("why_line_event_id")
    if not sources:
        errors.append("why_line_unsourced")
    text = ""
    status = "review"
    if template:
        required = set(template["slots"])
        supplied = set(slots)
        if supplied != required:
            errors.append("why_line_slots")
        if any(not slots.get(key) for key in required):
            errors.append("why_line_slot_missing")
        if any(
            _word_count(value) > 3
            for key, value in slots.items()
            if key in WHY_SHORT_REFERENCE_SLOTS
        ):
            errors.append("why_line_reference_length")
        if any(
            _word_count(value) > 16
            for key, value in slots.items()
            if key not in WHY_SHORT_REFERENCE_SLOTS
        ):
            errors.append("why_line_slot_length")
        if any(
            re.search(r"(?:https?://|www\.)", value, re.IGNORECASE)
            for value in slots.values()
        ):
            errors.append("why_line_slot_url")
        if template["sendable"] and not errors:
            text = review_copy(str(template["text"]).format(**slots))
            if not 20 <= _word_count(text) <= 55:
                errors.append("why_line_word_count")
            if "—" in text or "–" in text:
                errors.append("why_line_dash")
            if _sentence_count(text) != 2 or not text.endswith("?"):
                errors.append("why_line_sentence_count")
            prefix = "Hi [first name], I wanted to reach out after seeing on the news that "
            if not text.startswith(prefix):
                errors.append("why_line_opener")
            company_references = [slots["company"]] if slots.get("company") else []
            if not errors:
                status = "valid"
        elif not template["sendable"] and not errors:
            status = "skip"
    return WhyLineSelection(
        text=text if not errors else "",
        template_key=template_key,
        lead_event_id=lead_event_id,
        slots=slots,
        confidence=confidence,
        source_urls=sources,
        status="review" if errors else status,
        validation_errors=errors,
    )


def first_name(full_name: str) -> str:
    parts = [part for part in re.split(r"\s+", full_name.strip()) if part]
    while parts and parts[0].casefold().rstrip(".") in {
        "dr", "mr", "mrs", "ms", "miss", "prof",
    }:
        parts.pop(0)
    return parts[0].strip(" ,") if parts else ""


def personalize_why_line(text: str, name: str) -> str:
    if not name:
        raise ValueError("recipient has no usable first name")
    placeholder = "Hi [first name]"
    if not text.startswith(placeholder):
        raise ValueError("why line is missing the first-name placeholder")
    personalized = f"Hi {name}{text[len(placeholder):]}"
    if "[first name]" in personalized:
        raise ValueError("why line contains an unresolved first-name placeholder")
    return personalized


def anchor_event(events: list, scores: dict[str, int]):
    priority = {"high": 2, "medium": 1, "low": 0}
    return sorted(
        events,
        key=lambda item: (
            -scores.get(item.lead_event_id, -1),
            -priority.get(item.priority, -1),
            -(item.date_posted.toordinal() if isinstance(item.date_posted, date) else 0),
            item.lead_event_id,
        ),
    )[0]


def normalized_domain(value: str) -> str:
    raw = value.strip().casefold()
    if not raw:
        return ""
    try:
        url = canonicalize_url(raw if "://" in raw else f"https://{raw}")
    except ValueError:
        return ""
    return urlsplit(url).hostname or ""


def _word_count(value: str) -> int:
    return len([item for item in value.split() if item])


def _sentence_count(value: str) -> int:
    normalized = re.sub(r"(?<=\d)\.(?=\d)", "", value)
    normalized = re.sub(
        r"\b(?:Inc|Corp|Co|Ltd|LLC|L\.L\.C|U\.S|U\.S\.A)\.(?=\s)",
        lambda match: match.group(0).replace(".", ""),
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"\b([A-Z])\.(?=\s+[A-Z])", r"\1", normalized)
    return len(re.findall(r"[.!?](?=\s+[A-Z]|$)", normalized))


def _clean_slot(value: object, *, lowercase: bool = False) -> str:
    clean = " ".join(str(value or "").split()).strip(" ,.;:!?")
    return clean.lower() if lowercase else clean


def _locality_reference(value: object) -> str:
    clean = _clean_slot(value)
    if not clean:
        return ""
    clean = clean.split(",", 1)[0].strip()
    clean = re.split(r"\s+(?:and|/)\s+", clean, maxsplit=1)[0].strip()
    clean = re.split(r"\s+near\s+", clean, maxsplit=1)[0].strip()
    clean = re.sub(r"\s+(?:az|arizona)$", "", clean, flags=re.I).strip()
    clean = re.sub(r"\s+(?:area|region|outskirts)$", "", clean, flags=re.I).strip()
    clean = {"phoenix deer valley": "Deer Valley"}.get(clean.casefold(), clean)
    broad = {
        "arizona", "arizona cities", "east valley", "maricopa county",
        "metro phoenix", "phoenix metro", "pinal county", "west valley",
    }
    if (
        not clean
        or clean.casefold() in broad
        or clean.endswith(" county")
        or clean.endswith(" cities")
        or "," in clean
        or _word_count(clean) > 3
    ):
        return ""
    return clean


def _known_company_reference(value: object, names: Iterable[str]) -> str:
    requested = _clean_slot(value, lowercase=False)
    requested_key = normalize_text(requested)
    if not requested_key or _word_count(requested) > 3:
        return ""
    candidates: list[str] = []
    for known_name in names:
        name = _clean_slot(known_name, lowercase=False)
        words = list(re.finditer(r"[A-Za-z0-9]+(?:[&'’.-][A-Za-z0-9]+)*", name))
        for start in range(len(words)):
            for width in range(1, min(3, len(words) - start) + 1):
                phrase = name[words[start].start():words[start + width - 1].end()]
                if normalize_text(phrase) == requested_key:
                    candidates.append(phrase)
    if not candidates:
        return ""
    return max(
        candidates,
        key=lambda candidate: (
            sum(character.isupper() for character in candidate),
            sum(
                character.isupper()
                for index, character in enumerate(candidate)
                if index > 0
            ),
        ),
    )


def _uses_sentence_case_only(value: str, company_references: Iterable[str]) -> bool:
    allowed = {0}
    allowed.update(
        match.start(1)
        for match in re.finditer(r"[.!?]\s+([a-z])", value, re.IGNORECASE)
    )
    allowed.update(match.start() for match in re.finditer(r"\bI\b", value))
    for reference in company_references:
        allowed.update(
            index
            for match in re.finditer(re.escape(reference), value)
            for index in range(match.start(), match.end())
        )
    return all(
        not character.isupper() or index in allowed
        for index, character in enumerate(value)
    )
