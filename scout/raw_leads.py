"""Normalize provider exports into a durable, pre-enrichment raw lead queue."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path


RAW_FIELDS = (
    "raw_lead_id",
    "source",
    "source_record_id",
    "name",
    "title",
    "company",
    "location",
    "segment",
    "email",
    "phone",
    "linkedin_url",
    "website",
    "source_url",
    "evidence",
    "status",
    "retrieved_at",
    "original_row_json",
)


def ingest_sources(
    sources: list[tuple[str, Path]],
    output: Path,
    *,
    retrieved_at: str | None = None,
) -> dict[str, int | str]:
    """Read provider exports, write one normalized raw queue, and return counts.

    The importer intentionally does not qualify, enrich, verify, or send records.
    Those decisions belong to the later Grok pipeline.
    """
    timestamp = retrieved_at or datetime.now(UTC).replace(microsecond=0).isoformat()
    rows_by_key: dict[str, dict[str, str]] = {}
    source_counts: dict[str, int] = {}
    input_counts: dict[str, int] = {}
    for source, path in sources:
        source_key = _source_name(source)
        source_rows = 0
        for row in _read_rows(path):
            source_rows += 1
            normalized = _normalize_row(source_key, row, timestamp)
            if not normalized["company"] and not normalized["name"]:
                continue
            key = _identity_key(normalized)
            if key in rows_by_key:
                rows_by_key[key] = _merge_rows(rows_by_key[key], normalized)
            else:
                rows_by_key[key] = normalized
        input_counts[source_key] = input_counts.get(source_key, 0) + source_rows

    for row in rows_by_key.values():
        for source in row["source"].split(";"):
            source_counts[source] = source_counts.get(source, 0) + 1

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=RAW_FIELDS)
        writer.writeheader()
        writer.writerows(sorted(rows_by_key.values(), key=lambda row: (row["company"], row["name"])))
    manifest = output.with_name("raw_leads_manifest.json")
    manifest.write_text(
        json.dumps(
            {
                "output": str(output),
                "retrieved_at": timestamp,
                "inputs": [
                    {"source": source, "path": str(path)}
                    for source, path in sources
                ],
                "input_rows": input_counts,
                "unique_raw_leads": len(rows_by_key),
                "source_rows": source_counts,
                "status": "raw_pending_grok",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "output": str(output),
        "manifest": str(manifest),
        "input_rows": sum(input_counts.values()),
        "unique_raw_leads": len(rows_by_key),
    }


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as file:
        return [dict(row) for row in csv.DictReader(file)]


def _normalize_row(source: str, row: dict[str, str], timestamp: str) -> dict[str, str]:
    values = {_key(key): str(value or "").strip() for key, value in row.items()}
    first = _first(values, "first_name", "firstname", "given_name")
    last = _first(values, "last_name", "lastname", "surname", "family_name")
    name = _first(values, "name", "full_name", "person_name", "contact_name") or " ".join(
        part for part in (first, last) if part
    )
    company = _first(
        values,
        "company",
        "company_name",
        "organization",
        "organization_name",
        "business",
        "business_name",
        "tenant",
        "tenant_name",
    )
    city = _first(values, "city", "town")
    state = _first(values, "state", "region", "state_code")
    location = _first(values, "location", "address") or ", ".join(part for part in (city, state) if part)
    title = _first(values, "title", "role", "job_title", "current_title", "position")
    segment = _first(values, "segment", "category", "type", "industry", "lead_segment")
    email = _first(values, "email", "email_address", "work_email", "contact_email")
    phone = _first(values, "phone", "phone_number", "mobile", "telephone")
    linkedin = _first(values, "linkedin_url", "linkedin", "profile_url", "sales_navigator_url")
    website = _first(values, "website", "business_website", "company_website", "domain")
    source_url = _first(
        values,
        "source_url",
        "article_url",
        "property_url",
        "listing_url",
        "google_maps_url",
        "maps_url",
        "url",
    )
    evidence = _first(values, "evidence", "evidence_notes", "observed_signal", "notes", "description")
    source_record_id = _first(
        values,
        "source_record_id",
        "id",
        "lead_id",
        "place_id",
        "property_id",
        "tenant_id",
        "listing_id",
    )
    payload = {
        "source": source,
        "source_record_id": source_record_id,
        "name": name,
        "title": title,
        "company": company,
        "location": location,
        "segment": segment,
        "email": email,
        "phone": phone,
        "linkedin_url": linkedin,
        "website": website,
        "source_url": source_url,
        "evidence": evidence,
        "status": "raw_pending_grok",
        "retrieved_at": timestamp,
        "original_row_json": json.dumps(row, ensure_ascii=True, sort_keys=True),
    }
    payload["raw_lead_id"] = _stable_id(_identity_key(payload))
    return payload


def _merge_rows(existing: dict[str, str], incoming: dict[str, str]) -> dict[str, str]:
    merged = dict(existing)
    for field in RAW_FIELDS:
        if field in {"raw_lead_id", "original_row_json"}:
            continue
        if not merged.get(field) and incoming.get(field):
            merged[field] = incoming[field]
    sources = set(filter(None, (*existing["source"].split(";"), *incoming["source"].split(";"))))
    merged["source"] = ";".join(sorted(sources))
    original_rows: list[dict[str, str]] = []
    for payload in (existing["original_row_json"], incoming["original_row_json"]):
        value = json.loads(payload)
        original_rows.extend(value if isinstance(value, list) else [value])
    merged["original_row_json"] = json.dumps(original_rows, ensure_ascii=True, sort_keys=True)
    return merged


def _identity_key(row: dict[str, str]) -> str:
    linkedin = _clean_url(row.get("linkedin_url", ""))
    email = row.get("email", "").casefold()
    if linkedin:
        return f"linkedin:{linkedin}"
    if email:
        return f"email:{email}"
    values = "|".join(
        _normalize_text(row.get(field, ""))
        for field in ("name", "company", "title", "location")
    )
    return f"record:{values}"


def _source_name(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.strip().casefold()).strip("_") or "unknown"


def _key(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or ""))
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _first(values: dict[str, str], *names: str) -> str:
    for name in names:
        if values.get(name):
            return values[name]
    return ""


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _clean_url(value: str) -> str:
    return value.strip().removeprefix("https://").removeprefix("http://").rstrip("/").casefold()


def _stable_id(value: str) -> str:
    return "raw-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _parse_source(value: str) -> tuple[str, Path]:
    source, separator, path = value.partition("=")
    if not separator or not source.strip() or not path.strip():
        raise argparse.ArgumentTypeError("source must use NAME=/absolute/path.csv")
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        raise argparse.ArgumentTypeError(f"source file not found: {candidate}")
    return source.strip(), candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        action="append",
        type=_parse_source,
        required=True,
        metavar="NAME=CSV",
        help="provider export; repeat for MapsData, WarmySender, Sales Navigator, or Costar",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="normalized raw_leads.csv path, normally under results/raw-leads/YYYY-MM-DD/",
    )
    args = parser.parse_args(argv)
    result = ingest_sources(args.source, args.output)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
