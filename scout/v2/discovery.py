"""Curated-site and learned-feed discovery behind one typed contract."""
from __future__ import annotations

import csv
import gzip
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qsl, urljoin, urlsplit

import feedparser
from pydantic import BaseModel, ConfigDict, Field

from .artifacts import ArtifactStore
from .contracts import DiscoveryCandidate, FeedStatus, RecordStatus, ReviewItem
from .http import FetchResponse, HttpFetcher
from .ids import candidate_id, canonicalize_url, normalize_text, stable_hash, stable_uuid
from .state import StateStore


MAX_LINKS_PER_SOURCE = 25
MAX_DIRECT_LISTING_DOCUMENTS = 100
MAX_DIRECT_LISTING_DEPTH = 4
MAX_DIRECT_LISTING_ITEMS = 1_000
ARIZONA_SCOPE_TERMS = (
    "arizona", "phoenix", "tucson", "mesa", "scottsdale", "tempe",
    "chandler", "gilbert", "glendale", "peoria", "surprise", "goodyear",
    "avondale", "flagstaff", "prescott", "yuma", "buckeye", "queen creek",
    "maricopa", "pinal", "casa grande", "lake havasu", "bullhead city",
    "apache junction", "oro valley", "sierra vista", "fountain hills",
    "cottonwood", "sedona", "nogales", "kingman", "marana", "sahuarita",
    "san tan valley",
)
NON_ARIZONA_STATE_TERMS = (
    "alabama", "alaska", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana",
    "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland",
    "massachusetts", "michigan", "minnesota", "mississippi", "missouri",
    "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
    "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee",
    "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming",
)
NON_ARIZONA_LOCALITY_TERMS = (
    "oak harbor", "puget sound", "whidbey island", "albuquerque", "chicago",
    "wilmette",
)
COMMON_FEED_PATHS = ("/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml")
ARTICLE_HINTS = (
    "article",
    "news",
    "business",
    "real-estate",
    "commercial",
    "development",
    "construction",
    "project",
    "opening",
    "lease",
    "tenant",
    "acquisition",
    "property",
    "multifamily",
    "industrial",
    "retail",
    "office",
    "azre",
    "blog",
    "buildout",
    "groundbreaking",
    "permit",
    "permits",
    "redevelopment",
    "rezoning",
    "ribbon-cutting",
    "tenant-improvement",
)
TITLE_EVENT_TERMS = (
    "acquired",
    "acquisition",
    "approved",
    "breaks ground",
    "completed",
    "construction",
    "expands",
    "expansion",
    "groundbreaking",
    "lease",
    "leased",
    "opens",
    "opening",
    "plans",
    "redevelopment",
    "relocates",
    "renovation",
    "rezoning",
    "ribbon cutting",
    "site acquired",
)
TITLE_PROPERTY_TERMS = (
    "apartments",
    "campus",
    "center",
    "commercial",
    "community",
    "development",
    "facility",
    "hotel",
    "industrial",
    "medical office",
    "mixed use",
    "multifamily",
    "office",
    "project",
    "property",
    "restaurant",
    "retail",
    "self storage",
    "store",
    "warehouse",
)
REJECT_SEGMENTS = {
    "about",
    "advertise",
    "author",
    "authors",
    "careers",
    "category",
    "contact",
    "events",
    "login",
    "privacy",
    "search",
    "subscribe",
    "tag",
    "terms",
}
REJECT_EXTENSIONS = (
    ".css",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".rss",
    ".svg",
    ".webp",
    ".xml",
    ".zip",
)
DATE_META_KEYS = {
    "article:published_time",
    "date",
    "datepublished",
    "dc.date",
    "parsely-pub-date",
    "pubdate",
    "publishdate",
    "sailthru.date",
}
MULTIPART_SUFFIXES = {"co.uk", "com.au", "com.br", "co.nz", "co.jp", "com.mx"}


class CuratedSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    name: str
    url: str
    domain: str
    state: str = "Arizona"
    enabled: bool = True


class DiscoveryBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[DiscoveryCandidate] = Field(default_factory=list)
    reviews: list[ReviewItem] = Field(default_factory=list)
    source_errors: list[dict] = Field(default_factory=list)


@dataclass(slots=True)
class ParsedIndex:
    article_links: list[tuple[str, str]]
    feed_links: list[str]


@dataclass(slots=True, frozen=True)
class DirectListingEntry:
    url: str
    title: str = ""
    published_at: datetime | None = None


@dataclass(slots=True)
class DirectListing:
    kind: str
    entries: list[DirectListingEntry]
    children: list[str]
    truncated: bool = False


class _IndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.feed_links: list[str] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {str(key).lower(): value for key, value in attrs if key and value}
        if tag.lower() == "a" and self._href is None and attr_map.get("href"):
            self._href = str(attr_map["href"])
            self._text = []
        if tag.lower() == "link":
            rel = {part.casefold() for part in str(attr_map.get("rel", "")).split()}
            media_type = str(attr_map.get("type", "")).casefold()
            href = attr_map.get("href")
            if "alternate" in rel and href and media_type in {
                "application/rss+xml",
                "application/atom+xml",
                "application/feed+json",
            }:
                self.feed_links.append(str(href))

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, " ".join(" ".join(self._text).split())))
            self._href = None
            self._text = []


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.date_values: list[str] = []
        self.json_ld: list[str] = []
        self._json_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {str(key).lower(): value for key, value in attrs if key and value}
        if tag.lower() == "meta":
            key = str(attr_map.get("property") or attr_map.get("name") or attr_map.get("itemprop") or "").casefold()
            content = attr_map.get("content")
            if key in DATE_META_KEYS and content:
                self.date_values.append(str(content))
        elif tag.lower() == "time" and attr_map.get("datetime"):
            self.date_values.append(str(attr_map["datetime"]))
        elif tag.lower() == "script" and str(attr_map.get("type", "")).casefold() == "application/ld+json":
            self._json_parts = []

    def handle_data(self, data: str) -> None:
        if self._json_parts is not None:
            self._json_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._json_parts is not None:
            self.json_ld.append("".join(self._json_parts))
            self._json_parts = None


def load_curated_sources(path: str | Path) -> list[CuratedSource]:
    with Path(path).open(newline="", encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file))
    sources: list[CuratedSource] = []
    for row in rows:
        url = str(row.get("URL") or row.get("url") or "").strip()
        if not url:
            continue
        if "://" not in url:
            url = f"https://{url}"
        canonical = canonicalize_url(url)
        name = str(
            row.get("Resource Name")
            or row.get("name")
            or urlsplit(canonical).hostname
            or ""
        ).strip()
        domain = (urlsplit(canonical).hostname or "").lower()
        sources.append(
            CuratedSource(
                source_id=stable_uuid("source", canonical),
                name=name,
                url=canonical,
                domain=domain,
            )
        )
    return sources


def parse_index(html: str, base_url: str) -> ParsedIndex:
    parser = _IndexParser()
    parser.feed(html)
    seen: set[str] = set()
    articles: list[tuple[str, str]] = []
    for href, title in parser.links:
        try:
            url = canonicalize_url(urljoin(base_url, href))
        except ValueError:
            continue
        if not same_site(url, base_url) or url in seen or article_score(url, title, base_url) < 3:
            continue
        seen.add(url)
        articles.append((url, title))
    feeds: list[str] = []
    for href in parser.feed_links:
        try:
            url = canonicalize_url(urljoin(base_url, href))
        except ValueError:
            continue
        if same_registrable_domain(url, base_url) and url not in feeds:
            feeds.append(url)
    return ParsedIndex(articles[:MAX_LINKS_PER_SOURCE], feeds)


def parse_direct_listing(
    content: bytes,
    base_url: str,
    source_url: str,
    *,
    max_items: int = MAX_DIRECT_LISTING_ITEMS,
) -> DirectListing | None:
    """Parse a bounded exact curated listing, or return ``None`` for HTML.

    Supported listings are WordPress post arrays, JSON Feed, RSS/Atom,
    sitemap urlsets, and sitemap indexes. Every emitted URL remains on the
    curated source's registrable domain.
    """
    if max_items <= 0:
        return DirectListing(kind="bounded", entries=[], children=[])
    payload = gzip.decompress(content) if content[:2] == b"\x1f\x8b" else content
    stripped = payload.lstrip()
    if stripped[:1] in {b"[", b"{"}:
        try:
            value = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if isinstance(value, list):
            entries = _json_listing_entries(
                value, base_url, source_url, max_items=max_items, wordpress=True
            )
            return DirectListing(
                kind="wordpress",
                entries=entries,
                children=[],
                truncated=len(value) > max_items,
            )
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            entries = _json_listing_entries(
                value["items"], base_url, source_url, max_items=max_items, wordpress=False
            )
            return DirectListing(
                kind="json-feed",
                entries=entries,
                children=[],
                truncated=len(value["items"]) > max_items,
            )
        return None
    if stripped[:1] != b"<":
        return None
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None
    root_name = _xml_local(root.tag)
    if root_name in {"rss", "feed", "rdf"}:
        parsed = feedparser.parse(payload)
        valid, _ = _valid_feed_entries(parsed.entries, source_url, max_entries=max_items)
        return DirectListing(
            kind="feed",
            entries=[
                DirectListingEntry(
                    url=item["url"],
                    title=item["title"],
                    published_at=item["published_at"],
                )
                for item in valid
            ],
            children=[],
            truncated=len(parsed.entries) > max_items,
        )
    if root_name == "sitemapindex":
        children: list[str] = []
        for node in root:
            raw_url = _xml_child_text(node, "loc")
            url = _same_domain_url(raw_url, base_url, source_url)
            if url and url not in children:
                children.append(url)
                if len(children) >= max_items:
                    break
        return DirectListing(
            kind="sitemapindex",
            entries=[],
            children=children,
            truncated=len(root) > max_items,
        )
    if root_name == "urlset":
        entries: list[DirectListingEntry] = []
        seen: set[str] = set()
        for node in root:
            url = _same_domain_url(_xml_child_text(node, "loc"), base_url, source_url)
            if not url or url in seen:
                continue
            seen.add(url)
            # News sitemap publication_date is authoritative. Ordinary
            # sitemap lastmod is deliberately not treated as publication.
            published = parse_datetime(_xml_descendant_text(node, "publication_date"))
            entries.append(
                DirectListingEntry(
                    url=url,
                    title=_xml_descendant_text(node, "title"),
                    published_at=published,
                )
            )
            if len(entries) >= max_items:
                break
        return DirectListing(
            kind="urlset",
            entries=entries,
            children=[],
            truncated=len(root) > max_items,
        )
    return None


def listing_query_date(url: str) -> date | None:
    """Return a recognized exact ``?date=YYYY-MM-DD`` listing partition."""
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if key.casefold() != "date" or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            continue
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _listing_partition_outside_window(
    url: str, since: date, until: date | None
) -> bool:
    exact = listing_query_date(url)
    if exact:
        return exact < since or (until is not None and exact > until)
    parts = urlsplit(url)
    query = {key.casefold(): value for key, value in parse_qsl(parts.query)}
    year = query.get("year", "")
    month_value = query.get("month", "")
    if re.fullmatch(r"20\d{2}", year) and re.fullmatch(r"0?[1-9]|1[0-2]", month_value):
        month = (int(year), int(month_value))
        return month < (since.year, since.month) or (
            until is not None and month > (until.year, until.month)
        )
    match = re.search(
        r"(?<!\d)(20\d{2})(?:[-_/]?)(0[1-9]|1[0-2])(?!\d)",
        parts.path,
    )
    if match:
        month = (int(match.group(1)), int(match.group(2)))
        return month < (since.year, since.month) or (
            until is not None and month > (until.year, until.month)
        )
    return False


def _direct_listing_in_arizona_scope(title: str, url: str, page_html: str) -> bool:
    document_title = ""
    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", page_html[:100_000])
    if title_match:
        document_title = re.sub(r"(?s)<[^>]+>", " ", title_match.group(1))
    metadata = " ".join(
        re.findall(
            r'''(?is)<meta[^>]+(?:name|property)=["'](?:description|og:title|og:description|twitter:title)["'][^>]+content=["']([^"']+)["']''',
            page_html[:100_000],
        )
    )
    event_text = normalize_text(f"{title} {url} {document_title} {metadata}")
    if any(
        re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", event_text)
        for term in NON_ARIZONA_LOCALITY_TERMS
    ):
        return False
    event_nouns = (
        r"property|project|center|plaza|development|facility|store|restaurant|"
        r"warehouse|office|apartments?|community|campus|site"
    )
    for state in NON_ARIZONA_STATE_TERMS:
        escaped = re.escape(state)
        if re.search(
            rf"\b(?:in|near|at|outside|located in) {escaped}\b"
            rf"|\b{escaped} (?:{event_nouns})\b",
            event_text,
        ):
            return False
    value = normalize_text(f"{event_text} {page_html[:200_000]}")
    # An Arizona owner/investor address does not make an out-of-state property
    # event local. Remove those identity phrases before evaluating locality.
    value = re.sub(
        r"\barizona(?: based)? (?:investor|owner|developer|company|firm)\b",
        " ",
        value,
    )
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", value)
        for term in ARIZONA_SCOPE_TERMS
    )


def _json_listing_entries(
    rows: list,
    base_url: str,
    source_url: str,
    *,
    max_items: int,
    wordpress: bool,
) -> list[DirectListingEntry]:
    entries: list[DirectListingEntry] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_url = row.get("link") if wordpress else row.get("url") or row.get("external_url")
        url = _same_domain_url(str(raw_url or ""), base_url, source_url)
        if not url or url in seen:
            continue
        seen.add(url)
        raw_title = row.get("title") or ""
        if isinstance(raw_title, dict):
            raw_title = raw_title.get("rendered") or raw_title.get("raw") or ""
        raw_date = (
            row.get("date_gmt") or row.get("date")
            if wordpress
            else row.get("date_published")
        )
        entries.append(
            DirectListingEntry(
                url=url,
                title=str(raw_title),
                published_at=parse_datetime(str(raw_date or "")),
            )
        )
        if len(entries) >= max_items:
            break
    return entries


def _same_domain_url(raw_url: str, base_url: str, source_url: str) -> str:
    try:
        url = canonicalize_url(urljoin(base_url, raw_url))
    except ValueError:
        return ""
    return url if same_registrable_domain(url, source_url) else ""


def _xml_local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _xml_child_text(node: ET.Element, name: str) -> str:
    for child in node:
        if _xml_local(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _xml_descendant_text(node: ET.Element, name: str) -> str:
    for child in node.iter():
        if _xml_local(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def article_score(url: str, title: str, base_url: str) -> int:
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/") or "/"
    base_path = urlsplit(base_url).path.rstrip("/") or "/"
    if path in {"/", base_path} or path.casefold().endswith(REJECT_EXTENSIONS):
        return 0
    segments = [segment for segment in path.casefold().split("/") if segment]
    if any(segment in REJECT_SEGMENTS for segment in segments):
        return 0
    score = 3 if date_from_url(url) else 0
    if any(hint in path.casefold() for hint in ARTICLE_HINTS):
        score += 2
    slug_words = re.split(r"[-_\s]+", segments[-1] if segments else "")
    if len([word for word in slug_words if word]) >= 4:
        score += 2
    if len(title.strip()) >= 25:
        score += 1
    title_text = normalize_text(title)
    if any(term in title_text for term in TITLE_EVENT_TERMS) and any(
        term in title_text for term in TITLE_PROPERTY_TERMS
    ):
        score += 2
    if any(
        re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", title_text)
        for term in ARIZONA_SCOPE_TERMS
    ):
        score += 1
    return score


def publication_date(html: str, url: str) -> datetime | None:
    """Prefer structured page metadata and only then inspect the URL."""
    parser = _MetadataParser()
    parser.feed(html)
    values = list(parser.date_values)
    for raw in parser.json_ld:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        values.extend(_json_ld_dates(payload))
    for value in values:
        parsed = parse_datetime(value)
        if parsed:
            return parsed
    from_url = date_from_url(url)
    return datetime.combine(from_url, time.min, tzinfo=timezone.utc) if from_url else None


def parse_datetime(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            try:
                parsed = datetime.strptime(raw[:10], "%Y-%m-%d")
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def date_from_url(url: str) -> date | None:
    parts = [part for part in urlsplit(url).path.split("/") if part]
    for index in range(len(parts) - 2):
        if not all(part.isdigit() for part in parts[index : index + 3]):
            continue
        year = int(parts[index])
        if 2000 <= year <= 2100:
            try:
                return date(year, int(parts[index + 1]), int(parts[index + 2]))
            except ValueError:
                return None
    match = re.search(r"(?<!\d)(20\d{2})[-_](\d{1,2})[-_](\d{1,2})(?!\d)", urlsplit(url).path)
    if match:
        try:
            return date(*(int(part) for part in match.groups()))
        except ValueError:
            return None
    return None


def same_site(left: str, right: str) -> bool:
    a = (urlsplit(left).hostname or "").casefold().removeprefix("www.")
    b = (urlsplit(right).hostname or "").casefold().removeprefix("www.")
    return bool(a and b and (a == b or a.endswith(f".{b}") or b.endswith(f".{a}")))


def registrable_domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").casefold().strip(".")
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    suffix = ".".join(labels[-2:])
    return ".".join(labels[-3:]) if suffix in MULTIPART_SUFFIXES and len(labels) >= 3 else suffix


def same_registrable_domain(left: str, right: str) -> bool:
    return bool(registrable_domain(left) and registrable_domain(left) == registrable_domain(right))


class CuratedSiteAdapter:
    name = "curated"

    def __init__(
        self,
        sources: Iterable[CuratedSource],
        state: StateStore,
        artifacts: ArtifactStore,
        fetch: Callable[[str], FetchResponse] | None = None,
        workers: int = 5,
    ):
        self.sources = [source for source in sources if source.enabled]
        self.state = state
        self.artifacts = artifacts
        self.fetch = fetch or HttpFetcher()
        self.workers = workers

    def discover(
        self,
        run_id: str,
        since: date,
        max_candidates: int = 0,
        until: date | None = None,
    ) -> DiscoveryBatch:
        for source in self.sources:
            self.state.upsert_source(
                source.source_id, source.name, source.url, source.domain, source.state, source.enabled
            )
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            source_batches = list(
                pool.map(
                    lambda source: self._discover_source(run_id, since, until, source),
                    self.sources,
                )
            )
        batch = DiscoveryBatch()
        by_id: dict[str, DiscoveryCandidate] = {}
        for source_batch in source_batches:
            batch.reviews.extend(source_batch.reviews)
            batch.source_errors.extend(source_batch.source_errors)
            for candidate in source_batch.candidates:
                by_id.setdefault(candidate.candidate_id, candidate)
        candidates = list(by_id.values())
        if max_candidates > 0:
            candidates = candidates[:max_candidates]
        for candidate in candidates:
            self.state.save_candidate(candidate)
        for review in batch.reviews:
            self.state.add_review(review)
        batch.candidates = candidates
        return batch

    def _discover_source(
        self,
        run_id: str,
        since: date,
        until: date | None,
        source: CuratedSource,
    ) -> DiscoveryBatch:
        batch = DiscoveryBatch()
        try:
            index = self.fetch(source.url)
            index_artifact = self.artifacts.write_raw_text(
                "discover", f"source-{source.source_id}.html", index.text
            )
            direct = parse_direct_listing(index.content, index.url, source.url)
            parsed = parse_index(index.text, index.url) if direct is None else None
        except Exception as error:
            batch.source_errors.append(
                {"source_id": source.source_id, "url": source.url, "error": repr(error)}
            )
            return batch
        listing_entries: list[DirectListingEntry] = []
        direct_truncated = False
        if direct is not None:
            listing_entries.extend(direct.entries)
            direct_truncated = direct.truncated
            queue = [(url, 1) for url in direct.children]
            seen_docs = {canonicalize_url(source.url)}
            while queue and len(seen_docs) < MAX_DIRECT_LISTING_DOCUMENTS:
                listing_url, depth = queue.pop(0)
                if _listing_partition_outside_window(listing_url, since, until):
                    continue
                if listing_url in seen_docs or depth > MAX_DIRECT_LISTING_DEPTH:
                    continue
                seen_docs.add(listing_url)
                try:
                    response = self.fetch(listing_url)
                    child = parse_direct_listing(
                        response.content,
                        response.url,
                        source.url,
                        max_items=max(0, MAX_DIRECT_LISTING_ITEMS - len(listing_entries)),
                    )
                    if child is None:
                        continue
                    direct_truncated = direct_truncated or child.truncated
                    listing_entries.extend(child.entries)
                    queue.extend((url, depth + 1) for url in child.children)
                except Exception as error:
                    batch.source_errors.append(
                        {"source_id": source.source_id, "url": listing_url, "error": repr(error)}
                    )
                if len(listing_entries) >= MAX_DIRECT_LISTING_ITEMS:
                    direct_truncated = direct_truncated or bool(queue)
                    break
            if queue:
                batch.source_errors.append(
                    {
                        "source_id": source.source_id,
                        "url": source.url,
                        "error": "direct_listing_document_cap_reached",
                    }
                )
            if direct_truncated:
                batch.source_errors.append(
                    {
                        "source_id": source.source_id,
                        "url": source.url,
                        "error": "direct_listing_item_cap_reached",
                    }
                )
            links = [(item.url, item.title, item.published_at) for item in listing_entries]
        else:
            links = [(url, title, None) for url, title in parsed.article_links]
        seen_articles: set[str] = set()
        for link, title, listing_published in links:
            if link in seen_articles:
                continue
            seen_articles.add(link)
            if listing_published and (
                listing_published.date() < since
                or (until is not None and listing_published.date() > until)
            ):
                continue
            try:
                page = self.fetch(link)
                canonical = canonicalize_url(page.url)
                if not same_registrable_domain(canonical, source.url):
                    continue
                artifact = self.artifacts.write_raw_text(
                    "discover", f"article-{stable_hash(canonical)[:20]}.html", page.text
                )
                published = publication_date(page.text, canonical) or listing_published
                page_html = page.text
                errors: list[str] = []
            except Exception as error:
                canonical = canonicalize_url(link)
                published_date = date_from_url(canonical)
                published = listing_published or (
                    datetime.combine(published_date, time.min, tzinfo=timezone.utc)
                    if published_date
                    else None
                )
                artifact = index_artifact
                errors = [f"article_fetch_failed:{type(error).__name__}"]
                page_html = ""
            if published and (
                published.date() < since
                or (until is not None and published.date() > until)
            ):
                continue
            if published is None:
                errors.append("publication_date_missing")
            if direct is not None and not _direct_listing_in_arizona_scope(
                title, canonical, page_html
            ):
                errors.append("direct_listing_arizona_scope_unverified")
            record_status = RecordStatus.REVIEW if errors else RecordStatus.VALID
            cid = candidate_id(self.name, "", canonical)
            candidate = DiscoveryCandidate(
                candidate_id=cid,
                run_id=run_id,
                provider=self.name,
                discovered_url=link,
                resolved_url=canonical,
                canonical_url=canonical,
                title=title,
                source_id=source.source_id,
                source_name=source.name,
                source_domain=source.domain,
                published_at=published,
                raw_artifact_path=artifact["path"],
                raw_artifact_hash=artifact["sha256"],
                record_status=record_status,
                validation_errors=errors,
            )
            batch.candidates.append(candidate)
            if record_status == RecordStatus.REVIEW:
                batch.reviews.append(_candidate_review(candidate, "candidate_metadata_invalid"))
        return batch


class FeedRegistry:
    def __init__(
        self,
        state: StateStore,
        artifacts: ArtifactStore,
        fetch: Callable[[str], FetchResponse] | None = None,
    ):
        self.state = state
        self.artifacts = artifacts
        self.fetch = fetch or HttpFetcher()

    def discover_for_source(self, source: CuratedSource, html: str) -> list[str]:
        parsed = parse_index(html, source.url)
        candidates = [*parsed.feed_links, *(urljoin(source.url, path) for path in COMMON_FEED_PATHS)]
        out: list[str] = []
        for value in candidates:
            try:
                url = canonicalize_url(value)
            except ValueError:
                continue
            if same_registrable_domain(url, source.url) and url not in out:
                out.append(url)
        return out

    def validate_and_store(self, source: CuratedSource, url: str, method: str) -> tuple[FeedStatus, list[dict]]:
        url = canonicalize_url(url)
        prior = next((row for row in self.state.feeds() if row["url"] == url), None)
        feed_id = prior["feed_id"] if prior else stable_uuid("feed", url)
        failures = int(prior["consecutive_failures"]) if prior else 0
        now = datetime.now(timezone.utc)
        try:
            response = self.fetch(url)
            parsed = feedparser.parse(response.content)
            entries, problems = _valid_feed_entries(parsed.entries, source.url)
            if not entries:
                raise ValueError("feed has no valid same-domain article entries")
            if problems and len(problems) > len(entries):
                status = FeedStatus.QUARANTINED
                error = f"predominantly_invalid_entries:{Counter(problems).most_common()}"
            else:
                status = FeedStatus.ACTIVE
                error = ""
            latest = max((item["published_at"] for item in entries if item["published_at"]), default=now)
            failures = 0
            chain = [*response.history, response.url]
            artifact = self.artifacts.write_raw_text(
                "discover-rss", f"feed-{stable_hash(url)[:20]}.xml", response.text
            )
            for entry in entries:
                entry["feed_url"] = url
                entry["raw_artifact_path"] = artifact["path"]
                entry["raw_artifact_hash"] = artifact["sha256"]
        except Exception as exc:
            failures += 1
            latest = parse_datetime(prior["last_valid_item_at"]) if prior and prior["last_valid_item_at"] else None
            inactive_days = (now - latest).days if latest else 999
            status = feed_status_after_failure(failures, inactive_days)
            error = f"{type(exc).__name__}:{exc}"
            chain = []
            entries = []
        self.state.upsert_feed(
            feed_id,
            source.source_id,
            url,
            status.value,
            method,
            redirect_chain=chain,
            consecutive_failures=failures,
            last_valid_item_at=latest.isoformat() if latest else None,
            last_checked_at=now.isoformat(),
            validation_error=error,
        )
        return status, entries

    def candidates(
        self,
        run_id: str,
        source: CuratedSource,
        entries: Iterable[dict],
        since: date,
        until: date | None = None,
    ) -> DiscoveryBatch:
        batch = DiscoveryBatch()
        for entry in entries:
            published = entry["published_at"]
            if published and (
                published.date() < since
                or (until is not None and published.date() > until)
            ):
                continue
            errors = [] if published else ["publication_date_missing"]
            status = RecordStatus.REVIEW if errors else RecordStatus.VALID
            raw_provider_id = entry.get("provider_id", "")
            feed_identity = f"{entry.get('feed_url', '')}#{raw_provider_id}" if raw_provider_id else ""
            cid = candidate_id("rss", feed_identity, entry["url"])
            candidate = DiscoveryCandidate(
                candidate_id=cid,
                run_id=run_id,
                provider="rss",
                provider_id=feed_identity,
                discovered_url=entry["url"],
                resolved_url=entry["url"],
                canonical_url=entry["url"],
                title=entry.get("title", ""),
                source_id=source.source_id,
                source_name=source.name,
                source_domain=source.domain,
                published_at=published,
                raw_artifact_path=entry.get("raw_artifact_path", ""),
                raw_artifact_hash=entry.get("raw_artifact_hash", ""),
                record_status=status,
                validation_errors=errors,
                metadata={"feed_url": entry.get("feed_url", "")},
            )
            self.state.save_candidate(candidate)
            batch.candidates.append(candidate)
            if errors:
                review = _candidate_review(candidate, "rss_item_undated")
                self.state.add_review(review)
                batch.reviews.append(review)
        return batch


def feed_status_after_failure(consecutive_failures: int, inactive_days: int) -> FeedStatus:
    if consecutive_failures >= 7 or inactive_days >= 30:
        return FeedStatus.DISABLED
    if consecutive_failures >= 3 or inactive_days >= 14:
        return FeedStatus.DEGRADED
    return FeedStatus.PENDING


def _valid_feed_entries(
    entries: Iterable,
    source_url: str,
    *,
    max_entries: int = 0,
) -> tuple[list[dict], list[str]]:
    valid: list[dict] = []
    problems: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        link = str(entry.get("link") or "").strip()
        if not link:
            problems.append("missing_link")
            continue
        try:
            url = canonicalize_url(link)
        except ValueError:
            problems.append("invalid_url")
            continue
        if not same_registrable_domain(url, source_url):
            problems.append("off_domain")
            continue
        if url in seen:
            problems.append("duplicate")
            continue
        seen.add(url)
        struct_time = entry.get("published_parsed") or entry.get("updated_parsed")
        published = (
            datetime(
                struct_time.tm_year,
                struct_time.tm_mon,
                struct_time.tm_mday,
                tzinfo=timezone.utc,
            )
            if struct_time
            else parse_datetime(entry.get("published") or entry.get("updated"))
        )
        valid.append(
            {
                "url": url,
                "title": str(entry.get("title") or ""),
                "provider_id": str(entry.get("id") or ""),
                "published_at": published,
            }
        )
        if max_entries > 0 and len(valid) >= max_entries:
            break
    return valid, problems


def _json_ld_dates(value: object) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in {"datepublished", "datecreated", "uploaddate"} and isinstance(item, str):
                out.append(item)
            else:
                out.extend(_json_ld_dates(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(_json_ld_dates(item))
    return out


def _candidate_review(candidate: DiscoveryCandidate, reason: str) -> ReviewItem:
    return ReviewItem(
        review_id=stable_uuid("review", candidate.run_id, "discover", candidate.candidate_id, reason),
        run_id=candidate.run_id,
        stage="discover",
        record_type="discovery_candidate",
        record_id=candidate.candidate_id,
        reason_code=reason,
        validation_errors=candidate.validation_errors,
        raw_artifact_path=candidate.raw_artifact_path,
    )
