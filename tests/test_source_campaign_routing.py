from __future__ import annotations

from datetime import date
from pathlib import Path

from integration.config import Settings
from scout.v2.providers import SalesNavigatorAdapter


def test_maps_and_sales_sources_use_a_distinct_campaign_identity():
    settings = Settings(
        warmy_campaign_id="article-campaign",
        warmy_campaign_manifest_hash="article-hash",
        warmy_maps_sales_campaign_id="maps-sales-campaign",
        warmy_maps_sales_campaign_manifest_hash="maps-sales-hash",
    )

    assert settings.warmy_campaign_for_source("article") == (
        "article-campaign",
        "article-hash",
    )
    assert settings.warmy_campaign_for_source("mapsdata") == (
        "maps-sales-campaign",
        "maps-sales-hash",
    )
    assert settings.warmy_campaign_for_source("sales_navigator") == (
        "maps-sales-campaign",
        "maps-sales-hash",
    )
    assert settings.warmy_campaign_ids() == {
        "article-campaign",
        "maps-sales-campaign",
    }


def test_sales_navigator_adapter_preserves_provider_identity(tmp_path: Path):
    export = tmp_path / "sales-nav.csv"
    export.write_text(
        "First Name,Last Name,Title,Company Name,City,State,LinkedIn URL\n"
        "Jane,Doe,Facilities Director,Acme Hospitality,Phoenix,Arizona,"
        "https://www.linkedin.com/in/jane-doe\n",
        encoding="utf-8",
    )

    records = SalesNavigatorAdapter([str(export)]).discover(
        date(2026, 9, 1), date(2026, 9, 17)
    )

    assert len(records) == 1
    assert records[0].provider == "sales_navigator"
    assert "Acme Hospitality" in records[0].title
    assert records[0].raw["row"]["Company Name"] == "Acme Hospitality"
