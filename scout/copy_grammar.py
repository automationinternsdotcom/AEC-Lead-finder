"""Conservative grammar repairs for outreach, never a lead eligibility gate.

Only repair known constructions. Preserve claims, numbers, URLs, merge tokens,
tone and sentence order. Unknown prose passes through for editorial review.
"""
import re

GRAMMAR_REVIEW_VERSION = "grammar-v1"


def _article(phrase: str) -> str:
    # The numeric construction here is restricted to room counts.
    return "an" if re.match(r"(?:[aeiou]|8|(?:11|18)-)", phrase, re.I) else "a"

LOCALITIES = ("Phoenix", "Scottsdale", "Mesa", "Tempe", "Tucson", "Gilbert",
              "Chandler", "Glendale", "Peoria", "Surprise", "Buckeye", "Goodyear",
              "Queen Creek", "Casa Grande", "Apache Junction", "Maricopa", "Eloy",
              "Flagstaff", "Oro Valley", "San Tan Valley", "Sahuarita", "Tolleson",
              "Litchfield Park", "Avondale", "Wittmann", "Marana", "Superior",
              "Winslow", "Florence", "Yuma", "Paradise Valley", "Bullhead City")


def review_copy(text: str, references: tuple[str, ...] = ()) -> str:
    """Return idempotent, grammar-only corrections to a subject or plain body."""
    # Never alter markup, URLs or placeholders through a prose replacement.
    protected = re.compile(r"(https?://\S+|\{\{.*?\}\}|<[^>]*>)")
    parts = protected.split(text)
    for index in range(0, len(parts), 2):
        value = parts[index]
        value = re.sub(r"\bHi ([^,\n]+?) just wanted\b", r"Hi \1, I just wanted", value)
        value = re.sub(
            r"\binto ((?:\d[\d,]*-room |modern |luxury retail |entertainment and dining |law )?(?:hotel|sanctuary|destination|district|office))\b",
            lambda m: "into " + _article(m[1]) + " " + m[1],
            value, flags=re.I,
        )
        value = re.sub(r"\breceived (commission recommendation|zoning recommendation|\$[\d.]+m contract)\b", r"received a \1", value, flags=re.I)
        value = re.sub(r"\breached final phase\b", "reached the final phase", value, flags=re.I)
        singular = r"(?:data center campus|data center|industrial park|hotel site|battery site|recycling plant|plastic recycling plant|warehouse facility|distribution center|processing facility|training facility|performing arts center|rehabilitation hospital|medical school branch|building supply yard|small-bay industrial building|mixed-use building|mixed-use project|light industrial project|new development center|affordable condo project|massive dorm project|proving ground|law office)"
        value = re.sub(r"\b(for|on|of|acquired|occupy|into) (" + singular + r")(?= in\b|\.|\?)", lambda m: m[1] + " " + _article(m[2]) + " " + m[2], value, flags=re.I)
        value = re.sub(r"\bthat (former strip mall|office tower|first franchise location|second tucson location|semiconductor processing facility|flagship branch)\b", r"that the \1", value, flags=re.I)
        value = re.sub(r"\bon new headquarters\b", "on the new headquarters", value, flags=re.I)
        value = re.sub(r"\b(\d[\d,]*) sf (?=space|building|warehouse)", r"\1-square-foot ", value, flags=re.I)
        value = re.sub(r"\b(up to \$[\d.]+m) financing\b", r"\1 in financing", value, flags=re.I)
        for locality in LOCALITIES:
            value = re.sub(r"\bin ((?:(?:north|south|east|west|central|downtown|northwest|southwest) )?)" + re.escape(locality) + r"\b", lambda m: "in " + m[1] + locality, value, flags=re.I)
        # Restore only names supplied as trusted references; never title-case prose.
        for reference in sorted(references, key=len, reverse=True):
            if reference and any(c.isupper() for c in reference):
                value = re.sub(r"(?<!\w)" + re.escape(reference) + r"(?!\w)", lambda m: reference, value, flags=re.I)
        parts[index] = value
    return "".join(parts)
