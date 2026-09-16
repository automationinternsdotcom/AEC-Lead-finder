from __future__ import annotations

import csv
import json
from pathlib import Path

from scout.raw_leads import RAW_FIELDS, ingest_sources


def test_ingest_sources_normalizes_and_merges_provider_exports(tmp_path: Path):
    sales_nav = tmp_path / "sales-nav.csv"
    sales_nav.write_text(
        "name,title,company,location,linkedin_url\n"
        "Alex Rivera,Facilities Manager,Acme Hospitality,Phoenix AZ,https://linkedin.com/in/alex\n",
        encoding="utf-8",
    )
    warmy = tmp_path / "warmy.csv"
    warmy.write_text(
        "firstName,lastName,role,company,email,linkedinUrl\n"
        "Alex,Rivera,Facilities Manager,Acme Hospitality,alex@acme.example,https://linkedin.com/in/alex\n",
        encoding="utf-8",
    )
    output = tmp_path / "results" / "raw_leads.csv"

    result = ingest_sources(
        [("sales_navigator", sales_nav), ("warmysender", warmy)],
        output,
        retrieved_at="2026-09-15T12:00:00+00:00",
    )

    assert result["input_rows"] == 2
    assert result["unique_raw_leads"] == 1
    with output.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert list(rows[0]) == list(RAW_FIELDS)
    assert rows[0]["source"] == "sales_navigator;warmysender"
    assert rows[0]["email"] == "alex@acme.example"
    assert rows[0]["status"] == "raw_pending_grok"
    assert len(json.loads(rows[0]["original_row_json"])) == 2


def test_ingest_sources_keeps_company_only_mapsdata_rows(tmp_path: Path):
    mapsdata = tmp_path / "mapsdata.csv"
    mapsdata.write_text(
        "Business,Category,Address,City,State,Website\n"
        "Acme Distribution,Warehouse,1 Main St,Phoenix,Arizona,acme.example\n",
        encoding="utf-8",
    )
    output = tmp_path / "raw_leads.csv"

    ingest_sources([("mapsdata", mapsdata)], output, retrieved_at="2026-09-15T12:00:00+00:00")

    with output.open(newline="", encoding="utf-8") as file:
        row = next(csv.DictReader(file))
    assert row["company"] == "Acme Distribution"
    assert row["segment"] == "Warehouse"
    assert row["location"] == "1 Main St"
    assert row["website"] == "acme.example"
