"""Sourced, non-blank project references for Aether's subject variants."""
from __future__ import annotations
import re

SUBJECT_A = "{{firstName}}, could we stay in touch about janitorial needs for {{custom.projectPropertyName}}?"
SUBJECT_B = "{{firstName}}, open to staying in touch about janitorial needs for {{custom.projectPropertyName}}?"


def _clean(value):
    return " ".join(str(value or "").replace("\u2013", "-").replace("\u2014", "-").split())


def _norm(value):
    return re.sub(r"[^a-z0-9]+", " ", _clean(value).casefold()).strip()


def _display(value):
    # Preserve provided mixed case/acronyms; title-case legacy lowercase slots.
    if value != value.lower():
        return value
    words = value.title().split()
    acronyms = {"asu", "lds", "gcu", "tsmc", "mufg", "us", "usa", "az", "ii", "iii"}
    return " ".join(word.upper() if word.casefold() in acronyms else word for word in words)


def project_reference(sequence):
    """Never resurrect stale slots contradicted by an independently corrected why."""
    slots = sequence.get("why_slots") or {}
    why = sequence.get("personalized_why_line") or sequence.get("company_why_line") or ""
    normalized = " " + _norm(why) + " "
    # Reviewed corrections sometimes replace the original template slots.
    for pattern in (
        r"(?:plans(?: moving forward)? for|continued development at) (.+?) in [^.?!]+[.?!]",
        r"the office building at (.+?) reached full occupancy",
    ):
        match = re.search(pattern, why, re.I)
        if match and len(match[1]) <= 100:
            name = _display(_clean(match[1]))
            return "the office building at " + name if "office building" in pattern else name
    store = re.search(r"([A-Z][\w'-]+) opened its [\d,]+(?:st|nd|rd|th) store in ([^.?!]+)", why)
    if store:
        return store[1] + "'s store in " + _display(_clean(store[2]))
    facility = re.search(r"opening a new (manufacturing facility) in ([^.?!]+)", why, re.I)
    if facility:
        return "the " + facility[1].lower() + " in " + _display(_clean(facility[2]))
    for key in ("property", "project", "site", "project_or_expansion"):
        value = _clean(slots.get(key))
        if (value and len(value) <= 100 and "{" not in value and "}" not in value
                and " " + _norm(value) + " " in normalized):
            location = _clean(slots.get("location"))
            if _norm(value) in {"data center", "solar farm", "hotel", "industrial park", "campus", "battery site", "manufacturing facility"}:
                result = "the " + value.lower()
                if location and " " + _norm(location) + " " in normalized and _norm(location) not in _norm(value):
                    result += " in " + _display(location)
                return result
            return _display(value)
    location = _clean(slots.get("location"))
    if "expan" in why.casefold() and location and " " + _norm(location) + " " in normalized:
        return "your expansion in " + _display(location)
    # No fabricated property name and no exclusion for incomplete metadata.
    return "your properties"
