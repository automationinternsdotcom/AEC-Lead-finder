"""Validate the canonical Aether public source list without network access."""
from __future__ import annotations

import csv
import sys
from pathlib import Path


EXPECTED_ROWS = 125
EXPECTED_HEADER = ["Resource Name", "URL"]


def validate(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        rows = list(reader)
    if header != EXPECTED_HEADER:
        raise ValueError(f"expected header {EXPECTED_HEADER!r}, got {header!r}")
    if len(rows) != EXPECTED_ROWS:
        raise ValueError(f"expected {EXPECTED_ROWS} data rows, got {len(rows)}")
    if any(len(row) != 2 or not row[1].strip() for row in rows):
        raise ValueError("every source row must contain a URL")
    normalized_urls = {
        (row[1].strip() if "://" in row[1] else f"https://{row[1].strip()}").casefold()
        for row in rows
    }
    if len(normalized_urls) != EXPECTED_ROWS:
        raise ValueError("source URLs must be unique")
    if any(not url.startswith(("http://", "https://")) for url in normalized_urls):
        raise ValueError("every source URL must be HTTP(S) after normalization")
    return len(rows)


def main() -> int:
    path = Path(__file__).resolve().parents[1] / "news_websites.csv"
    try:
        count = validate(path)
    except (OSError, ValueError) as exc:
        print(f"source-list validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"source-list validation passed: {count} websites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
