"""Discovery adapter, feed lifecycle, provider, and dedup contracts."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scout"))

from v2.artifacts import ArtifactStore  # noqa: E402
from v2.contracts import FeedStatus, RecordStatus  # noqa: E402
from v2.dedup import DedupContractError, validate_fuzzy_groups  # noqa: E402
from v2.discovery import (  # noqa: E402
    CuratedSiteAdapter,
    CuratedSource,
    FeedRegistry,
    _direct_listing_in_arizona_scope,
    _listing_partition_outside_window,
    article_score,
    feed_status_after_failure,
    load_curated_sources,
    parse_direct_listing,
    parse_index,
    publication_date,
)
from v2.http import FetchResponse  # noqa: E402
from v2.providers import (  # noqa: E402
    ApifyFacebookAdapter,
    NewsApiAdapter,
    ProviderPreflightError,
)
from v2.state import StateStore  # noqa: E402


def response(url: str, text: str) -> FetchResponse:
    return FetchResponse(url=url, content=text.encode())


def test_curated_source_loader_normalizes_bare_domains(tmp_path):
    path = tmp_path / "sources.csv"
    path.write_text("Resource Name,URL\nAZ Central,azcentral.com\n")
    source = load_curated_sources(path)[0]
    assert source.url == "https://azcentral.com/"
    assert source.domain == "azcentral.com"


def test_parse_index_finds_articles_and_same_domain_feed():
    parsed = parse_index(
        """
        <link rel="alternate" type="application/rss+xml" href="/feed.xml">
        <a href="/2026/08/28/large-commercial-project-opens">A large commercial project opens in Phoenix</a>
        <a href="/about">About our publication and team</a>
        <link rel="alternate" type="application/rss+xml" href="https://spam.test/feed">
        """,
        "https://news.example.com",
    )
    assert parsed.article_links == [
        (
            "https://news.example.com/2026/08/28/large-commercial-project-opens",
            "A large commercial project opens in Phoenix",
        )
    ]
    assert parsed.feed_links == ["https://news.example.com/feed.xml"]


def test_article_score_uses_anchor_title_for_generic_story_urls():
    assert article_score(
        "https://news.example.com/story/12345",
        "Phoenix industrial warehouse expansion breaks ground",
        "https://news.example.com",
    ) >= 3
    parsed = parse_index(
        '<a href="/story/12345">Phoenix industrial warehouse expansion breaks ground</a>',
        "https://news.example.com",
    )
    assert parsed.article_links == [
        (
            "https://news.example.com/story/12345",
            "Phoenix industrial warehouse expansion breaks ground",
        )
    ]


def test_publication_date_prefers_structured_metadata_to_url():
    found = publication_date(
        '<meta property="article:published_time" content="2026-08-28T15:30:00Z">',
        "https://example.com/2020/01/01/story",
    )
    assert found and found.date() == date(2026, 8, 28)


@pytest.mark.parametrize(
    ("payload", "kind", "article", "published"),
    [
        (
            json.dumps(
                [{
                    "link": "https://example.com/wp-story",
                    "date_gmt": "2026-08-28T12:00:00",
                    "title": {"rendered": "WP story"},
                }]
            ),
            "wordpress",
            "https://example.com/wp-story",
            date(2026, 8, 28),
        ),
        (
            """<rss><channel><item><guid>one</guid><title>RSS story</title>
            <link>https://example.com/rss-story</link>
            <pubDate>Fri, 28 Aug 2026 10:00:00 GMT</pubDate></item></channel></rss>""",
            "feed",
            "https://example.com/rss-story",
            date(2026, 8, 28),
        ),
        (
            """<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>one</id>
            <title>Atom story</title><link href="https://example.com/atom-story" />
            <published>2026-08-28T11:00:00Z</published></entry></feed>""",
            "feed",
            "https://example.com/atom-story",
            date(2026, 8, 28),
        ),
        (
            json.dumps({
                "version": "https://jsonfeed.org/version/1.1",
                "items": [{
                    "id": "one",
                    "url": "https://example.com/json-story",
                    "title": "JSON story",
                    "date_published": "2026-08-28T09:00:00Z",
                }],
            }),
            "json-feed",
            "https://example.com/json-story",
            date(2026, 8, 28),
        ),
        (
            """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
              xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"><url>
              <loc>https://example.com/sitemap-story</loc><lastmod>2025-01-01</lastmod>
              <news:news><news:publication_date>2026-08-28</news:publication_date>
              <news:title>Sitemap story</news:title></news:news></url></urlset>""",
            "urlset",
            "https://example.com/sitemap-story",
            date(2026, 8, 28),
        ),
    ],
)
def test_parse_direct_listing_saved_entry_shapes(payload, kind, article, published):
    listing = parse_direct_listing(
        payload.encode(), "https://example.com/listing", "https://example.com/listing"
    )

    assert listing and listing.kind == kind
    assert listing.entries[0].url == article
    assert listing.entries[0].published_at.date() == published


def test_parse_direct_listing_sitemap_index_preserves_query_and_rejects_off_domain():
    listing = parse_direct_listing(
        b"""<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <sitemap><loc>https://example.com/sitemap.xml?date=2026-08-28&amp;type=post</loc></sitemap>
        <sitemap><loc>https://off-domain.test/sitemap.xml?date=2026-08-28</loc></sitemap>
        </sitemapindex>""",
        "https://example.com/sitemap-index.xml",
        "https://example.com/sitemap-index.xml",
    )

    assert listing and listing.kind == "sitemapindex"
    assert listing.children == [
        "https://example.com/sitemap.xml?date=2026-08-28&type=post"
    ]


def test_ordinary_sitemap_lastmod_is_not_article_publication_date():
    listing = parse_direct_listing(
        b"""<urlset><url><loc>https://example.com/story</loc>
        <lastmod>2026-08-28T10:00:00Z</lastmod></url></urlset>""",
        "https://example.com/sitemap.xml",
        "https://example.com/sitemap.xml",
    )

    assert listing and listing.entries[0].published_at is None


def test_direct_listing_is_bounded_and_malformed_payload_is_not_misparsed():
    payload = json.dumps(
        [
            {"link": f"https://example.com/story-{index}", "date_gmt": "2026-08-28"}
            for index in range(5)
        ]
    ).encode()

    listing = parse_direct_listing(
        payload, "https://example.com/wp-json/posts", "https://example.com", max_items=2
    )
    assert listing and len(listing.entries) == 2
    assert listing.truncated
    assert parse_direct_listing(
        b"[{malformed", "https://example.com/wp-json/posts", "https://example.com"
    ) is None


def test_curated_direct_feed_uses_feed_date_and_rejects_out_of_window_and_off_domain(tmp_path):
    source = CuratedSource(
        source_id="source-direct",
        name="Regional desk",
        url="https://example.com/feed.xml",
        domain="example.com",
    )
    feed = """<rss><channel>
      <item><title>In window</title><link>https://example.com/short</link>
      <pubDate>Fri, 28 Aug 2026 10:00:00 GMT</pubDate></item>
      <item><title>Too old</title><link>https://example.com/old</link>
      <pubDate>Fri, 1 May 2026 10:00:00 GMT</pubDate></item>
      <item><title>Foreign</title><link>https://foreign.test/story</link>
      <pubDate>Fri, 28 Aug 2026 10:00:00 GMT</pubDate></item>
    </channel></rss>"""
    pages = {
        source.url: feed,
        "https://example.com/short": (
            '<meta property="article:published_time" content="2026-08-27T08:00:00Z">'
        ),
    }
    store = StateStore(tmp_path / "scout.db")
    store.migrate()
    store.create_run("run-direct", "2026-08-28", "2026-06-01")
    artifacts = ArtifactStore(tmp_path / "results", "2026-08-28", "run-direct", store)
    batch = CuratedSiteAdapter(
        [source], store, artifacts, fetch=lambda url: response(url, pages[url]), workers=1
    ).discover("run-direct", date(2026, 6, 1), until=date(2026, 8, 28))

    assert [(item.canonical_url, item.published_at.date()) for item in batch.candidates] == [
        ("https://example.com/short", date(2026, 8, 27))
    ]


def test_curated_explicit_sitemap_prunes_old_children_before_cap_and_keeps_query(tmp_path):
    root_url = "https://www.jll.com/sitemap-index.xml?type=post&year=2026"
    source = CuratedSource(
        source_id="source-sitemap",
        name="JLL Phoenix",
        url=root_url,
        domain="www.jll.com",
    )
    old_children = "".join(
        f"<sitemap><loc>https://www.jll.com/day.xml?date=2026-05-{day:02d}&amp;page={page}</loc></sitemap>"
        for page in range(1, 101)
        for day in [(page - 1) % 28 + 1]
    )
    in_window = "https://www.jll.com/day.xml?date=2026-08-28&page=1"
    article = "https://www.jll.com/en-us/insights/phoenix-industrial-market"
    pages = {
        root_url: f"<sitemapindex>{old_children}<sitemap><loc>{in_window.replace('&', '&amp;')}</loc></sitemap></sitemapindex>",
        in_window: f"<urlset><url><loc>{article}</loc></url></urlset>",
        article: '<meta property="article:published_time" content="2026-08-28T08:00:00Z">',
    }
    calls: list[str] = []

    def fetch(url):
        calls.append(url)
        if url not in pages:
            raise RuntimeError("not found")
        return response(url, pages[url])

    store = StateStore(tmp_path / "scout.db")
    store.migrate()
    store.create_run("run-sitemap", "2026-08-28", "2026-06-01")
    artifacts = ArtifactStore(tmp_path / "results", "2026-08-28", "run-sitemap", store)
    batch = CuratedSiteAdapter(
        [source], store, artifacts, fetch=fetch, workers=1
    ).discover("run-sitemap", date(2026, 6, 1), until=date(2026, 8, 28))

    assert calls.count(root_url) == 1
    assert in_window in calls
    assert not any("date=2026-05-" in url for url in calls)
    assert [item.canonical_url for item in batch.candidates] == [article]


def test_curated_direct_listing_routes_non_arizona_article_to_review(tmp_path):
    source = CuratedSource(
        source_id="source-national",
        name="JLL Phoenix",
        url="https://www.jll.com/api/posts?region=phoenix",
        domain="www.jll.com",
    )
    article = "https://www.jll.com/en-us/insights/national-market-report"
    pages = {
        source.url: json.dumps([{
            "link": article,
            "date_gmt": "2026-08-28T08:00:00Z",
            "title": {"rendered": "National market report"},
        }]),
        article: (
            '<meta property="article:published_time" content="2026-08-28T08:00:00Z">'
            "<p>United States commercial property trends.</p>"
        ),
    }
    store = StateStore(tmp_path / "scout.db")
    store.migrate()
    store.create_run("run-national", "2026-08-28", "2026-06-01")
    artifacts = ArtifactStore(tmp_path / "results", "2026-08-28", "run-national", store)
    batch = CuratedSiteAdapter(
        [source], store, artifacts, fetch=lambda url: response(url, pages[url]), workers=1
    ).discover("run-national", date(2026, 6, 1), until=date(2026, 8, 28))

    assert len(batch.candidates) == 1
    assert batch.candidates[0].record_status == RecordStatus.REVIEW
    assert "direct_listing_arizona_scope_unverified" in batch.candidates[0].validation_errors


def test_simoncre_oak_harbor_arizona_investor_article_routes_to_review(tmp_path):
    source = CuratedSource(
        source_id="source-simoncre",
        name="SimonCRE News",
        url="https://blog.simoncre.com/news/rss.xml",
        domain="blog.simoncre.com",
    )
    article = (
        "https://blog.simoncre.com/news/"
        "haggen-anchored-shopping-center-in-oak-harbor-sells-to-arizona-investor"
    )
    title = "Haggen-anchored shopping center in Oak Harbor sells to Arizona investor"
    feed = f"""<rss><channel><item><title>{title}</title><link>{article}</link>
      <pubDate>Tue, 4 Aug 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
    page = f"""<html><head><title>{title}</title>
      <meta name="description" content="Island Plaza is located in Oak Harbor, WA.">
      <meta property="article:published_time" content="2026-08-04T10:00:00Z">
      </head><body><p>Scottsdale, Arizona-based investor SimonCRE acquired the
      shopping center in Oak Harbor on Whidbey Island in the Puget Sound region.</p>
      </body></html>"""
    store = StateStore(tmp_path / "scout.db")
    store.migrate()
    store.create_run("run-simoncre", "2026-09-01", "2026-06-01")
    artifacts = ArtifactStore(tmp_path / "results", "2026-09-01", "run-simoncre", store)
    batch = CuratedSiteAdapter(
        [source],
        store,
        artifacts,
        fetch=lambda url: response(url, feed if url == source.url else page),
        workers=1,
    ).discover("run-simoncre", date(2026, 6, 1), until=date(2026, 9, 1))

    assert len(batch.candidates) == 1
    assert batch.candidates[0].record_status == RecordStatus.REVIEW
    assert batch.candidates[0].validation_errors == [
        "direct_listing_arizona_scope_unverified"
    ]


def test_non_arizona_investor_origin_does_not_block_phoenix_event():
    assert _direct_listing_in_arizona_scope(
        "Phoenix shopping center sells to Washington investor",
        "https://example.com/phoenix-shopping-center-sale",
        "<title>Phoenix shopping center sells to Washington investor</title>",
    )


@pytest.mark.parametrize(
    ("url", "outside"),
    [
        ("https://example.com/sitemap-202605.xml", True),
        ("https://example.com/sitemap-202606.xml", False),
        ("https://example.com/sitemaps/2026-08/posts.xml", False),
        ("https://example.com/sitemaps/2026/09/posts.xml", True),
        ("https://example.com/posts.xml?year=2026&month=05", True),
        ("https://example.com/posts.xml?month=8&year=2026", False),
    ],
)
def test_monthly_listing_partition_pruning_uses_month_overlap(url, outside):
    assert _listing_partition_outside_window(
        url, date(2026, 6, 15), date(2026, 8, 10)
    ) is outside


def test_curated_direct_listing_reports_document_cap(tmp_path):
    root_url = "https://example.com/sitemap-index.xml?year=2026"
    source = CuratedSource(
        source_id="source-cap",
        name="Arizona source",
        url=root_url,
        domain="example.com",
    )
    children = [
        f"https://example.com/day.xml?date=2026-08-28&page={index}"
        for index in range(101)
    ]
    index_xml = "<sitemapindex>" + "".join(
        f"<sitemap><loc>{url.replace('&', '&amp;')}</loc></sitemap>" for url in children
    ) + "</sitemapindex>"

    def fetch(url):
        if url == root_url:
            return response(url, index_xml)
        if url in children:
            return response(url, "<urlset></urlset>")
        raise RuntimeError("not found")

    store = StateStore(tmp_path / "scout.db")
    store.migrate()
    store.create_run("run-cap", "2026-08-28", "2026-06-01")
    artifacts = ArtifactStore(tmp_path / "results", "2026-08-28", "run-cap", store)
    batch = CuratedSiteAdapter(
        [source], store, artifacts, fetch=fetch, workers=1
    ).discover("run-cap", date(2026, 6, 1), until=date(2026, 8, 28))

    assert any(
        item["error"] == "direct_listing_document_cap_reached"
        for item in batch.source_errors
    )


def test_curated_adapter_persists_valid_and_undated_review(tmp_path):
    source = CuratedSource(
        source_id="source-1",
        name="Example News",
        url="https://example.com",
        domain="example.com",
    )
    pages = {
        "https://example.com/": """
            <a href="/2026/08/28/a-new-commercial-project-opens">A new commercial project opens in Phoenix today</a>
            <a href="/news/another-commercial-development-story">Another commercial development story in Arizona</a>
        """,
        "https://example.com/2026/08/28/a-new-commercial-project-opens": (
            '<meta property="article:published_time" content="2026-08-28T08:00:00Z">'
        ),
        "https://example.com/news/another-commercial-development-story": "<html>no date</html>",
    }

    def fetch(url: str) -> FetchResponse:
        canonical = url if url != "https://example.com" else "https://example.com/"
        return response(canonical, pages[canonical])

    store = StateStore(tmp_path / "scout.db")
    store.migrate()
    store.create_run("run-1", "2026-08-28", "2026-08-27")
    artifacts = ArtifactStore(tmp_path / "results", "2026-08-28", "run-1", store)
    batch = CuratedSiteAdapter([source], store, artifacts, fetch=fetch, workers=1).discover(
        "run-1", date(2026, 8, 27)
    )

    assert [item.record_status for item in batch.candidates] == [
        RecordStatus.VALID,
        RecordStatus.REVIEW,
    ]
    assert len(batch.reviews) == 1
    assert len(store.candidates_for_run("run-1")) == 2


def test_curated_adapter_excludes_candidates_after_until_date(tmp_path):
    source = CuratedSource(
        source_id="source-1",
        name="Example News",
        url="https://example.com",
        domain="example.com",
    )
    pages = {
        "https://example.com/": """
            <a href="/2026/08/28/commercial-project-opens-in-phoenix">Commercial project opens in Phoenix today</a>
            <a href="/2026/08/29/commercial-project-opens-in-tempe">Commercial project opens in Tempe today</a>
        """,
        "https://example.com/2026/08/28/commercial-project-opens-in-phoenix": "<html></html>",
        "https://example.com/2026/08/29/commercial-project-opens-in-tempe": "<html></html>",
    }

    def fetch(url: str) -> FetchResponse:
        canonical = url if url != "https://example.com" else "https://example.com/"
        return response(canonical, pages[canonical])

    store = StateStore(tmp_path / "scout.db")
    store.migrate()
    store.create_run("run-1", "2026-08-28", "2026-08-28")
    artifacts = ArtifactStore(tmp_path / "results", "2026-08-28", "run-1", store)
    batch = CuratedSiteAdapter([source], store, artifacts, fetch=fetch, workers=1).discover(
        "run-1", date(2026, 8, 28), until=date(2026, 8, 28)
    )

    assert [item.published_at.date() for item in batch.candidates] == [date(2026, 8, 28)]


def test_feed_candidates_exclude_entries_after_until_date(tmp_path):
    source = CuratedSource(
        source_id="source-1",
        name="Example",
        url="https://example.com",
        domain="example.com",
    )
    feed = """<rss><channel>
      <item><guid>yesterday</guid><title>Yesterday</title>
      <link>https://example.com/yesterday</link>
      <pubDate>Fri, 28 Aug 2026 10:00:00 GMT</pubDate></item>
      <item><guid>today</guid><title>Today</title>
      <link>https://example.com/today</link>
      <pubDate>Sat, 29 Aug 2026 10:00:00 GMT</pubDate></item>
    </channel></rss>"""
    store = StateStore(tmp_path / "scout.db")
    store.migrate()
    store.create_run("run-1", "2026-08-28", "2026-08-28")
    store.upsert_source(source.source_id, source.name, source.url, source.domain)
    artifacts = ArtifactStore(tmp_path / "results", "2026-08-28", "run-1", store)
    registry = FeedRegistry(store, artifacts, fetch=lambda url: response(url, feed))
    _, entries = registry.validate_and_store(
        source, "https://example.com/feed.xml", "autodiscovery"
    )
    batch = registry.candidates(
        "run-1",
        source,
        entries,
        date(2026, 8, 28),
        until=date(2026, 8, 28),
    )

    assert [item.title for item in batch.candidates] == ["Yesterday"]


def test_feed_validation_and_health_thresholds(tmp_path):
    source = CuratedSource(
        source_id="source-1",
        name="Example",
        url="https://example.com",
        domain="example.com",
    )
    feed = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><guid>one</guid><title>Opening</title><link>https://example.com/news/opening</link>
      <pubDate>Fri, 28 Aug 2026 10:00:00 GMT</pubDate></item>
    </channel></rss>"""
    store = StateStore(tmp_path / "scout.db")
    store.migrate()
    store.create_run("run-1", "2026-08-28", "2026-08-27")
    store.upsert_source(source.source_id, source.name, source.url, source.domain)
    artifacts = ArtifactStore(tmp_path / "results", "2026-08-28", "run-1", store)
    registry = FeedRegistry(store, artifacts, fetch=lambda url: response(url, feed))

    status, entries = registry.validate_and_store(source, "https://example.com/feed.xml", "autodiscovery")
    assert status == FeedStatus.ACTIVE
    assert entries[0]["provider_id"] == "one"
    assert feed_status_after_failure(2, 1) == FeedStatus.PENDING
    assert feed_status_after_failure(3, 1) == FeedStatus.DEGRADED
    assert feed_status_after_failure(7, 1) == FeedStatus.DISABLED


def test_same_feed_discovered_by_duplicate_curated_sources_is_idempotent(tmp_path):
    sources = [
        CuratedSource(
            source_id=f"source-{index}",
            name=f"Example {index}",
            url=f"https://example.com/news-{index}",
            domain="example.com",
        )
        for index in (1, 2)
    ]
    feed = """<rss><channel><item><guid>one</guid><title>Opening</title>
      <link>https://example.com/opening</link>
      <pubDate>Fri, 28 Aug 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
    store = StateStore(tmp_path / "scout.db")
    store.migrate()
    for source in sources:
        store.upsert_source(source.source_id, source.name, source.url, source.domain)
    store.create_run("run-1", "2026-08-28", "2026-08-27")
    registry = FeedRegistry(
        store,
        ArtifactStore(tmp_path / "results", "2026-08-28", "run-1", store),
        fetch=lambda url: response(url, feed),
    )
    for source in sources:
        registry.validate_and_store(source, "https://example.com/feed.xml", "autodiscovery")
    assert len(store.feeds()) == 1


def test_provider_preflight_and_bounded_results():
    with pytest.raises(ProviderPreflightError, match="NEWSAPI_AI_API_KEY"):
        NewsApiAdapter(api_key="").preflight()

    calls = []

    def post_json(url, payload, timeout):
        calls.append(payload)
        return {
            "articles": {
                "results": [
                    {
                        "uri": f"id-{len(calls)}",
                        "url": f"https://example.com/{len(calls)}",
                        "title": "Arizona opening",
                        "date": "2026-08-28",
                    }
                ],
                "pages": 1,
            }
        }

    records = NewsApiAdapter(api_key="key", max_pages=0, post_json=post_json).discover(
        date(2026, 8, 27), date(2026, 8, 28)
    )
    assert len(calls) == 10
    assert len(records) == 10

    rows = [{"id": str(i), "url": f"https://facebook.com/{i}", "text": "opening"} for i in range(30)]
    apify = ApifyFacebookAdapter(
        token="token",
        actor_id="actor",
        run_actor=lambda token, actor, payload, timeout: rows,
    )
    assert len(apify.discover(date(2026, 8, 27), date(2026, 8, 28))) == 20


def test_fuzzy_dedup_requires_exact_coverage():
    assert validate_fuzzy_groups(
        ["a", "b"], [{"kept_id": "a", "member_ids": ["a", "b"]}]
    )[0].member_ids == ("a", "b")
    with pytest.raises(DedupContractError, match=r"missing=\['b'\]"):
        validate_fuzzy_groups(["a", "b"], [{"kept_id": "a", "member_ids": ["a"]}])
    with pytest.raises(DedupContractError, match=r"unknown=\['x'\]"):
        validate_fuzzy_groups(["a"], [{"kept_id": "a", "member_ids": ["a", "x"]}])
