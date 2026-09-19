from datetime import UTC, date, datetime
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from scout.github_article_pipeline import (
    dedupe_leads,
    qualify_candidate,
    render_report,
    require_github_configuration,
    validate_source_list,
)


def _candidate(title="Acme opens a new commercial facility in Phoenix"):
    return SimpleNamespace(
        published_at=datetime(2026, 9, 18, 12, tzinfo=UTC),
        title=title,
        canonical_url="https://example.com/article/acme-phoenix",
        source_name="Example Business Journal",
    )


def test_qualify_candidate_requires_sourced_arizona_property_signal():
    lead = qualify_candidate(
        _candidate(),
        "Arizona commercial property coverage: the facility will open in Phoenix after tenant improvements.",
        date(2026, 9, 18),
        date(2026, 9, 18),
    )

    assert lead is not None
    assert lead.location == "Phoenix"
    assert lead.event.startswith("opening:")
    assert lead.email == ""


def test_qualify_candidate_rejects_unscoped_or_negative_articles():
    assert qualify_candidate(
        _candidate("Acme publishes an opinion about office trends"),
        "An opinion about commercial property in Phoenix.",
        date(2026, 9, 18),
        date(2026, 9, 18),
    ) is None
    assert qualify_candidate(
        _candidate("Acme opens a new commercial facility"),
        "The facility will open after tenant improvements.",
        date(2026, 9, 18),
        date(2026, 9, 18),
    ) is None


def test_dedupe_leads_keeps_one_sourced_event():
    lead = qualify_candidate(
        _candidate(),
        "Arizona commercial property coverage: the facility will open in Phoenix after tenant improvements.",
        date(2026, 9, 18),
        date(2026, 9, 18),
    )
    assert lead is not None
    duplicate = replace(lead, source_url="https://example.com/article/acme-phoenix?utm_source=x")

    assert len(dedupe_leads([lead, duplicate])) == 1


def test_source_list_requires_exactly_125_rows(tmp_path):
    path = tmp_path / "news_websites.csv"
    path.write_text(
        "Resource Name,URL\n" + "\n".join(f"Source {i},https://example{i}.com" for i in range(125)) + "\n",
        encoding="utf-8",
    )

    assert len(validate_source_list(path)) == 125


def test_report_includes_crm_id_and_no_prospect_send_statement():
    lead = qualify_candidate(
        _candidate(),
        "Arizona commercial property coverage: the facility will open in Phoenix after tenant improvements.",
        date(2026, 9, 18),
        date(2026, 9, 18),
    )
    assert lead is not None
    lead.pipedrive_lead_id = "lead-123"
    lead.pipedrive_status = "created"

    report = render_report(
        [lead], source_count=125, source_errors=0, since=date(2026, 9, 18), until=date(2026, 9, 18)
    )

    assert "lead-123 (created)" in report
    assert "No prospect outreach was sent." in report


def test_github_configuration_rejects_malformed_gmail_credentials(monkeypatch):
    values = {
        "TREG_TOKEN": "token",
        "PIPEDRIVE_API_TOKEN": "pipedrive",
        "PIPEDRIVE_DOMAIN": "example.pipedrive.com",
        "PIPEDRIVE_JORDAN_USER_ID": "11380767",
        "PIPEDRIVE_DEAL_FIELDS": json.dumps({"article_url": "field"}),
        "GMAIL_SERVICE_ACCOUNT_JSON": "not-json",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(RuntimeError, match="GMAIL_SERVICE_ACCOUNT_JSON must be valid JSON"):
        require_github_configuration()
