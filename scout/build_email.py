"""Build the daily lead email from raw_leads.csv + contacts.csv, sorted by score."""
from __future__ import annotations

import csv
import os
import sys
from collections import defaultdict
from datetime import date

import config
import email_html

PRIORITY_SCORE = 50


def build(day, results_dir=None):
    day_dir = os.path.join(results_dir or config.RESULTS_DIR, day)
    raw_path = os.path.join(day_dir, "raw_leads.csv")
    contacts_path = os.path.join(day_dir, "contacts.csv")
    with open(raw_path, newline="", encoding="utf-8") as f:
        leads = list(csv.DictReader(f))
    contacts = defaultdict(list)
    if os.path.exists(contacts_path):
        with open(contacts_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = row.get("lead_event_id") or row["business_name"]
                contacts[key].append(row)

    leads.sort(key=lambda r: -int(r.get("score") or 0))
    priority = [r for r in leads if int(r.get("score") or 0) >= PRIORITY_SCORE]
    nurture = [r for r in leads if int(r.get("score") or 0) < PRIORITY_SCORE]
    contact_count = sum(
        1
        for people in contacts.values()
        for person in people
        if person.get("email") or person.get("phone") or person.get("linkedin")
    )

    sections = email_html.section(
        "Priority outreach",
        f"{len(priority)} sales-ready AEC leads",
        "Sorted by lead score, highest to lowest.",
        priority,
        contacts,
    )
    if nurture:
        sections += email_html.section(
            "Research &amp; review",
            f"Remaining {len(nurture)} reviewed AEC signals",
            "Sorted by lead score, highest to lowest. These are not approved for outreach and may include negative, incomplete, or lower-confidence signals.",
            nurture,
            contacts,
            styles="background:#f4f8f6;border-top:1px solid #e0ebe6",
        )

    out_path = os.path.join(day_dir, "leads_email.html")
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(
            email_html.page(
                sections,
                priority_count=len(priority),
                nurture_count=len(nurture),
                contact_count=contact_count,
            )
        )
    os.replace(tmp_path, out_path)
    print(f"wrote {out_path}: {len(priority)} priority + {len(nurture)} nurture")
    return out_path


def _self_check():
    assert email_html.slug("K's Apartments") == "k-s-apartments"
    assert "No contact identified" in email_html.contact_cell([])
    sample = {
        "business_name": "Example Business",
        "score": "75",
        "event": "New lease-up",
        "location": "Phoenix, Arizona",
        "summary": "Apartments enter lease-up.",
        "link": "https://example.com/article",
        "priority": "high",
    }
    table = email_html.table([sample], {})
    assert table.count("<th ") == 3
    assert "Business &amp; property signal" in table
    page = email_html.page(table, priority_count=1, nurture_count=0, contact_count=2)
    assert "Sales handoff" in page
    assert "Pipedrive and WarmySender" in page
    print("build_email self-check passed")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-check"]:
        _self_check()
    else:
        build(sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat())
