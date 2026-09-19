"""Manual-only discovery adapters for paid and external lead sources."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import quote, urlsplit

import httpx


NEWSAPI_ENDPOINT = "https://eventregistry.org/api/v1/article/getArticles"
NEWSAPI_HARD_MAX_PAGES = 100
NEWSAPI_PAGE_SIZE = 100
PROVIDER_WORKERS = 4
APIFY_RESULTS_PER_QUERY = 20
MAPSDATA_BASE_URL = "https://mapsdata.ai/api/v1"
AEC_QUERY_GROUPS = {
    "openings": "Arizona grand opening commercial property",
    "leases": "Arizona commercial lease tenant signed",
    "occupancy": "Arizona new tenant occupancy move in",
    "construction_completion": "Arizona construction completed commercial project",
    "redevelopment": "Arizona commercial redevelopment adaptive reuse",
    "management_change": "Arizona property management company selected transition",
    "expansion": "Arizona business expansion new facility",
    "multifamily_leaseup": "Arizona apartment lease up opening",
    "industrial_activation": "Arizona warehouse industrial facility opening",
    "retail_hospitality": "Arizona restaurant retail hotel opening",
}


class ProviderPreflightError(RuntimeError):
    pass


@dataclass(slots=True)
class ProviderRecord:
    provider: str
    provider_id: str
    url: str
    title: str
    published_at: str = ""
    source_name: str = ""
    raw: dict | None = None


class ProviderAdapter(Protocol):
    name: str

    def preflight(self) -> None: ...

    def discover(self, start: date, end: date) -> list[ProviderRecord]: ...


class NewsApiAdapter:
    name = "newsapi_ai"

    def __init__(
        self,
        api_key: str | None = None,
        max_pages: int | None = None,
        timeout: int | None = None,
        post_json: Callable[[str, dict, int], dict] | None = None,
    ):
        self.api_key = api_key if api_key is not None else os.environ.get("NEWSAPI_AI_API_KEY", "")
        configured_pages = os.environ.get("NEWSAPI_AI_MAX_PAGES", "0") if max_pages is None else str(max_pages)
        self.max_pages = NEWSAPI_HARD_MAX_PAGES if int(configured_pages) == 0 else min(
            max(int(configured_pages), 1), NEWSAPI_HARD_MAX_PAGES
        )
        self.timeout = int(timeout or os.environ.get("NEWSAPI_AI_TIMEOUT_SECONDS", "120"))
        self.post_json = post_json or _post_json

    def preflight(self) -> None:
        if not self.api_key.strip():
            raise ProviderPreflightError("NEWSAPI_AI_API_KEY is required")

    def discover(self, start: date, end: date) -> list[ProviderRecord]:
        self.preflight()
        records: dict[str, ProviderRecord] = {}
        for query_name, query in AEC_QUERY_GROUPS.items():
            for page in range(1, self.max_pages + 1):
                payload = {
                    "action": "getArticles",
                    "resultType": "articles",
                    "keyword": query,
                    "keywordSearchMode": "exact",
                    "keywordLoc": "title,body",
                    "sourceLocationUri": "http://en.wikipedia.org/wiki/United_States",
                    "lang": "eng",
                    "dataType": "news",
                    "dateStart": start.isoformat(),
                    "dateEnd": end.isoformat(),
                    "isDuplicateFilter": "skipDuplicates",
                    "articlesSortBy": "date",
                    "articlesSortByAsc": False,
                    "articlesCount": NEWSAPI_PAGE_SIZE,
                    "articlesPage": page,
                    "apiKey": self.api_key,
                }
                response = self.post_json(NEWSAPI_ENDPOINT, payload, self.timeout)
                if response.get("error"):
                    raise RuntimeError(f"NewsAPI error for {query_name}: {response['error']}")
                block = response.get("articles") or {}
                rows = block.get("results") or []
                for row in rows:
                    url = str(row.get("url") or "").strip()
                    if not url:
                        continue
                    records.setdefault(
                        url,
                        ProviderRecord(
                            provider=self.name,
                            provider_id=str(row.get("uri") or row.get("id") or ""),
                            url=url,
                            title=str(row.get("title") or ""),
                            published_at=str(row.get("dateTimePub") or row.get("date") or ""),
                            source_name=str((row.get("source") or {}).get("title") or ""),
                            raw={"query_group": query_name, "article": row},
                        ),
                    )
                pages = int(block.get("pages") or 0)
                if len(rows) < NEWSAPI_PAGE_SIZE or (pages and page >= pages):
                    break
        return list(records.values())


class ApifyFacebookAdapter:
    name = "apify_facebook"

    def __init__(
        self,
        token: str | None = None,
        actor_id: str | None = None,
        timeout: int | None = None,
        run_actor: Callable[[str, str, dict, int], list[dict]] | None = None,
    ):
        self.token = token if token is not None else os.environ.get("APIFY_TOKEN", "")
        self.actor_id = actor_id if actor_id is not None else os.environ.get("APIFY_FACEBOOK_ACTOR_ID", "")
        self.timeout = int(timeout or os.environ.get("APIFY_TIMEOUT_SECONDS", "120"))
        self.run_actor = run_actor or _run_apify_actor

    def preflight(self) -> None:
        missing = [
            name
            for name, value in (("APIFY_TOKEN", self.token), ("APIFY_FACEBOOK_ACTOR_ID", self.actor_id))
            if not value.strip()
        ]
        if missing:
            raise ProviderPreflightError(f"missing provider configuration: {', '.join(missing)}")

    def discover(self, start: date, end: date) -> list[ProviderRecord]:
        self.preflight()
        records: dict[str, ProviderRecord] = {}
        for query_name, query in AEC_QUERY_GROUPS.items():
            payload = {
                "searchQueries": [query],
                "maxPosts": APIFY_RESULTS_PER_QUERY,
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
            }
            for row in self.run_actor(self.token, self.actor_id, payload, self.timeout)[:APIFY_RESULTS_PER_QUERY]:
                url = str(row.get("url") or row.get("postUrl") or "").strip()
                if not url:
                    continue
                records.setdefault(
                    url,
                    ProviderRecord(
                        provider=self.name,
                        provider_id=str(row.get("id") or row.get("postId") or ""),
                        url=url,
                        title=str(row.get("text") or row.get("title") or "")[:500],
                        published_at=str(row.get("time") or row.get("timestamp") or ""),
                        source_name=str(row.get("pageName") or row.get("userName") or "Facebook"),
                        raw={"query_group": query_name, "post": row},
                    ),
                )
        return list(records.values())


class MapsDataAdapter:
    """Import completed MapsData jobs or exported CSV files as discovery records.

    The pipeline deliberately does not submit new MapsData scrapes. MapsData jobs
    are asynchronous and bill against workspace allowance, so creation belongs in
    an operator-controlled step. This adapter only reads an existing completed job
    or a local export.
    """

    name = "mapsdata"

    def __init__(
        self,
        api_key: str | None = None,
        csv_paths: list[str] | None = None,
        job_ids: list[str] | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
        get_json: Callable[[str, str, int], dict] | None = None,
        get_text: Callable[[str, int], str] | None = None,
    ):
        self.api_key = api_key if api_key is not None else os.environ.get("MAPSDATA_KEY", "")
        self.csv_paths = csv_paths if csv_paths is not None else _env_list("MAPSDATA_CSV")
        configured_jobs = [*(_env_list("MAPSDATA_JOB_IDS")), *(_env_list("MAPSDATA_JOB_ID"))]
        self.job_ids = job_ids if job_ids is not None else configured_jobs
        self.base_url = (base_url or os.environ.get("MAPSDATA_BASE_URL") or MAPSDATA_BASE_URL).rstrip("/")
        self.timeout = int(timeout or os.environ.get("MAPSDATA_TIMEOUT_SECONDS", "120"))
        self.get_json = get_json or self._get_json
        self.get_text = get_text or _get_text

    def preflight(self) -> None:
        if not self.csv_paths and not self.job_ids:
            raise ProviderPreflightError("MAPSDATA_CSV or MAPSDATA_JOB_ID is required")
        if self.job_ids and not self.api_key.strip():
            raise ProviderPreflightError("MAPSDATA_KEY is required for MAPSDATA_JOB_ID")
        for path in self.csv_paths:
            if not Path(path).is_file():
                raise ProviderPreflightError(f"MapsData CSV not found: {path}")

    def discover(self, start: date, end: date) -> list[ProviderRecord]:
        self.preflight()
        records: dict[str, ProviderRecord] = {}
        for path in self.csv_paths:
            text = Path(path).read_text(encoding="utf-8-sig")
            for record in _records_from_rows(
                "mapsdata",
                _read_csv_rows(text),
                default_date=end.isoformat(),
                raw_context={"csv_path": path},
            ):
                records.setdefault(record.provider_id, record)
        for job_id in self.job_ids:
            job = self.get_json(f"{self.base_url}/jobs/{quote(job_id)}", self.api_key, self.timeout)
            status = str(job.get("status") or "").casefold()
            if status != "completed":
                raise ProviderPreflightError(f"MapsData job is not completed: {job_id} ({status or 'unknown'})")
            link = self.get_json(
                f"{self.base_url}/jobs/{quote(job_id)}/download?format=csv&scope=all",
                self.api_key,
                self.timeout,
            )
            text = self.get_text(str(link.get("url") or ""), self.timeout)
            for record in _records_from_rows(
                "mapsdata",
                _read_csv_rows(text),
                default_date=end.isoformat(),
                raw_context={"job_id": job_id, "job": job, "download": link},
            ):
                records.setdefault(record.provider_id, record)
        return list(records.values())

    def _get_json(self, url: str, api_key: str, timeout: int) -> dict:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
            response.raise_for_status()
            return response.json()


class SalesNavigatorAdapter:
    """Import an operator-exported Sales Navigator CSV without UI automation."""

    name = "sales_navigator"

    def __init__(self, csv_paths: list[str] | None = None):
        self.csv_paths = csv_paths if csv_paths is not None else _env_list(
            "SALES_NAVIGATOR_CSV"
        ) or _env_list("SALES_NAV_CSV")

    def preflight(self) -> None:
        if not self.csv_paths:
            raise ProviderPreflightError("SALES_NAVIGATOR_CSV is required")
        for path in self.csv_paths:
            if not Path(path).is_file():
                raise ProviderPreflightError(f"Sales Navigator CSV not found: {path}")

    def discover(self, start: date, end: date) -> list[ProviderRecord]:
        self.preflight()
        records: dict[str, ProviderRecord] = {}
        for path in self.csv_paths:
            text = Path(path).read_text(encoding="utf-8-sig")
            for record in _records_from_rows(
                self.name,
                _read_csv_rows(text),
                default_date=end.isoformat(),
                raw_context={"csv_path": path},
            ):
                records.setdefault(record.provider_id, record)
        return list(records.values())


class CostarTenantAdapter:
    """Import operator-reviewed Costar tenant exports as discovery records."""

    name = "costar_tenant"

    def __init__(self, csv_paths: list[str] | None = None):
        self.csv_paths = csv_paths if csv_paths is not None else _env_list("COSTAR_TENANT_CSV")

    def preflight(self) -> None:
        if not self.csv_paths:
            raise ProviderPreflightError("COSTAR_TENANT_CSV is required")
        for path in self.csv_paths:
            if not Path(path).is_file():
                raise ProviderPreflightError(f"Costar tenant CSV not found: {path}")

    def discover(self, start: date, end: date) -> list[ProviderRecord]:
        self.preflight()
        records: dict[str, ProviderRecord] = {}
        for path in self.csv_paths:
            text = Path(path).read_text(encoding="utf-8-sig")
            for record in _records_from_rows(
                self.name,
                _read_csv_rows(text),
                default_date=end.isoformat(),
                raw_context={"csv_path": path},
            ):
                records.setdefault(record.provider_id, record)
        return list(records.values())


def _post_json(url: str, payload: dict, timeout: int) -> dict:
    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()
        return response.json()


def _run_apify_actor(token: str, actor_id: str, payload: dict, timeout: int) -> list[dict]:
    actor = quote(actor_id.replace("/", "~"), safe="~")
    run_url = f"https://api.apify.com/v2/acts/{actor}/runs"
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            run_url,
            params={"token": token, "waitForFinish": timeout},
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        run = (response.json().get("data") or {})
        dataset_id = run.get("defaultDatasetId")
        if not dataset_id:
            raise RuntimeError("Apify actor did not return a dataset")
        dataset = client.get(
            f"https://api.apify.com/v2/datasets/{dataset_id}/items",
            params={"token": token, "clean": "true", "format": "json"},
        )
        dataset.raise_for_status()
        value = dataset.json()
        return value if isinstance(value, list) else []


def _env_list(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def _read_csv_rows(text: str) -> list[dict]:
    return [dict(row) for row in csv.DictReader(io.StringIO(text))]


def _records_from_rows(
    provider: str,
    rows: list[dict],
    *,
    default_date: str,
    raw_context: dict,
) -> list[ProviderRecord]:
    records: list[ProviderRecord] = []
    for row in rows:
        normalized = {_normalize_key(key): value for key, value in row.items()}
        url = _row_url(normalized, provider)
        title = _row_title(normalized, provider)
        if not title:
            continue
        row_hash = _row_hash(provider, normalized)
        provider_id = _first(
            normalized,
            "id",
            "lead_id",
            "place_id",
            "google_place_id",
            "cid",
            "property_id",
            "tenant_id",
            "listing_id",
        ) or row_hash
        source_name = _first(normalized, "source", "source_name", "list_name") or provider
        records.append(
            ProviderRecord(
                provider=provider,
                provider_id=str(provider_id),
                url=url,
                title=title,
                published_at=_first(
                    normalized,
                    "date",
                    "published_at",
                    "updated_at",
                    "scraped_at",
                    "lease_date",
                    "move_in_date",
                    "opening_date",
                    "occupancy_date",
                ) or default_date,
                source_name=str(source_name),
                raw={
                    **raw_context,
                    "row": row,
                    "normalized_row": normalized,
                    "provider_record_hash": row_hash,
                },
            )
        )
    return records


def _row_url(row: dict, provider: str) -> str:
    value = _first(
        row,
        "source_url",
        "article_url",
        "property_url",
        "listing_url",
        "website",
        "business_website",
        "company_website",
        "url",
        "google_maps_url",
        "maps_url",
    )
    normalized = _normalize_url(value)
    if normalized:
        return normalized
    return f"https://{provider.replace('_', '-')}.aether.local/lead/{_row_hash(provider, row)}"


def _row_title(row: dict, provider: str) -> str:
    if provider == "costar_tenant":
        pieces = [
            _first(row, "tenant", "tenant_name", "business_name", "company"),
            _first(row, "property", "property_name", "building_name"),
            _first(row, "address", "street_address"),
            _first(row, "city"),
            _first(row, "state"),
            _first(row, "event", "lease_status", "occupancy_status"),
        ]
    elif provider == "sales_navigator":
        pieces = [
            _first(row, "company_name", "company", "organization", "organization_name"),
            _first(row, "title", "job_title", "current_title", "role", "position"),
            _first(row, "industry", "segment", "function", "seniority"),
            _first(row, "city", "location"),
            _first(row, "state", "region"),
            _first(row, "email", "work_email", "contact_email"),
            _first(row, "linkedin_url", "linkedin", "profile_url"),
        ]
    else:
        pieces = [
            _first(row, "business", "business_name", "name", "company"),
            _first(row, "category", "type"),
            _first(row, "address", "street_address"),
            _first(row, "city"),
            _first(row, "state", "region"),
            _first(row, "email", "phone", "website"),
        ]
    return " | ".join(str(piece).strip() for piece in pieces if str(piece or "").strip())[:500]


def _first(row: dict, *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _normalize_key(key: str | None) -> str:
    return "".join(character if character.isalnum() else "_" for character in str(key or "").strip().casefold()).strip("_")


def _normalize_url(value: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        return ""
    parsed = urlsplit(cleaned if "://" in cleaned else f"https://{cleaned}")
    if not parsed.netloc or "." not in parsed.netloc:
        return ""
    return parsed.geturl()


def _row_hash(provider: str, row: dict) -> str:
    payload = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{provider}:{payload}".encode()).hexdigest()[:24]


def _get_text(url: str, timeout: int) -> str:
    if not url:
        raise ProviderPreflightError("download URL is missing")
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text
