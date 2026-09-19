"""GitHub-native daily article discovery, enrichment, CRM sync, and reporting.

This module is deliberately independent from the legacy model-backed Scout
stages.  It fetches the curated source list over public HTTP, qualifies only
evidence present in the fetched article, uses TREG for contact enrichment, and
delivers the internal report through Gmail domain-wide delegation.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

import httpx

from scout.v2.artifacts import ArtifactStore
from scout.v2.contracts import Evidence, LeadEvent, Organization
from scout.v2.discovery import CuratedSiteAdapter, load_curated_sources
from scout.v2.ids import event_id, organization_id, stable_uuid
from scout.v2.state import StateStore
from scout.v2.treg import TregClient, TregDeferred, TregResolver
from scout.v2.verification import ContactVerifier


SOURCE_COUNT = 125
TARGET_LEADS = 30
REPORT_SENDER = "akhil@automationinterns.com"
REPORT_RECIPIENT = "jw@aetherclean.com"
INTERNAL_COPY_RECIPIENTS = ("jon@automationinterns.com", REPORT_SENDER)
CSV_FIELDS = [
    "lead_id",
    "source_url",
    "source_name",
    "publication_date",
    "company",
    "property",
    "event",
    "location",
    "summary",
    "service_angle",
    "score",
    "contact_name",
    "first_name",
    "contact_title",
    "email",
    "phone",
    "linkedin",
    "contact_provider",
    "contact_verification",
    "pipedrive_lead_id",
    "pipedrive_status",
]

ARIZONA_TERMS = (
    "arizona", "phoenix", "tucson", "mesa", "scottsdale", "tempe", "chandler",
    "gilbert", "glendale", "peoria", "surprise", "goodyear", "avondale",
    "flagstaff", "prescott", "yuma", "buckeye", "queen creek", "maricopa",
    "casa grande", "sedona", "kingman", "marana", "sahuarita", "san tan valley",
)
EVENT_PATTERNS = (
    ("acquisition", ("acquired", "acquisition", "purchased", "bought")),
    ("opening", ("opens", "opened", "opening", "grand opening", "ribbon cutting")),
    ("construction", ("breaks ground", "groundbreaking", "construction", "under construction")),
    ("completion", ("completed", "completion", "topped out", "delivered")),
    ("lease", ("lease", "leased", "tenant", "occupancy", "move in", "move-in")),
    ("redevelopment", ("redevelopment", "renovation", "adaptive reuse", "rezoning")),
    ("expansion", ("expansion", "expands", "new facility", "relocates", "relocation")),
    ("management", ("property management", "management selected", "management transition")),
)
PROPERTY_TERMS = (
    "commercial", "facility", "property", "project", "development", "campus", "center",
    "hotel", "industrial", "warehouse", "office", "retail", "restaurant", "medical",
    "multifamily", "apartment", "apartments", "self-storage", "school", "hospital",
)
NEGATIVE_TERMS = (
    "opinion", "editorial", "podcast", "webinar", "job opening", "careers", "obituary",
    "bankruptcy", "lawsuit", "closure", "closed permanently", "stalled project",
)
CITY_TERMS = (
    "Phoenix", "Tucson", "Mesa", "Scottsdale", "Tempe", "Chandler", "Gilbert",
    "Glendale", "Peoria", "Surprise", "Goodyear", "Avondale", "Flagstaff", "Prescott",
    "Yuma", "Buckeye", "Queen Creek", "Maricopa", "Casa Grande", "Sedona", "Kingman",
    "Marana", "Sahuarita", "San Tan Valley", "Arizona",
)
SERVICE_ANGLES = {
    "acquisition": "recurring janitorial and facility transition support",
    "opening": "opening-ready cleaning and recurring janitorial support",
    "construction": "construction cleanup and recurring facility support",
    "completion": "post-construction turnover and recurring facility support",
    "lease": "tenant turnover, porter, and recurring janitorial support",
    "redevelopment": "renovation turnover and recurring facility support",
    "expansion": "new-space cleaning and recurring facility support",
    "management": "property transition and recurring janitorial support",
}


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden and data.strip():
            self.parts.append(data)


@dataclass(slots=True)
class ArticleLead:
    lead_id: str
    source_url: str
    source_name: str
    publication_date: str
    company: str
    property: str
    event: str
    location: str
    summary: str
    service_angle: str
    score: int
    contact_name: str = ""
    first_name: str = ""
    contact_title: str = ""
    email: str = ""
    phone: str = ""
    linkedin: str = ""
    contact_provider: str = ""
    contact_verification: str = ""
    pipedrive_lead_id: str = ""
    pipedrive_status: str = ""
    enrichment_error: str = ""

    def csv_row(self) -> dict[str, str | int]:
        return asdict(self) | {"enrichment_error": self.enrichment_error}


def visible_text(raw_html: str) -> str:
    parser = _VisibleText()
    parser.feed(raw_html)
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def validate_source_list(path: str | Path, expected: int = SOURCE_COUNT) -> list[Any]:
    sources = load_curated_sources(path)
    if len(sources) != expected:
        raise RuntimeError(f"news_websites.csv must contain exactly {expected} data rows; found {len(sources)}")
    return sources


def _first_match(text: str, values: Iterable[str]) -> str:
    lowered = text.casefold()
    for value in values:
        if value.casefold() in lowered:
            return value
    return ""


def _event_kind(text: str) -> tuple[str, str]:
    lowered = text.casefold()
    for kind, phrases in EVENT_PATTERNS:
        phrase = _first_match(lowered, phrases)
        if phrase:
            return kind, phrase
    return "", ""


def _entity_from_title(title: str) -> str:
    cleaned = re.sub(r"\s+", " ", title).strip(" -|:")
    patterns = (
        r"^(.{3,90}?)\s+(?:announces?|opens?|opened|plans?|will|is|has|acquires?|bought|purchased|breaks ground|begins|completes?|expands?|leases?)\b",
        r"^(.{3,90}?)\s*[:\-]\s+(?:new|plans?|construction|opening|expansion|lease)",
    )
    for pattern in patterns:
        match = re.search(pattern, cleaned, re.IGNORECASE)
        if match:
            entity = match.group(1).strip(" -|:")
            if len(entity.split()) >= 2 and not entity.casefold() in {"arizona", "phoenix"}:
                return entity[:120]
    return cleaned[:120]


def _summary(title: str, text: str) -> str:
    body = text.strip()
    if body:
        first = re.split(r"(?<=[.!?])\s+", body, maxsplit=1)[0]
        if len(first) >= 80:
            return first[:360]
    return title[:360]


def qualify_candidate(candidate: Any, article_text: str, since: date, until: date) -> ArticleLead | None:
    if candidate.published_at is None:
        return None
    published = candidate.published_at.date()
    if published < since or published > until:
        return None
    title = re.sub(r"\s+", " ", candidate.title or "").strip()
    text = f"{title} {article_text}".casefold()
    if not any(term in text for term in ARIZONA_TERMS):
        return None
    if any(term in text for term in NEGATIVE_TERMS):
        return None
    if not any(term in text for term in PROPERTY_TERMS):
        return None
    kind, phrase = _event_kind(text)
    if not kind:
        return None
    location = _first_match(text, CITY_TERMS) or ""
    if not location:
        return None
    company = _entity_from_title(title)
    if len(company.split()) < 2:
        return None
    score = 45 + (15 if location.casefold() != "arizona" else 5)
    score += 15 if any(term in title.casefold() for term in PROPERTY_TERMS) else 0
    score += 10 if any(term in title.casefold() for term in dict(EVENT_PATTERNS)[kind]) else 0
    score += 10 if len(article_text) >= 400 else 0
    score = min(score, 95)
    lead_id = stable_uuid("github-article-lead", candidate.canonical_url, company, kind, published.isoformat())
    return ArticleLead(
        lead_id=lead_id,
        source_url=candidate.canonical_url,
        source_name=candidate.source_name,
        publication_date=published.isoformat(),
        company=company,
        property=title,
        event=f"{kind}: {phrase}",
        location=location,
        summary=_summary(title, article_text),
        service_angle=SERVICE_ANGLES[kind],
        score=score,
    )


def dedupe_leads(leads: Iterable[ArticleLead]) -> list[ArticleLead]:
    seen: set[tuple[str, str, str, str]] = set()
    output: list[ArticleLead] = []
    for lead in sorted(leads, key=lambda item: (-item.score, item.source_url, item.lead_id)):
        key = (
            re.sub(r"\W+", " ", lead.company.casefold()).strip(),
            re.sub(r"\W+", " ", lead.event.casefold()).strip(),
            re.sub(r"\W+", " ", lead.location.casefold()).strip(),
            lead.publication_date,
        )
        if key in seen or lead.source_url.casefold() in {item.source_url.casefold() for item in output}:
            continue
        seen.add(key)
        output.append(lead)
    return output


def _split_name(value: str) -> tuple[str, str]:
    parts = value.split()
    return (parts[0], " ".join(parts[1:])) if parts else ("", "")


def enrich_with_treg(leads: list[ArticleLead], *, state_path: Path, cache_path: Path, budget_usd: float) -> list[ArticleLead]:
    state = StateStore(state_path)
    state.migrate()
    client = TregClient(cache_path, budget_usd=budget_usd)
    resolver = TregResolver(client, ContactVerifier(state))
    try:
        for lead in leads:
            org_id = organization_id(lead.company, "", lead.location)
            evidence = [Evidence(url=lead.source_url, supports="Fetched article source for this property event.", provider="github-http")]
            organization = Organization(
                organization_id=org_id,
                canonical_name=lead.company,
                location=lead.location,
                evidence=evidence,
            )
            event = LeadEvent(
                lead_event_id=event_id(org_id, lead.event, lead.location, lead.publication_date),
                run_id="github-article-pipeline",
                organization_id=org_id,
                source_provider="article",
                primary_candidate_id=stable_uuid("github-candidate", lead.source_url),
                supporting_candidate_ids=[stable_uuid("github-candidate", lead.source_url)],
                event=lead.event,
                location=lead.location,
                date_posted=date.fromisoformat(lead.publication_date),
                summary=lead.summary,
                priority="high" if lead.score >= 75 else "medium",
                property_type="commercial_property",
                service_angle=lead.service_angle,
                confidence="high",
                evidence=evidence,
            )
            try:
                result = resolver.resolve(organization, opportunities=[event])
            except TregDeferred as exc:
                lead.enrichment_error = str(exc)
                continue
            if result.get("status") not in {"found", "existing_email"}:
                lead.enrichment_error = "; ".join(result.get("errors") or [result.get("status", "no_match")])
                continue
            person = result.get("person") or {}
            lead.contact_name = str(person.get("name") or "").strip()
            lead.first_name, _ = _split_name(lead.contact_name)
            lead.contact_title = str(person.get("title") or "").strip()
            lead.email = str(result.get("email") or "").strip().casefold()
            lead.linkedin = str(result.get("linkedin") or "").strip()
            lead.contact_provider = str(result.get("provider") or "").strip()
            lead.contact_verification = str(result.get("reason") or "unknown").strip()
    finally:
        client.close()
    return leads


class PipedriveArticleWriter:
    def __init__(self, *, domain: str, token: str, owner_id: int, fields: dict[str, str]):
        if not domain or not token:
            raise RuntimeError("PIPEDRIVE_DOMAIN and PIPEDRIVE_API_TOKEN are required")
        required = {"aether_lead_event_id", "article_url", "date_posted"}
        missing = sorted(required - set(fields))
        if missing:
            raise RuntimeError(f"PIPEDRIVE_DEAL_FIELDS is missing: {', '.join(missing)}")
        self.http = httpx.Client(
            base_url=f"https://{domain}.pipedrive.com/api/",
            params={"api_token": token},
            headers={"Accept": "application/json"},
            timeout=30,
        )
        self.owner_id = owner_id
        self.fields = fields

    def close(self) -> None:
        self.http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.http.request(method, path, **kwargs)
        body = response.json() if response.content else {}
        if not 200 <= response.status_code < 300 or body.get("success") is False:
            raise RuntimeError(f"Pipedrive {method} {path} failed: {response.status_code} {body}")
        return body.get("data")

    @staticmethod
    def _first_id(data: Any) -> int | None:
        items = data.get("items", []) if isinstance(data, dict) else []
        if not items:
            return None
        item = items[0].get("item") or items[0]
        return int(item["id"]) if item.get("id") is not None else None

    def _organization(self, name: str, location: str) -> int:
        found = self._first_id(self._request("GET", "v2/organizations/search", params={"term": name, "fields": "name", "exact_match": "true", "limit": 1}))
        if found is not None:
            return found
        data = self._request("POST", "v2/organizations", json={"name": name, "owner_id": self.owner_id, "address": {"value": location}})
        return int(data["id"])

    def upsert(self, lead: ArticleLead) -> tuple[str, str]:
        org_id = self._organization(lead.company, lead.location)
        values = {
            "aether_lead_event_id": lead.lead_id,
            "article_url": lead.source_url,
            "date_posted": lead.publication_date,
            "event_role": "anchor",
            "outreach_state": "research_only",
        }
        custom_fields = {self.fields[key]: value for key, value in values.items() if self.fields.get(key) and value}
        title = f"Article lead — {lead.company} — {lead.event}"[:255]
        existing = self._first_id(self._request("GET", "v2/leads/search", params={"term": lead.lead_id, "fields": "custom_fields", "exact_match": "true", "limit": 1}))
        payload = {"title": title, "organization_id": org_id, "owner_id": self.owner_id, **custom_fields}
        if existing is None:
            data = self._request("POST", "v1/leads", json=payload)
            lead_id = str(data["id"])
            status = "created"
        else:
            self._request("PATCH", f"v1/leads/{existing}", json=payload)
            lead_id = str(existing)
            status = "updated"
        return lead_id, status


class GmailReportSender:
    def __init__(self, service_account_json: str):
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        info = json.loads(service_account_json)
        credentials = service_account.Credentials.from_service_account_info(
            info,
            scopes=("https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send"),
        ).with_subject(REPORT_SENDER)
        self.service = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    def _sent_ids(self, subject: str) -> list[str]:
        result = self.service.users().messages().list(
            userId=REPORT_SENDER, q=f'in:sent from:{REPORT_SENDER} subject:"{subject}"', maxResults=20
        ).execute()
        ids: list[str] = []
        for item in result.get("messages") or []:
            message = self.service.users().messages().get(
                userId=REPORT_SENDER, id=item["id"], format="metadata", metadataHeaders=["Subject", "From"]
            ).execute()
            headers = {h["name"].casefold(): h["value"] for h in message.get("payload", {}).get("headers", [])}
            if headers.get("subject") == subject and REPORT_SENDER in headers.get("from", "").casefold():
                ids.append(str(item["id"]))
        return ids

    def send_html_once(self, *, recipients: tuple[str, ...], subject: str, html_body: str) -> str:
        existing = self._sent_ids(subject)
        if len(existing) > 1:
            raise RuntimeError(f"Gmail exact-subject collision for {subject!r}")
        if existing:
            return existing[0]
        message = EmailMessage()
        message["To"] = ", ".join(recipients)
        message["From"] = REPORT_SENDER
        message["Subject"] = subject
        message.set_content("Aether article report. Please view the HTML version.")
        message.add_alternative(html_body, subtype="html")
        import base64

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        response = self.service.users().messages().send(userId=REPORT_SENDER, body={"raw": raw}).execute()
        message_id = str(response.get("id") or "")
        if not message_id:
            raise RuntimeError("Gmail send returned no message ID")
        if self._sent_ids(subject) != [message_id]:
            raise RuntimeError(f"Gmail post-send verification failed for {subject!r}")
        return message_id


def render_report(leads: list[ArticleLead], *, source_count: int, source_errors: int, since: date, until: date) -> str:
    rows = []
    for lead in leads:
        contact = "<br>".join(
            value for value in (
                html.escape(lead.contact_name), html.escape(lead.contact_title), html.escape(lead.email), html.escape(lead.phone), html.escape(lead.linkedin)
            ) if value
        ) or "No verified contact found"
        crm = html.escape(
            f"{lead.pipedrive_lead_id} ({lead.pipedrive_status})"
            if lead.pipedrive_lead_id
            else "Not synced"
        )
        rows.append(
            "<tr>"
            f"<td>{lead.score}</td><td><a href=\"{html.escape(lead.source_url, quote=True)}\">{html.escape(lead.company)}</a><br>{html.escape(lead.property)}</td>"
            f"<td>{html.escape(lead.event)}</td><td>{html.escape(lead.location)}</td><td>{html.escape(lead.summary)}</td>"
            f"<td>{html.escape(lead.service_angle)}</td><td>{contact}</td><td>{crm}</td>"
            "</tr>"
        )
    shortfall = max(0, TARGET_LEADS - len(leads))
    return (
        "<html><body style=\"font-family:Arial,sans-serif;color:#173c30\">"
        f"<h1>Aether article leads — {until.isoformat()}</h1>"
        f"<p>Coverage window: {since.isoformat()} through {until.isoformat()}. "
        f"Inspected {source_count} curated websites. Qualified {len(leads)} unique leads. "
        f"Shortfall versus target of {TARGET_LEADS}: {shortfall}. Source errors: {source_errors}.</p>"
        "<p><strong>No prospect outreach was sent.</strong> These are internal article leads for review and Pipedrive tracking.</p>"
        "<table border=\"1\" cellpadding=\"6\" cellspacing=\"0\"><thead><tr>"
        "<th>Score</th><th>Company / property</th><th>Event</th><th>Location</th><th>Source summary</th><th>Service angle</th><th>Verified contact</th><th>Pipedrive lead</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></body></html>"
    )


def _write_csv(path: Path, leads: list[ArticleLead]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for lead in leads:
            writer.writerow({field: getattr(lead, field, "") for field in CSV_FIELDS})


def require_github_configuration() -> dict[str, str]:
    required = (
        "TREG_TOKEN",
        "PIPEDRIVE_API_TOKEN",
        "PIPEDRIVE_DOMAIN",
        "PIPEDRIVE_JORDAN_USER_ID",
        "PIPEDRIVE_DEAL_FIELDS",
        "GMAIL_SERVICE_ACCOUNT_JSON",
    )
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError("missing GitHub pipeline configuration: " + ", ".join(missing))
    try:
        fields = json.loads(os.environ["PIPEDRIVE_DEAL_FIELDS"])
    except json.JSONDecodeError as exc:
        raise RuntimeError("PIPEDRIVE_DEAL_FIELDS must be valid JSON") from exc
    if not isinstance(fields, dict):
        raise RuntimeError("PIPEDRIVE_DEAL_FIELDS must be a JSON object")
    try:
        owner_id = int(os.environ["PIPEDRIVE_JORDAN_USER_ID"])
    except ValueError as exc:
        raise RuntimeError("PIPEDRIVE_JORDAN_USER_ID must be an integer") from exc
    if owner_id <= 0:
        raise RuntimeError("PIPEDRIVE_JORDAN_USER_ID must be positive")
    try:
        gmail = json.loads(os.environ["GMAIL_SERVICE_ACCOUNT_JSON"])
    except json.JSONDecodeError as exc:
        raise RuntimeError("GMAIL_SERVICE_ACCOUNT_JSON must be valid JSON") from exc
    required_gmail_keys = {"client_email", "private_key", "token_uri"}
    if not isinstance(gmail, dict) or not required_gmail_keys <= set(gmail):
        raise RuntimeError(
            "GMAIL_SERVICE_ACCOUNT_JSON must contain client_email, private_key, and token_uri"
        )
    return {str(key): str(value) for key, value in fields.items()}


def run(args: argparse.Namespace) -> dict[str, Any]:
    if os.environ.get("AETHER_GITHUB_PIPELINE_ENABLED", "").casefold() not in {"1", "true", "yes", "on"}:
        raise RuntimeError("AETHER_GITHUB_PIPELINE_ENABLED must be true")
    pipedrive_fields = require_github_configuration()
    source_path = Path(args.sources).resolve()
    sources = validate_source_list(source_path)
    stamp = args.until
    since = date.fromisoformat(args.since)
    until = date.fromisoformat(stamp)
    state_dir = Path(args.state_dir).resolve()
    results_dir = Path(args.results_dir).resolve()
    run_id = f"github-article-{stamp}"
    state = StateStore(state_dir / "scout.sqlite")
    state.migrate()
    artifacts = ArtifactStore(results_dir, stamp, run_id, state)
    state.create_run(
        run_id,
        stamp,
        since.isoformat(),
        {
            "mode": "github-deterministic",
            "sources": str(source_path),
            "source_count": len(sources),
            "treg_budget_usd": float(os.environ.get("TREG_BUDGET_USD", "5")),
        },
        str(artifacts.manifest_path),
    )
    batch = CuratedSiteAdapter(sources, state, artifacts, workers=args.workers).discover(
        run_id, since, max_candidates=0, until=until
    )
    candidates = [candidate for candidate in batch.candidates if candidate.record_status.value == "valid"]
    leads: list[ArticleLead] = []
    for candidate in candidates:
        try:
            raw = Path(candidate.raw_artifact_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if lead := qualify_candidate(candidate, visible_text(raw), since, until):
            leads.append(lead)
    leads = dedupe_leads(leads)
    target = int(os.environ.get("GITHUB_ARTICLE_LEAD_TARGET", TARGET_LEADS))
    if len(leads) > target:
        leads = leads[:target]
    leads = enrich_with_treg(
        leads,
        state_path=state_dir / "verification.sqlite",
        cache_path=state_dir / "treg-cache.sqlite",
        budget_usd=float(os.environ.get("TREG_BUDGET_USD", "5")),
    )
    writer = PipedriveArticleWriter(
        domain=os.environ.get("PIPEDRIVE_DOMAIN", "").strip(),
        token=os.environ.get("PIPEDRIVE_API_TOKEN", "").strip(),
        owner_id=int(os.environ.get("PIPEDRIVE_JORDAN_USER_ID", "11380767")),
        fields=pipedrive_fields,
    )
    pipedrive_ids: dict[str, str] = {}
    try:
        for lead in leads:
            pipedrive_id, pipedrive_status = writer.upsert(lead)
            lead.pipedrive_lead_id = pipedrive_id
            lead.pipedrive_status = pipedrive_status
            pipedrive_ids[lead.lead_id] = pipedrive_id
    finally:
        writer.close()
    output_dir = results_dir / stamp
    _write_csv(output_dir / "article_leads.csv", leads)
    report_html = render_report(
        leads, source_count=len(sources), source_errors=len(batch.source_errors), since=since, until=until
    )
    (output_dir / "article_report.html").write_text(report_html, encoding="utf-8")
    report_subject = f"Aether article leads — {stamp}"
    sender = GmailReportSender(os.environ.get("GMAIL_SERVICE_ACCOUNT_JSON", ""))
    report_message_id = sender.send_html_once(
        recipients=(REPORT_RECIPIENT,), subject=report_subject, html_body=report_html
    )
    copy_subject = f"Fwd: {report_subject}"
    copy_html = f"<p>Forwarded internal copy of the article report sent to {html.escape(REPORT_RECIPIENT)}.</p>{report_html}"
    copy_message_id = sender.send_html_once(
        recipients=INTERNAL_COPY_RECIPIENTS, subject=copy_subject, html_body=copy_html
    )
    result = {
        "run_id": run_id,
        "source_count": len(sources),
        "source_errors": len(batch.source_errors),
        "candidate_count": len(batch.candidates),
        "qualified_count": len(leads),
        "target": target,
        "shortfall": max(0, target - len(leads)),
        "pipedrive_ids": pipedrive_ids,
        "report_message_id": report_message_id,
        "copy_message_id": copy_message_id,
        "report_recipient": REPORT_RECIPIENT,
        "copy_recipients": list(INTERNAL_COPY_RECIPIENTS),
        "no_prospect_outreach": True,
    }
    (output_dir / "run_summary.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", default="news_websites.csv")
    parser.add_argument("--state-dir", default=".github-state")
    parser.add_argument("--results-dir", default="results")
    parser.add_argument("--since", default=(date.today() - timedelta(days=1)).isoformat())
    parser.add_argument("--until", default=date.today().isoformat())
    parser.add_argument("--workers", type=int, default=8)
    return parser


if __name__ == "__main__":
    try:
        print(json.dumps(run(parser().parse_args()), indent=2, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 - GitHub boundary emits a concise failure
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
