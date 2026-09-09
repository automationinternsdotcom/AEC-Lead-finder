"""Implementation for the explicit-only Aether bulk enrichment skill."""
from __future__ import annotations

import csv
import gzip
import hashlib
import html
import json
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel, ConfigDict, Field
from trafilatura import extract as extract_main_text
import outreach_contract as shared_outreach
from recipient_policy import assess_recipient, candidate_rank_key, current_employer_match

from v2.artifacts import ArtifactStore, new_manifest
from v2.apollo import ApolloFatalError, ApolloResolver, ApolloTransientError
from v2.contracts import (
    ContactCandidate,
    DiscoveryCandidate,
    Evidence,
    LeadEvent,
    LeadScore,
    Organization,
    Person,
    RecordStatus,
    ReviewItem,
    StageStatus,
    VerificationStatus,
)
from v2.dedup import FUZZY_PROMPT, dedupe_candidates_exact, validate_fuzzy_groups
from v2.discovery import (
    CuratedSiteAdapter,
    CuratedSource,
    article_score,
    date_from_url,
    load_curated_sources,
    parse_datetime,
    publication_date,
    same_registrable_domain,
)
from v2.http import FetchResponse, HttpFetcher
from v2.ids import (
    candidate_id,
    canonicalize_url,
    event_id,
    normalize_text,
    organization_id,
    stable_hash,
    stable_uuid,
)
from v2.qualification import JudgmentPayload
from v2.research import ContactResearchService, DecisionMakerService
from v2.state import SCHEMA_VERSION, StateStore
from v2.verification import ContactVerifier, is_organization_mismatch, select_best
from v2.outreach import score_recipient_role
from integration.handoff import (
    HANDOFF_PROTOCOL_VERSION,
    HANDOFF_SCHEMA_VERSION,
    handoff_content_hash,
    load_handoff,
)
from integration.models import (
    CompanySync,
    EligibilityStatus,
    EventRole,
    LeadEventSync,
    OutreachSequenceSync,
    RecipientSync,
    SalesHandoff,
)


ModelCall = Callable[[str, str, list[dict]], tuple[str, dict]]
MAX_SITEMAP_DEPTH = 4
MAX_SITEMAP_DOCUMENTS = 100
MAX_URLS_PER_SOURCE = 10_000
CONVENTIONAL_SITEMAPS = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/wp-sitemap.xml",
)
WHY_LINE_PROTOCOL_VERSION = "recipient-outreach-v4"
WHY_LINE_REVISION_STAGE = "why_lines_recipient_outreach_v4"
WHY_LINE_MIGRATION_PROTOCOLS = ("recipient-outreach-v3",)
RECIPIENT_PROTOCOL_VERSION = "recipients-v1"
RECIPIENT_STAGE = "bulk_recipient_enrichment_v1"
SALES_HANDOFF_STAGE = "bulk_sales_handoff_v1"
RECIPIENT_DECISION_STAGE = "decision-makers"
RECIPIENT_CONTACT_STAGE = "contacts"
RECIPIENT_APOLLO_STAGE = "apollo"
MAX_PEOPLE_PER_COMPANY = 3
APOLLO_MIN_REQUEST_INTERVAL_SECONDS = 1.25
MAX_QUALIFICATION_RECOVERY_CALLS = 49
MAX_SCORE_RECOVERY_CALLS = 79
ARTICLE_PRUNE_XPATHS = (
    '//*[contains(concat(" ", normalize-space(@class), " "), " wp-block-embed ")]',
    '//*[contains(concat(" ", normalize-space(@class), " "), " wp-embedded-content ")]',
    '//*[contains(@class, "related-post") or contains(@id, "related-post")]',
    '//*[contains(@class, "jp-relatedposts") or contains(@id, "jp-relatedposts")]',
    '//*[contains(@class, "yarpp-related") or contains(@id, "yarpp-related")]',
)
WHY_QUESTION_FUTURE = " Is there any chance we could stay in touch regarding your future janitorial needs?"
WHY_QUESTION_REVIEW = " Is there any chance you'll be reviewing your janitorial needs?"
WHY_QUESTION_ADDITIONAL_SPACE = " Is there any chance you'll be reviewing your janitorial needs, with the additional space?"
WHY_SHORT_REFERENCE_SLOTS = {
    "company", "project", "project_or_expansion",
}
WHY_TEMPLATES = {
    "acquisition": {
        "text": "Hi [first name] just wanted to reach out since I saw on the news that {company} took ownership of {property} in {location}." + WHY_QUESTION_FUTURE,
        "slots": ("company", "property", "location"),
        "sendable": True,
    },
    "opening": {
        "text": "Hi [first name] just wanted to reach out since I saw on the news that {property} is opening in {location}." + WHY_QUESTION_FUTURE,
        "slots": ("property", "location"),
        "sendable": True,
    },
    "planned_development": {
        "text": "Hi [first name] just wanted to reach out since I saw on the news that plans are moving forward for {project} in {location}." + WHY_QUESTION_FUTURE,
        "slots": ("project", "location"),
        "sendable": True,
    },
    "approval": {
        "text": "Hi [first name] just wanted to reach out since I saw on the news that {project} received {approval}." + WHY_QUESTION_FUTURE,
        "slots": ("project", "approval"),
        "sendable": True,
    },
    "construction_start": {
        "text": "Hi [first name] just wanted to reach out since I saw on the news that construction started on {project} in {location}." + WHY_QUESTION_FUTURE,
        "slots": ("project", "location"),
        "sendable": True,
    },
    "lease_relocation": {
        "text": "Hi [first name] just wanted to reach out since I saw on the news that {company} is preparing to occupy {property} in {location}." + WHY_QUESTION_FUTURE,
        "slots": ("company", "property", "location"),
        "sendable": True,
    },
    "site_acquisition": {
        "text": "Hi [first name] just wanted to reach out since I saw on the news that {company} acquired {site} in {location}." + WHY_QUESTION_FUTURE,
        "slots": ("company", "site", "location"),
        "sendable": True,
    },
    "expansion": {
        "text": "Hi [first name] just wanted to reach out since I saw on the news that {company} is expanding in {location}." + WHY_QUESTION_ADDITIONAL_SPACE,
        "slots": ("company", "location"),
        "sendable": True,
    },
    "funded_facility": {
        "text": "Hi [first name] just wanted to reach out since I saw on the news that {funding} is supporting {project_or_expansion}." + WHY_QUESTION_FUTURE,
        "slots": ("funding", "project_or_expansion"),
        "sendable": True,
    },
    "renovation_conversion": {
        "text": "Hi [first name] just wanted to reach out since I saw on the news that {property} is being renovated into {new_use}." + WHY_QUESTION_REVIEW,
        "slots": ("property", "new_use"),
        "sendable": True,
    },
    "construction_progress": {
        "text": "Hi [first name] just wanted to reach out since I saw on the news that {project} reached {milestone}." + WHY_QUESTION_FUTURE,
        "slots": ("project", "milestone"),
        "sendable": True,
    },
    "completion": {
        "text": "Hi [first name] just wanted to reach out since I saw on the news that {project} was completed in {location}." + WHY_QUESTION_FUTURE,
        "slots": ("project", "location"),
        "sendable": True,
    },
    "route_new_owner": {"text": "", "slots": (), "sendable": False},
    "skip_negative": {"text": "", "slots": (), "sendable": False},
    "skip_general": {"text": "", "slots": (), "sendable": False},
}
WHY_TEMPLATES = shared_outreach.WHY_TEMPLATES


def _template_catalog() -> str:
    rows = []
    for key, template in WHY_TEMPLATES.items():
        if template["sendable"]:
            rows.append(f'- {key}: {template["text"]}')
    rows.extend(
        (
            "- route_new_owner: seller, broker, listing, auction, or unverified ownership transition; do not produce copy.",
            "- skip_negative: closure, bankruptcy, lawsuit, stalled, or abandoned project without a verified reopening or reuse; do not produce copy.",
            "- skip_general: market report, portfolio statistic, vendor article, or other signal without a specific property-level trigger; do not produce copy.",
        )
    )
    return "\n".join(rows)
ARIZONA_TERMS = (
    "arizona", "phoenix", "tucson", "mesa", "scottsdale", "tempe",
    "chandler", "gilbert", "glendale", "peoria", "surprise", "goodyear",
    "avondale", "flagstaff", "prescott", "yuma", "buckeye", "queen creek",
    "maricopa", "pinal county", "maricopa county", "casa grande",
    "lake havasu city", "bullhead city", "apache junction", "oro valley",
    "sierra vista", "prescott valley", "fountain hills", "cottonwood",
    "sedona", "nogales", "kingman", "eloy", "coolidge", "marana",
    "sahuarita", "san tan valley",
)
NON_ARIZONA_STATE_TERMS = (
    "california", "colorado", "florida", "georgia", "illinois", "indiana",
    "maryland", "massachusetts", "michigan", "minnesota", "missouri",
    "nevada", "new jersey", "new mexico", "new york", "north carolina",
    "ohio", "oregon", "pennsylvania", "south carolina", "tennessee",
    "texas", "utah", "virginia", "washington", "wisconsin",
)
AEC_EVENT_TERMS = (
    "open", "lease", "occup", "construct", "develop", "redevelop",
    "acqui", "sold", "sale", "tenant", "facility", "warehouse",
    "industrial", "retail", "multifamily", "hotel", "restaurant", "office",
    "medical", "campus", "data center", "distribution", "manufactur",
    "groundbreak", "expan", "renovat", "property", "real estate",
)


@dataclass(frozen=True, slots=True)
class BulkOptions:
    since: str
    until: str
    output_dir: Path
    sources_csv: Path
    workers: int
    model: str
    run_id: str
    archive_until: str = ""
    resume: bool = False
    seed_db: Path | None = None
    seed_run_id: str = ""
    corpus_db: Path | None = None
    corpus_run_id: str = ""
    search_fallback: bool = True
    reuse_discovery_corpus: bool = False
    batch_size: int = 20


@dataclass(slots=True)
class _BatchRecoveryBudget:
    remaining_calls: int

    def claim(self) -> bool:
        if self.remaining_calls <= 0:
            return False
        self.remaining_calls -= 1
        return True


class _QualificationBatchContractError(ValueError):
    """A response cannot be assigned safely to every candidate in its batch."""


class _ScoreBatchContractError(ValueError):
    """A response cannot be assigned safely to every event in its score batch."""


class SourceCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    source_name: str
    source_url: str
    sitemap_documents: int = 0
    sitemap_urls: int = 0
    pages_fetched: int = 0
    dated_candidates: int = 0
    undated_pages: int = 0
    fallback_used: bool = False
    fallback_candidates: int = 0
    incomplete: bool = False
    errors: list[str] = Field(default_factory=list)


class WhyVariant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = ""
    template_key: str = ""
    lead_event_id: str = ""
    slots: dict[str, str] = Field(default_factory=dict)
    confidence: str = "low"
    source_urls: list[str] = Field(default_factory=list)
    status: str = "review"
    validation_errors: list[str] = Field(default_factory=list)


class CompanyProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_key: str
    company_id: str = ""
    canonical_name: str
    domain: str = ""
    aliases: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    employee_count: str = ""
    organization_ids: list[str] = Field(default_factory=list)
    lead_event_ids: list[str] = Field(default_factory=list)
    anchor_lead_event_id: str
    relationship_type: str = "unknown"
    relationship_confidence: str = "low"
    relationship_sources: list[str] = Field(default_factory=list)
    outreach_route: str = "review"
    variants: dict[str, WhyVariant]
    evidence_urls: list[str] = Field(default_factory=list)
    record_status: str = "review"
    raw_artifact_path: str = ""


ARCHIVE_FALLBACK_PROMPT = """Search the web for articles published from {since} through {until} on this exact publication site that report Arizona commercial-property events relevant to facilities services.

Source ID: {source_id}
Publication: {source_name}
Site: {source_url}

Include specific openings, occupancy, leases, completed construction, redevelopment, management transitions, business expansions, multifamily lease-up, industrial activation, retail/hospitality openings, and property transactions. Exclude macro commentary and residential consumer stories.

Return strict JSON only as {{"source_id":"{source_id}","urls":["https://..."]}}. Return an empty urls list when none are found. Invent no URLs."""


COMPANY_PROMPT = """Select one approved Aether cold-email opening template for one company and return only the sourced insertion values. This single response produces the company's one why line.

Profile key: {profile_key}
Company name: {company_name}
Known official domain: {domain}
Known aliases: {names}
Known locations: {locations}
Deterministic anchor event ID: {anchor_id}
Events: {events}

Research protocol:
- Use at most two web searches and one research path. Start with the supplied event sources and use the known official domain only when needed to verify the company, property, or ownership role. Do not research people or contacts.
- Choose the strongest specific property event and distinguish its operator/owner from its developer, general contractor, broker/leasing representative, tenant, and project name. Never silently relabel one as another. Relationship uncertainty is a review signal, not a reason to drop the opportunity. The selected lead_event_id must be one of the supplied event IDs.
- Ground every insertion in an official company page, government page, credible business listing, or supplied event article. Keep each insertion short, natural, and free of URLs. Use lowercase for every non-company slot.
- The company slot must be an exact, recognizable one-to-three-word name or abbreviation derived from the supplied company name or aliases. Use its natural brand capitalization when known (for example, TSMC, JLL, or Raytheon); deterministic code resolves casing against the supplied names and rejects unknown forms.
- The project and project_or_expansion slots may contain no more than three whitespace-separated words. Prefer a familiar shortened name that would be clear as a standalone reference in casual conversation, such as formation park 10 or nexus commerce center. Never invent an obscure abbreviation merely to satisfy the limit.
- The location slot must contain exactly one smallest useful locality or neighborhood, in no more than three words. Never include a comma, state, county, metro/region label, parent city, multiple cities, or street detail. Return tempe rather than tempe, arizona; deer valley rather than deer valley, north phoenix; and tucson rather than tucson and gilbert. Deterministic code normalizes the value and rejects broad or unusable locations.
- For acquisition, verify the named company is the buyer or new owner. Use route_new_owner for a seller, broker, listing, auction, or unclear ownership role.
- Use funded_facility only when the funding, contract, grant, or financing is explicitly tied to a physical project or facility expansion.
- Use skip_negative for a closure, bankruptcy, lawsuit, stalled project, or abandoned project unless the evidence establishes a specific reopening, conversion, or reuse.
- Use skip_general when there is no specific property-level trigger. Never force a sendable template onto weak evidence.
- Do not rewrite the approved wording and do not return a final sentence. Return only the template key and insertion slots; deterministic code renders the line.

Approved templates and routing outcomes:
{template_catalog}

Return strict JSON only as {{"canonical_name":"","domain":"","employee_count":"","relationship_type":"operator|owner|property_manager|developer|contractor|broker|tenant|project|unknown","relationship_confidence":"high|medium|low","relationship_sources":[],"outreach_route":"direct_facility|construction_closeout|referral|review","selection":{{"template_key":"","lead_event_id":"","slots":{{}},"confidence":"high|medium|low","source_urls":[]}}}}. Use exactly one listed template_key. For a routing outcome, return an empty slots object but still cite the evidence supporting the routing decision."""


BULK_QUALIFICATION_PROMPT = """Qualify this bounded batch using only the supplied saved article evidence. Do not search the web and do not identify people. For every exact candidate_id, decide whether the article reports a specific Arizona commercial-property event that creates a facilities-services opportunity.

Requested article publication window: {since} through {until}, inclusive.

Return strict JSON only as one object mapping every exact candidate_id to an object with keys: qualified, business_name, event, date_posted, location, summary, state, priority, property_type, service_angle, filter_reason, confidence. date_posted must be the article's exact YYYY-MM-DD publication date or an empty string, never a timestamp. Include every submitted ID exactly once and invent no IDs. A rejection requires a specific filter_reason. Reject articles outside the requested publication window. A qualification requires state Arizona, priority high or medium, and nonempty business_name, event, and location. The named business must appear in that candidate's supplied title or saved article evidence; never carry a business or event from another candidate in the batch.

Candidates:
{candidates}"""


BULK_SCORE_PROMPT = """Score each Arizona commercial-property lead event from 0 to 100 for Aether Facility Services outreach priority. Consider event fit, commercial property fit, timing, geography, and facilities-service need. Do not consider contact availability.

Return strict JSON only as one object mapping every exact lead_event_id to one integer 0-100. Include every submitted ID exactly once and invent no IDs.

Events:
{events}"""


class BulkRunner:
    STAGES = (
        "discover", "screen", "qualify", "qualification-audit", "seed", "dedup", "score",
        "companies", "export",
    )

    def __init__(
        self,
        options: BulkOptions,
        *,
        fetch: Callable[[str], FetchResponse] | None = None,
        model_call: ModelCall | None = None,
    ):
        self.options = options
        self.since = date.fromisoformat(options.since)
        self.until = date.fromisoformat(options.until)
        self.archive_until = date.fromisoformat(options.archive_until or options.until)
        if self.until < self.since:
            raise ValueError("--until must be on or after --since")
        if not self.since <= self.archive_until <= self.until:
            raise ValueError("--archive-until must be within --since and --until")
        if options.model != "grok-4.3":
            raise ValueError("bulk enrichment is pinned to grok-4.3")
        options.output_dir.mkdir(parents=True, exist_ok=True)
        self.state = StateStore(options.output_dir / "state.sqlite")
        self.state.migrate()
        self.fetch = fetch or HttpFetcher()
        self.model_call = model_call or _default_model_call
        self.sources = load_curated_sources(options.sources_csv)
        self.sources_sha256 = hashlib.sha256(options.sources_csv.read_bytes()).hexdigest()
        self.artifacts = ArtifactStore(
            options.output_dir, options.until, options.run_id, self.state
        )
        configuration = {
            "kind": "explicit_bulk_enrichment",
            "workflow_version": 3,
            "schema_version": SCHEMA_VERSION,
            "since": options.since,
            "until": options.until,
            "archive_until": self.archive_until.isoformat(),
            "workers": options.workers,
            "model": options.model,
            "search_fallback": options.search_fallback,
            "apollo": False,
            "email_delivery": False,
            "seed_db": str(options.seed_db or ""),
            "seed_run_id": options.seed_run_id,
            "corpus_db": str(options.corpus_db or ""),
            "corpus_run_id": options.corpus_run_id,
            "sources_sha256": self.sources_sha256,
            "reuse_discovery_corpus": options.reuse_discovery_corpus,
            "batch_size": options.batch_size,
        }
        self._validate_resume(configuration)
        self.state.create_run(
            options.run_id,
            options.until,
            options.since,
            configuration,
            str(self.artifacts.manifest_path),
        )
        if options.resume and self.artifacts.manifest_path.exists():
            self.manifest = self.artifacts.load_manifest()
            self.manifest.configuration = configuration
        else:
            self.manifest = new_manifest(
                options.run_id, options.until, options.since, configuration
            )
            self.artifacts.write_manifest(self.manifest)
        self.coverage: list[SourceCoverage] = []
        self.profiles: list[CompanyProfile] = []
        # Source discovery is parallel too, so keep total article traffic bounded
        # while still allowing a large final sitemap to use more than one worker.
        self._archive_fetch_slots = threading.BoundedSemaphore(
            max(1, options.workers * 2)
        )
        self._persisted_archive_by_source: dict[str, list[DiscoveryCandidate]] = {}
        self._persisted_archive_by_url: dict[str, DiscoveryCandidate] = {}

    def _validate_resume(self, configuration: dict) -> None:
        with self.state.connect() as conn:
            row = conn.execute(
                "SELECT stamp, since_date, configuration_json FROM v2_runs WHERE run_id=?",
                (self.options.run_id,),
            ).fetchone()
        if not row:
            if self.options.resume:
                raise ValueError("--resume run ID does not exist in this output database")
            return
        if not self.options.resume:
            raise ValueError("run ID already exists; use --resume")
        if row["stamp"] != self.options.until or row["since_date"] != self.options.since:
            raise ValueError("resume date range does not match the existing run")
        prior = json.loads(row["configuration_json"] or "{}")
        immutable = (
            "kind", "model", "archive_until", "seed_db", "seed_run_id",
            "corpus_db", "corpus_run_id",
            "apollo", "email_delivery", "search_fallback", "workflow_version",
            "reuse_discovery_corpus", "batch_size",
        )
        mismatched = [
            key for key in immutable
            if key in prior and prior.get(key) != configuration.get(key)
        ]
        if prior.get("sources_sha256") and prior["sources_sha256"] != self.sources_sha256:
            mismatched.append("sources_sha256")
        if mismatched:
            raise ValueError(
                "resume configuration mismatch: " + ", ".join(sorted(set(mismatched)))
            )

    def run(self) -> dict:
        self.manifest.status = StageStatus.RUNNING
        self.state.set_run_status(self.options.run_id, StageStatus.RUNNING)
        self.artifacts.write_manifest(self.manifest)
        try:
            self._stage("discover", self._discover)
            self._stage("screen", self._screen)
            self._stage("qualify", self._qualify)
            self._stage("qualification-audit", self._qualification_audit)
            self._stage("seed", self._seed)
            self._stage("dedup", self._dedup)
            self._stage("score", self._score)
            self._stage("companies", self._companies)
            self._stage("export", self._export)
        except Exception as exc:
            self.manifest.status = StageStatus.FAILED
            self.manifest.errors.append(
                {"type": type(exc).__name__, "message": str(exc)}
            )
            self._refresh_manifest()
            raise
        self.manifest.status = StageStatus.COMPLETED
        self._refresh_manifest()
        return {
            "run_id": self.options.run_id,
            "manifest": str(self.artifacts.manifest_path),
            "leads": len(self.state.active_events_for_run(self.options.run_id)),
            "companies": len(self._load_profiles()),
            "output": str(self.artifacts.final_dir),
        }

    def refresh_why_lines(self, *, limit: int | None = None) -> dict:
        """Create a versioned why-line-only revision with one model call per company."""
        if not self.options.resume:
            raise ValueError("why-line refresh requires --resume and an existing run ID")
        if self.manifest.status != StageStatus.COMPLETED:
            raise ValueError("why-line refresh requires a completed source run")
        if limit is not None and limit < 1:
            raise ValueError("why-line refresh limit must be positive")
        stage = WHY_LINE_REVISION_STAGE
        revision_dir = self.artifacts.final_dir / WHY_LINE_PROTOCOL_VERSION
        summary_path = revision_dir / "summary.json"
        already_complete = stage in self.state.completed_stages(self.options.run_id)

        protocol_hash = hashlib.sha256(
            f"{WHY_LINE_PROTOCOL_VERSION}\n{COMPANY_PROMPT}".encode()
        ).hexdigest()
        protocol_name = f"{WHY_LINE_PROTOCOL_VERSION}/protocol.json"
        protocol_path = self.artifacts.raw_dir / protocol_name
        protocol = {
            "protocol_version": WHY_LINE_PROTOCOL_VERSION,
            "prompt_sha256": protocol_hash,
            "model": self.options.model,
            "run_id": self.options.run_id,
            "one_model_call_per_business": True,
            "model_repair_calls": False,
        }
        if protocol_path.exists():
            if json.loads(protocol_path.read_text(encoding="utf-8")) != protocol:
                raise ValueError("why-line revision protocol changed during resume")
        else:
            self.artifacts.write_raw(stage, protocol_name, protocol)

        if not already_complete:
            self.state.set_stage_status(self.options.run_id, stage, StageStatus.RUNNING)
            self.manifest.stages[stage] = {"status": StageStatus.RUNNING.value}
            self._refresh_manifest()
        try:
            counters, complete = self._refresh_company_why_lines(
                stage, revision_dir, limit=limit
            )
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            self.state.set_stage_status(
                self.options.run_id, stage, StageStatus.FAILED, error=error
            )
            self.manifest.stages[stage] = {
                "status": StageStatus.FAILED.value,
                "error": error,
            }
            self._refresh_manifest()
            raise
        if not complete:
            self.manifest.stages[stage] = {
                "status": StageStatus.RUNNING.value,
                "counters": counters,
            }
            self._refresh_manifest()
            return {
                "run_id": self.options.run_id,
                "protocol_version": WHY_LINE_PROTOCOL_VERSION,
                "model": self.options.model,
                "status": "running",
                "counts": counters,
                "raw_output": str(
                    self.artifacts.raw_dir / WHY_LINE_PROTOCOL_VERSION
                ),
            }
        self.state.set_stage_status(
            self.options.run_id, stage, StageStatus.COMPLETED, counters=counters
        )
        self.manifest.stages[stage] = {
            "status": StageStatus.COMPLETED.value,
            "counters": counters,
        }
        self.manifest.counts.update(
            {f"{stage}.{key}": int(value) for key, value in counters.items()}
        )
        self._refresh_manifest()
        return json.loads(summary_path.read_text(encoding="utf-8"))

    def enrich_recipients(self, *, apollo_go: bool = False, apollo_cap: int = 444, treg_go: bool = False, treg_budget_usd: float = 5.0) -> dict:
        """Add GPS-style person/contact rows to the completed company revision."""
        if not self.options.resume:
            raise ValueError("recipient enrichment requires --resume and an existing run ID")
        if self.manifest.status != StageStatus.COMPLETED:
            raise ValueError("recipient enrichment requires a completed source run")
        if apollo_cap < 0:
            raise ValueError("Apollo cap cannot be negative")

        source_dir = self.artifacts.final_dir / WHY_LINE_PROTOCOL_VERSION
        profiles_path = source_dir / "company_profiles.jsonl"
        if not profiles_path.exists():
            raise ValueError(
                f"recipient enrichment requires the completed {WHY_LINE_PROTOCOL_VERSION} revision"
            )
        profiles = [
            CompanyProfile.model_validate_json(line)
            for line in profiles_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        valid_profiles = [
            profile
            for profile in profiles
            if _profile_why_line(profile).status == "valid"
        ]
        combined_profiles = [
            profile
            for profile in valid_profiles
            if not _single_sendable_company_name(profile.canonical_name)
        ]
        for profile in combined_profiles:
            self.state.add_review(
                ReviewItem(
                    review_id=stable_uuid(
                        "review",
                        self.options.run_id,
                        RECIPIENT_STAGE,
                        profile.company_id,
                        "combined-company",
                    ),
                    run_id=self.options.run_id,
                    stage=RECIPIENT_STAGE,
                    record_type="company_profile",
                    record_id=profile.company_id,
                    reason_code="combined_company_not_sendable",
                    validation_errors=["combined_company_not_sendable"],
                )
            )
        profiles = sorted(
            (
                profile
                for profile in valid_profiles
                if _single_sendable_company_name(profile.canonical_name)
            ),
            key=lambda profile: profile.company_id,
        )
        if not profiles:
            raise ValueError("why-line revision has no sendable companies")

        output_dir = source_dir / RECIPIENT_PROTOCOL_VERSION
        summary_path = output_dir / "summary.json"
        protocol = {
            "protocol_version": RECIPIENT_PROTOCOL_VERSION,
            "model": self.options.model,
            "run_id": self.options.run_id,
            "source_protocol": WHY_LINE_PROTOCOL_VERSION,
            "sendable_companies_only": True,
            "single_company_required": True,
            "max_people_per_company": MAX_PEOPLE_PER_COMPANY,
            "decision_maker_attempts": 1,
            "public_contact_attempts": 1,
            "apollo_authorized": bool(apollo_go),
            "apollo_new_request_cap": apollo_cap,
            "apollo_trigger": "no_nonrejected_public_email_or_phone",
            "apollo_reveal_phone": False,
            "email_delivery": False,
        }
        if treg_go:
            protocol.update(treg_authorized=True, treg_budget_usd=treg_budget_usd)
        protocol_path = self.artifacts.raw_dir / RECIPIENT_PROTOCOL_VERSION / "protocol.json"
        if protocol_path.exists():
            if json.loads(protocol_path.read_text(encoding="utf-8")) != protocol:
                raise ValueError("recipient enrichment protocol changed during resume")
        else:
            self.artifacts.write_raw(
                RECIPIENT_STAGE, f"{RECIPIENT_PROTOCOL_VERSION}/protocol.json", protocol
            )
        if (
            RECIPIENT_STAGE in self.state.completed_stages(self.options.run_id)
            and summary_path.exists()
        ):
            return json.loads(summary_path.read_text(encoding="utf-8"))

        self.state.set_stage_status(
            self.options.run_id, RECIPIENT_STAGE, StageStatus.RUNNING
        )
        self.manifest.stages[RECIPIENT_STAGE] = {
            "status": StageStatus.RUNNING.value,
        }
        self._refresh_manifest()
        try:
            events_by_id = {
                event.lead_event_id: event
                for event in self.state.active_events_for_run(self.options.run_id)
            }
            organizations = self._recipient_organizations(profiles)
            organizations_by_id = {
                organization.organization_id: organization
                for organization in organizations
            }
            anchors: dict[str, LeadEvent] = {}
            for profile in profiles:
                event = events_by_id.get(profile.anchor_lead_event_id)
                if not event:
                    raise ValueError(
                        f"recipient company {profile.company_id} has no anchor event"
                    )
                anchors[profile.company_id] = event.model_copy(
                    update={"organization_id": profile.company_id}
                )

            decision_attempted = self._provider_target_ids(RECIPIENT_DECISION_STAGE)
            decision_pending = [
                organization
                for organization in organizations
                if organization.organization_id not in decision_attempted
            ]
            if decision_pending:
                with ThreadPoolExecutor(max_workers=self.options.workers) as executor:
                    futures = {
                        executor.submit(
                            self._research_recipient_company,
                            organization,
                            anchors[organization.organization_id],
                        ): organization
                        for organization in decision_pending
                    }
                    for future in as_completed(futures):
                        future.result()

            organization_ids = set(organizations_by_id)
            people = sorted(
                (
                    person
                    for person in self.state.people()
                    if person.organization_id in organization_ids
                ),
                key=lambda person: (person.organization_id, person.person_id),
            )
            # The decision-maker contract itself caps each response at three. This
            # second deterministic cap protects exports if state was populated manually.
            people = _cap_people_by_company(people, MAX_PEOPLE_PER_COMPANY)

            contact_attempted = self._provider_target_ids(RECIPIENT_CONTACT_STAGE)
            existing_contact_people = {
                contact.person_id
                for contact in self.state.contacts_for_run(self.options.run_id)
                if contact.selected
                and contact.email
                and contact.verification_status != VerificationStatus.REJECTED
                and contact.organization_id in anchors
                and contact.lead_event_id
                == anchors[contact.organization_id].lead_event_id
            }
            contact_pending = [
                person
                for person in people
                if person.person_id not in contact_attempted
                and person.person_id not in existing_contact_people
            ]
            if contact_pending:
                with ThreadPoolExecutor(max_workers=self.options.workers) as executor:
                    futures = {
                        executor.submit(
                            self._research_recipient_contact,
                            person,
                            organizations_by_id[person.organization_id],
                            anchors[person.organization_id],
                        ): person
                        for person in contact_pending
                    }
                    for future in as_completed(futures):
                        future.result()

            if treg_go:
                from v2.treg import TregClient, recover_state
                client = TregClient(self.options.output_dir / "treg-cache.sqlite", budget_usd=treg_budget_usd)
                try:
                    people, _ = recover_state(self.state, self.artifacts, organizations, people,
                        list(anchors.values()), self.state.contacts_for_run(self.options.run_id), client=client)
                finally:
                    client.close()
            apollo_counts = self._recipient_apollo(
                people=people,
                organizations=organizations_by_id,
                anchors=anchors,
                authorized=apollo_go,
                new_request_cap=apollo_cap,
            )
            summary = self._write_recipient_exports(
                output_dir=output_dir,
                profiles=profiles,
                people=people,
                organizations=organizations_by_id,
                anchors=anchors,
                protocol=protocol,
                apollo_counts=apollo_counts,
            )
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            self.state.set_stage_status(
                self.options.run_id, RECIPIENT_STAGE, StageStatus.FAILED, error=error
            )
            self.manifest.stages[RECIPIENT_STAGE] = {
                "status": StageStatus.FAILED.value,
                "error": error,
            }
            self._refresh_manifest()
            raise

        counters = summary["counts"]
        if apollo_counts.get("key_missing"):
            self.state.set_stage_status(
                self.options.run_id,
                RECIPIENT_STAGE,
                StageStatus.RUNNING,
                counters={key: int(value) for key, value in counters.items()},
            )
            self.manifest.stages[RECIPIENT_STAGE] = {
                "status": StageStatus.RUNNING.value,
                "counters": counters,
                "waiting_for": "APOLLO_API_KEY",
            }
            self._refresh_manifest()
            return summary
        self.state.set_stage_status(
            self.options.run_id,
            RECIPIENT_STAGE,
            StageStatus.COMPLETED,
            counters={key: int(value) for key, value in counters.items()},
        )
        self.manifest.stages[RECIPIENT_STAGE] = {
            "status": StageStatus.COMPLETED.value,
            "counters": counters,
        }
        self.manifest.counts.update(
            {f"{RECIPIENT_STAGE}.{key}": int(value) for key, value in counters.items()}
        )
        self._refresh_manifest()
        return summary

    def build_sales_handoff(
        self, *, existing_sales_db: Path | None = None
    ) -> dict:
        """Build the immutable local provider boundary without calling providers."""
        if not self.options.resume:
            raise ValueError("sales handoff requires --resume and an existing run ID")
        if self.manifest.status != StageStatus.COMPLETED:
            raise ValueError("sales handoff requires a completed source run")
        if RECIPIENT_STAGE not in self.state.completed_stages(self.options.run_id):
            raise ValueError("sales handoff requires completed recipient enrichment")

        source_dir = self.artifacts.final_dir / WHY_LINE_PROTOCOL_VERSION
        recipient_dir = source_dir / RECIPIENT_PROTOCOL_VERSION
        profiles_path = source_dir / "company_profiles.jsonl"
        people_path = recipient_dir / "people.jsonl"
        contacts_path = recipient_dir / "contacts.jsonl"
        required = (profiles_path, people_path, contacts_path)
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise ValueError("sales handoff inputs are missing: " + ", ".join(missing))

        profiles = [
            CompanyProfile.model_validate_json(line)
            for line in profiles_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        profiles = sorted(
            (
                profile
                for profile in profiles
                if _profile_why_line(profile).status == "valid"
                and _single_sendable_company_name(profile.canonical_name)
            ),
            key=lambda profile: profile.company_id,
        )
        people = [
            Person.model_validate_json(line)
            for line in people_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        contacts = [
            ContactCandidate.model_validate_json(line)
            for line in contacts_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        events = self.state.active_events_for_run(self.options.run_id)
        candidates = {
            item.candidate_id: item
            for item in self.state.candidates_for_run(self.options.run_id)
        }
        scores = {
            item.lead_event_id: item.score
            for item in self.state.scores_for_run(self.options.run_id)
        }
        open_review_ids = {
            item.record_id
            for item in self.state.reviews_for_run(self.options.run_id, state="open")
        }
        handoff = _bulk_sales_handoff(
            run_id=self.options.run_id,
            profiles=profiles,
            events=events,
            candidates=candidates,
            scores=scores,
            people=people,
            contacts=contacts,
            open_review_ids=open_review_ids,
        )
        duplicate_blocks = 0
        identity_alignments = 0
        existing_sequence_skips = 0
        if existing_sales_db is not None:
            if not existing_sales_db.is_file():
                raise ValueError(
                    f"existing sales database does not exist: {existing_sales_db}"
                )
            (
                handoff,
                identity_alignments,
                existing_sequence_skips,
            ) = _align_handoff_to_existing_companies(handoff, existing_sales_db)
            handoff, duplicate_blocks = _block_cross_run_duplicate_events(
                handoff, existing_sales_db
            )
        handoff, duplicate_primary_email_blocks = _block_duplicate_primary_emails(
            handoff
        )
        relative_name = (
            f"{WHY_LINE_PROTOCOL_VERSION}/{RECIPIENT_PROTOCOL_VERSION}/sales_handoff.json"
        )
        artifact = self.artifacts.write_json(
            SALES_HANDOFF_STAGE,
            relative_name,
            handoff.model_dump(mode="json"),
        )
        validated = load_handoff(artifact["path"])
        counters = {
            "companies": len(validated.companies),
            "lead_events": len(validated.lead_events),
            "crm_eligible_events": sum(item.crm_eligible for item in validated.lead_events),
            "recipients": len(validated.recipients),
            "sequences": len(validated.sequences),
            "ready_sequences": sum(
                item.eligibility_status == EligibilityStatus.READY
                for item in validated.sequences
            ),
            "cross_run_duplicate_blocks": duplicate_blocks,
            "duplicate_primary_email_blocks": duplicate_primary_email_blocks,
            "existing_company_id_alignments": identity_alignments,
            "existing_sequence_skips": existing_sequence_skips,
        }
        self.state.set_stage_status(
            self.options.run_id,
            SALES_HANDOFF_STAGE,
            StageStatus.COMPLETED,
            counters=counters,
        )
        self.manifest.stages[SALES_HANDOFF_STAGE] = {
            "status": StageStatus.COMPLETED.value,
            "counters": counters,
        }
        self.manifest.counts.update(
            {f"{SALES_HANDOFF_STAGE}.{key}": int(value) for key, value in counters.items()}
        )
        self._refresh_manifest()
        return {
            "run_id": self.options.run_id,
            "status": "completed",
            "content_hash": validated.content_hash,
            "counts": counters,
            "sales_handoff": artifact["path"],
            "provider_calls": 0,
            "email_delivery": False,
            "campaign_enrollment": False,
            "existing_sales_db": str(existing_sales_db or ""),
        }

    def _recipient_organizations(
        self, profiles: list[CompanyProfile]
    ) -> list[Organization]:
        organizations: list[Organization] = []
        for profile in profiles:
            evidence = []
            for url in dict.fromkeys(
                [*profile.evidence_urls, *_profile_why_line(profile).source_urls]
            ):
                try:
                    evidence.append(
                        Evidence(
                            url=url,
                            supports=f"Supports the company profile for {profile.canonical_name}.",
                            provider="web",
                        )
                    )
                except Exception:
                    continue
            why_line = _profile_why_line(profile)
            location = why_line.slots.get("location") or next(
                iter(profile.locations), "Arizona"
            )
            organization = Organization(
                organization_id=profile.company_id,
                canonical_name=profile.canonical_name,
                domain=profile.domain,
                location=location,
                aliases=profile.aliases,
                evidence=evidence,
            )
            self.state.save_organization(organization)
            organizations.append(organization)
        return organizations

    def _research_recipient_company(
        self, organization: Organization, anchor: LeadEvent
    ) -> None:
        service = DecisionMakerService(
            self.state,
            self.artifacts,
            self.options.model,
            call_model=self.model_call,
            verifier=ContactVerifier(self.state),
            events=[anchor],
        )
        service.research([organization], attempts=1)

    def _research_recipient_contact(
        self,
        person: Person,
        organization: Organization,
        anchor: LeadEvent,
    ) -> None:
        service = ContactResearchService(
            self.state,
            self.artifacts,
            self.options.model,
            ContactVerifier(self.state),
            call_model=self.model_call,
        )
        service.research([person], [organization], [anchor], attempts=1)

    def _provider_target_ids(self, stage: str) -> set[str]:
        with self.state.connect() as conn:
            return {
                str(row["target_id"])
                for row in conn.execute(
                    "SELECT target_id FROM v2_provider_attempts WHERE run_id=? AND stage=?",
                    (self.options.run_id, stage),
                )
            }

    def _recipient_apollo(
        self,
        *,
        people: list[Person],
        organizations: dict[str, Organization],
        anchors: dict[str, LeadEvent],
        authorized: bool,
        new_request_cap: int,
    ) -> dict[str, int]:
        contacts = self.state.contacts_for_run(self.options.run_id)
        person_ids = {person.person_id for person in people}
        contacts = [contact for contact in contacts if contact.person_id in person_ids]
        publicly_reachable = {
            contact.person_id
            for contact in contacts
            if contact.selected
            and contact.provider == "model"
            and contact.verification_status != VerificationStatus.REJECTED
            and (contact.email or contact.phone)
        }
        missing = [person for person in people if person.person_id not in publicly_reachable]
        with self.state.connect() as conn:
            prior_rows = list(
                conn.execute(
                    "SELECT target_id, status, error_json FROM v2_provider_attempts "
                    "WHERE run_id=? AND stage=?",
                    (self.options.run_id, RECIPIENT_APOLLO_STAGE),
                )
            )
        prior_targets = {
            str(row["target_id"])
            for row in prior_rows
            if not (
                row["status"] == "fatal"
                and "HTTP 429" in str(row["error_json"] or "")
            )
        }
        with self.state.connect() as conn:
            rows = list(
                conn.execute(
                    "SELECT token_usage_json, billable FROM v2_provider_attempts "
                    "WHERE run_id=? AND stage=? AND provider='apollo'",
                    (self.options.run_id, RECIPIENT_APOLLO_STAGE),
                )
            )
        new_requests = sum(
            int(json.loads(row["token_usage_json"] or "{}").get("api_requests", 0))
            for row in rows
        )
        billable_flags = sum(int(row["billable"]) for row in rows)
        created: list[ContactCandidate] = []
        transient_reviews = 0
        capped = 0
        if not authorized:
            return {
                "eligible": len(missing),
                "new_requests": new_requests,
                "billable_flags": billable_flags,
                "created": 0,
                "transient_reviews": 0,
                "capped": 0,
                "key_missing": 0,
            }

        import config as scout_config

        api_key = scout_config._get("APOLLO_API_KEY")
        if missing and not api_key:
            return {
                "eligible": len(missing),
                "new_requests": new_requests,
                "billable_flags": billable_flags,
                "created": 0,
                "transient_reviews": 0,
                "capped": 0,
                "key_missing": 1,
            }
        resolver = ApolloResolver(
            self.state,
            api_key=api_key,
        )
        verifier = ContactVerifier(self.state)
        next_apollo_request_at = 0.0
        for person in missing:
            if person.person_id in prior_targets:
                continue
            organization = organizations[person.organization_id]
            cache_key = stable_hash(
                "apollo", normalize_text(person.name), normalize_text(organization.canonical_name)
            )
            cached_row = self.state.get_apollo_cache(cache_key)
            if (
                cached_row is not None
                and cached_row["status"] == "fatal"
                and "HTTP 429" in str(cached_row["error_json"] or "")
            ):
                with self.state.transaction() as conn:
                    conn.execute(
                        "DELETE FROM v2_apollo_cache WHERE cache_key=?",
                        (cache_key,),
                    )
                cached_row = None
            cached_before = cached_row is not None
            if not cached_before and new_requests >= new_request_cap:
                capped += 1
                continue
            attempt_id = stable_uuid(
                "attempt", self.options.run_id, RECIPIENT_APOLLO_STAGE, person.person_id
            )
            request = self.artifacts.write_raw(
                RECIPIENT_APOLLO_STAGE,
                f"{RECIPIENT_PROTOCOL_VERSION}/apollo/{attempt_id}-request.json",
                {
                    "person_id": person.person_id,
                    "person": person.name,
                    "organization": organization.canonical_name,
                    "reveal_personal_emails": True,
                    "reveal_phone_number": False,
                },
            )
            started = datetime.now(timezone.utc).isoformat()
            if not cached_before:
                new_requests += 1
            try:
                if not cached_before:
                    wait_seconds = next_apollo_request_at - time.monotonic()
                    if wait_seconds > 0:
                        time.sleep(wait_seconds)
                    next_apollo_request_at = (
                        time.monotonic() + APOLLO_MIN_REQUEST_INTERVAL_SECONDS
                    )
                found = resolver.resolve(
                    person.name,
                    organization.canonical_name,
                    spend=True,
                    reveal_phone=False,
                )
                if found.status == "fatal":
                    raise ApolloFatalError(found.error or "cached fatal Apollo result")
            except ApolloTransientError as exc:
                review = ReviewItem(
                    review_id=stable_uuid(
                        "review", self.options.run_id, RECIPIENT_APOLLO_STAGE, person.person_id
                    ),
                    run_id=self.options.run_id,
                    stage=RECIPIENT_APOLLO_STAGE,
                    record_type="person",
                    record_id=person.person_id,
                    reason_code="apollo_transient_failure",
                    validation_errors=[str(exc)],
                )
                self.state.add_review(review)
                self.state.record_provider_attempt(
                    attempt_id=attempt_id,
                    run_id=self.options.run_id,
                    stage=RECIPIENT_APOLLO_STAGE,
                    provider="apollo",
                    target_type="person",
                    target_id=person.person_id,
                    status="review",
                    token_usage={"api_requests": int(not cached_before)},
                    request_artifact_path=request["path"],
                    error={"type": type(exc).__name__, "message": str(exc)},
                    started_at=started,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                transient_reviews += 1
                continue
            except ApolloFatalError as exc:
                self.state.record_provider_attempt(
                    attempt_id=attempt_id,
                    run_id=self.options.run_id,
                    stage=RECIPIENT_APOLLO_STAGE,
                    provider="apollo",
                    target_type="person",
                    target_id=person.person_id,
                    status="fatal",
                    token_usage={"api_requests": int(not cached_before)},
                    request_artifact_path=request["path"],
                    error={"type": type(exc).__name__, "message": str(exc)},
                    started_at=started,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                raise
            response = self.artifacts.write_raw(
                RECIPIENT_APOLLO_STAGE,
                f"{RECIPIENT_PROTOCOL_VERSION}/apollo/{attempt_id}-response.json",
                asdict(found),
            )
            billable = bool(found.billable and not found.cached)
            billable_flags += int(billable)
            self.state.record_provider_attempt(
                attempt_id=attempt_id,
                run_id=self.options.run_id,
                stage=RECIPIENT_APOLLO_STAGE,
                provider="apollo",
                target_type="person",
                target_id=person.person_id,
                status=found.status,
                billable=billable,
                token_usage={"api_requests": int(not found.cached)},
                request_artifact_path=request["path"],
                response_artifact_path=response["path"],
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            if found.status != "found" or not any(
                (found.email, found.phone, found.linkedin)
            ):
                continue
            verification = verifier.verify(
                email=found.email,
                phone=found.phone,
                linkedin=found.linkedin,
                organization_domain=organization.domain,
            )
            created.append(
                ContactCandidate(
                    contact_candidate_id=stable_uuid(
                        "contact-candidate",
                        anchors[person.organization_id].lead_event_id,
                        person.person_id,
                        "apollo",
                    ),
                    run_id=self.options.run_id,
                    lead_event_id=anchors[person.organization_id].lead_event_id,
                    organization_id=person.organization_id,
                    person_id=person.person_id,
                    person_name=person.name,
                    title=person.title,
                    email=verification.email,
                    phone=verification.phone,
                    linkedin=verification.linkedin,
                    provider="apollo",
                    verification_status=verification.status,
                    verification_reason=verification.reason,
                    evidence=[
                        Evidence(
                            url="https://app.apollo.io/",
                            supports=(
                                f"Apollo match for {person.name} at "
                                f"{organization.canonical_name}."
                            ),
                            provider="apollo",
                        )
                    ],
                )
            )

        contacts = select_best([*contacts, *created])
        for contact in contacts:
            self.state.save_contact(contact)
        return {
            "eligible": len(missing),
            "new_requests": new_requests,
            "billable_flags": billable_flags,
            "created": len(created),
            "transient_reviews": transient_reviews,
            "capped": capped,
            "key_missing": 0,
        }

    def _write_recipient_exports(
        self,
        *,
        output_dir: Path,
        profiles: list[CompanyProfile],
        people: list[Person],
        organizations: dict[str, Organization],
        anchors: dict[str, LeadEvent],
        protocol: dict,
        apollo_counts: dict[str, int],
    ) -> dict:
        profile_by_id = {profile.company_id: profile for profile in profiles}
        people_by_company: dict[str, list[Person]] = defaultdict(list)
        for person in people:
            people_by_company[person.organization_id].append(person)
        contacts = [
            contact
            for contact in self.state.contacts_for_run(self.options.run_id)
            if contact.person_id in {person.person_id for person in people}
            and contact.selected
            and contact.verification_status != VerificationStatus.REJECTED
        ]
        contacts_by_person = {contact.person_id: contact for contact in contacts}
        recipient_rows = []
        for person in people:
            profile = profile_by_id[person.organization_id]
            why_line = _profile_why_line(profile)
            contact = contacts_by_person.get(person.person_id)
            first_name = 'team' if person.scope.startswith('company_mailbox:') else _first_name(person.name)
            personalized = _personalize_why_line(why_line.text, first_name)
            recipient_rows.append(
                {
                    "company_id": profile.company_id,
                    "business_name": profile.canonical_name,
                    "first_name": first_name,
                    "full_name": person.name,
                    "title": person.title,
                    "scope": person.scope,
                    "email": contact.email if contact else "",
                    "phone": contact.phone if contact else "",
                    "linkedin": contact.linkedin if contact else "",
                    "contact_provider": contact.provider if contact else "",
                    "verification_status": (
                        contact.verification_status.value if contact else "missing"
                    ),
                    "verification_reason": contact.verification_reason if contact else "",
                    "recipient_status": _recipient_status(contact),
                    "anchor_lead_event_id": anchors[profile.company_id].lead_event_id,
                    "lead_event_ids": ",".join(profile.lead_event_ids),
                    "why_line": personalized,
                    "why_template_key": why_line.template_key,
                    "why_sources": "; ".join(why_line.source_urls),
                    "person_sources": "; ".join(
                        evidence.url for evidence in person.evidence
                    ),
                    "contact_sources": "; ".join(
                        evidence.url for evidence in (contact.evidence if contact else [])
                    ),
                    "run_id": self.options.run_id,
                }
            )
        recipient_rows.sort(
            key=lambda row: (row["business_name"].casefold(), row["full_name"].casefold())
        )
        company_rows = []
        for profile in profiles:
            company_people = people_by_company.get(profile.company_id, [])
            statuses = [
                _recipient_status(contacts_by_person.get(person.person_id))
                for person in company_people
            ]
            company_rows.append(
                {
                    "company_id": profile.company_id,
                    "business_name": profile.canonical_name,
                    "recipient_count": len(company_people),
                    "email_count": statuses.count("email"),
                    "phone_only_count": statuses.count("phone_only"),
                    "linkedin_only_count": statuses.count("linkedin_only"),
                    "no_contact_count": statuses.count("no_contact"),
                    "recipient_status": (
                        "email"
                        if "email" in statuses
                        else "phone_only"
                        if "phone_only" in statuses
                        else "linkedin_only"
                        if "linkedin_only" in statuses
                        else "no_contact"
                        if company_people
                        else "no_person"
                    ),
                    "why_line": _profile_why_line(profile).text,
                    "run_id": self.options.run_id,
                }
            )
        company_rows.sort(key=lambda row: row["business_name"].casefold())

        recipient_fields = [
            "company_id", "business_name", "first_name", "full_name", "title",
            "scope", "email", "phone", "linkedin", "contact_provider",
            "verification_status", "verification_reason", "recipient_status",
            "anchor_lead_event_id", "lead_event_ids", "why_line",
            "why_template_key", "why_sources", "person_sources", "contact_sources",
            "run_id",
        ]
        company_fields = [
            "company_id", "business_name", "recipient_count", "email_count",
            "phone_only_count", "linkedin_only_count", "no_contact_count",
            "recipient_status", "why_line", "run_id",
        ]
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_csv(output_dir / "recipients.csv", recipient_fields, recipient_rows)
        _write_csv(output_dir / "companies.csv", company_fields, company_rows)
        _write_jsonl(output_dir / "people.jsonl", people)
        _write_jsonl(output_dir / "contacts.jsonl", contacts)
        recipient_reviews = [
            review
            for review in self.state.reviews_for_run(self.options.run_id)
            if review.stage
            in {
                RECIPIENT_DECISION_STAGE,
                RECIPIENT_CONTACT_STAGE,
                RECIPIENT_APOLLO_STAGE,
            }
        ]
        _write_jsonl(output_dir / "reviews.jsonl", recipient_reviews)

        usage: dict[str, int] = {"provider_attempts": 0}
        with self.state.connect() as conn:
            rows = list(
                conn.execute(
                    "SELECT token_usage_json, billable FROM v2_provider_attempts "
                    "WHERE run_id=? AND stage IN (?, ?, ?)",
                    (
                        self.options.run_id,
                        RECIPIENT_DECISION_STAGE,
                        RECIPIENT_CONTACT_STAGE,
                        RECIPIENT_APOLLO_STAGE,
                    ),
                )
            )
        usage["provider_attempts"] = len(rows)
        usage["billable_attempts"] = sum(int(row["billable"]) for row in rows)
        for row in rows:
            for key, value in json.loads(row["token_usage_json"] or "{}").items():
                if isinstance(value, (int, float)):
                    usage[key] = usage.get(key, 0) + int(value)
        counts = {
            "eligible_companies": len(profiles),
            "companies_with_people": sum(bool(people_by_company.get(profile.company_id)) for profile in profiles),
            "companies_without_people": sum(not people_by_company.get(profile.company_id) for profile in profiles),
            "people": len(people),
            "recipients_with_email": sum(row["recipient_status"] == "email" for row in recipient_rows),
            "recipients_phone_only": sum(row["recipient_status"] == "phone_only" for row in recipient_rows),
            "recipients_linkedin_only": sum(row["recipient_status"] == "linkedin_only" for row in recipient_rows),
            "recipients_without_contact": sum(row["recipient_status"] == "no_contact" for row in recipient_rows),
            "reviews": len(recipient_reviews),
            "apollo_eligible": apollo_counts["eligible"],
            "apollo_new_requests": apollo_counts["new_requests"],
            "apollo_billable_flags": apollo_counts["billable_flags"],
            "apollo_contacts_created": apollo_counts["created"],
            "apollo_capped": apollo_counts["capped"],
            "apollo_key_missing": apollo_counts["key_missing"],
        }
        summary = {
            "run_id": self.options.run_id,
            "protocol_version": RECIPIENT_PROTOCOL_VERSION,
            "status": (
                "awaiting_apollo_key"
                if apollo_counts["key_missing"]
                else "completed"
            ),
            "model": self.options.model,
            "counts": counts,
            "usage": usage,
            "apollo": {
                "authorized": protocol["apollo_authorized"],
                "new_request_cap": protocol["apollo_new_request_cap"],
                "phone_reveal": False,
                "provider_credit_note": (
                    "Billable flags are local upper-bound accounting; exact provider "
                    "credits and dollar cost require the Apollo account usage ledger."
                ),
            },
            "email_delivery": False,
            "output": str(output_dir),
            "source_output_preserved": str(output_dir.parent),
        }
        self.artifacts.write_json(
            RECIPIENT_STAGE,
            f"{WHY_LINE_PROTOCOL_VERSION}/{RECIPIENT_PROTOCOL_VERSION}/summary.json",
            summary,
        )
        for path, kind in (
            (output_dir / "recipients.csv", "csv"),
            (output_dir / "companies.csv", "csv"),
            (output_dir / "people.jsonl", "jsonl"),
            (output_dir / "contacts.jsonl", "jsonl"),
            (output_dir / "reviews.jsonl", "jsonl"),
        ):
            self.artifacts.record_existing(RECIPIENT_STAGE, kind, path)
        return summary

    def _refresh_company_why_lines(
        self,
        stage: str,
        revision_dir: Path,
        *,
        limit: int | None = None,
    ) -> tuple[dict, bool]:
        original_profiles = self._load_profiles()
        if not original_profiles:
            raise ValueError("completed run has no company profiles to refresh")
        events_by_id = {
            item.lead_event_id: item
            for item in self.state.active_events_for_run(self.options.run_id)
        }
        scores = {
            item.lead_event_id: item.score
            for item in self.state.scores_for_run(self.options.run_id)
        }

        cache_dir = self.artifacts.raw_dir / WHY_LINE_PROTOCOL_VERSION
        cached_by_key: dict[str, CompanyProfile] = {}
        pending: list[CompanyProfile] = []
        current_cached_count = 0
        migrated_count = 0
        for profile in original_profiles:
            cache_path = cache_dir / f"{profile.profile_key}-profile.json"
            if cache_path.exists():
                current_cached_count += 1
                cached = CompanyProfile.model_validate_json(
                    cache_path.read_text(encoding="utf-8")
                )
                response_path = Path(cached.raw_artifact_path)
                if response_path.is_file():
                    why_line = _why_line_from_payload(
                        _parse_object(response_path.read_text(encoding="utf-8")),
                        allowed_event_ids=set(profile.lead_event_ids),
                        known_company_names=[profile.canonical_name, *profile.aliases],
                    )
                    cached = cached.model_copy(
                        update={
                            "variants": {"primary": why_line},
                            "record_status": _profile_record_status(why_line),
                        }
                    )
                    self.artifacts.write_raw(
                        stage,
                        f"{WHY_LINE_PROTOCOL_VERSION}/{profile.profile_key}-profile.json",
                        cached.model_dump(mode="json"),
                    )
                cached_by_key[profile.profile_key] = cached
            else:
                migrated = None
                for prior_protocol in WHY_LINE_MIGRATION_PROTOCOLS:
                    prior_path = (
                        self.artifacts.raw_dir
                        / prior_protocol
                        / f"{profile.profile_key}-profile.json"
                    )
                    if not prior_path.exists():
                        continue
                    prior = CompanyProfile.model_validate_json(
                        prior_path.read_text(encoding="utf-8")
                    )
                    response_path = Path(prior.raw_artifact_path)
                    if not response_path.is_file():
                        continue
                    why_line = _why_line_from_payload(
                        _parse_object(response_path.read_text(encoding="utf-8")),
                        allowed_event_ids=set(profile.lead_event_ids),
                        known_company_names=[profile.canonical_name, *profile.aliases],
                    )
                    migrated = prior.model_copy(
                        update={
                            "variants": {"primary": why_line},
                            "record_status": _profile_record_status(why_line),
                        }
                    )
                    self.artifacts.write_raw(
                        stage,
                        f"{WHY_LINE_PROTOCOL_VERSION}/{profile.profile_key}-profile.json",
                        migrated.model_dump(mode="json"),
                    )
                    break
                if migrated is None:
                    base_path = (
                        self.artifacts.raw_dir
                        / "company-profiles"
                        / f"{profile.profile_key}.json"
                    )
                    if base_path.exists():
                        base = CompanyProfile.model_validate_json(
                            base_path.read_text(encoding="utf-8")
                        )
                        response_path = Path(base.raw_artifact_path)
                        if response_path.is_file():
                            why_line = _why_line_from_payload(
                                _parse_object(
                                    response_path.read_text(encoding="utf-8")
                                ),
                                allowed_event_ids=set(profile.lead_event_ids),
                                known_company_names=[
                                    profile.canonical_name,
                                    *profile.aliases,
                                ],
                            )
                            migrated = profile.model_copy(
                                update={
                                    "variants": {"primary": why_line},
                                    "record_status": _profile_record_status(why_line),
                                    "raw_artifact_path": base.raw_artifact_path,
                                }
                            )
                        else:
                            migrated = profile.model_copy(
                                update={
                                    "variants": base.variants,
                                    "record_status": "review",
                                    "raw_artifact_path": base.raw_artifact_path,
                                }
                            )
                        self.artifacts.write_raw(
                            stage,
                            (
                                f"{WHY_LINE_PROTOCOL_VERSION}/"
                                f"{profile.profile_key}-profile.json"
                            ),
                            migrated.model_dump(mode="json"),
                        )
                if migrated is not None:
                    migrated_count += 1
                    cached_by_key[profile.profile_key] = migrated
                else:
                    pending.append(profile)
        selected = pending[:limit] if limit is not None else pending

        def refresh(profile: CompanyProfile) -> tuple[CompanyProfile, bool]:
            events = [
                events_by_id[event_id]
                for event_id in profile.lead_event_ids
                if event_id in events_by_id
            ]
            if not events:
                raise ValueError(f"company profile has no active events: {profile.profile_key}")
            return self._refresh_company_profile(stage, profile, events, scores)

        with ThreadPoolExecutor(max_workers=self.options.workers) as pool:
            results = list(pool.map(refresh, selected))
        refreshed_by_key = {profile.profile_key: profile for profile, _ in results}
        model_calls = sum(called for _, called in results)

        profiles_by_key = {**cached_by_key, **refreshed_by_key}
        profiles = [
            profiles_by_key[profile.profile_key]
            for profile in original_profiles
            if profile.profile_key in profiles_by_key
        ]
        for profile in profiles:
            why_line = _profile_why_line(profile)
            if why_line.status != "review":
                continue
            self.state.add_review(
                ReviewItem(
                    review_id=stable_uuid(
                        "review", self.options.run_id, stage, profile.profile_key
                    ),
                    run_id=self.options.run_id,
                    stage=stage,
                    record_type="company_profile",
                    record_id=profile.profile_key,
                    reason_code="recipient_why_line_invalid",
                    validation_errors=(
                        why_line.validation_errors or ["recipient_why_line_invalid"]
                    ),
                    raw_artifact_path=profile.raw_artifact_path,
                )
            )
        invalid_record_ids = {
            profile.profile_key
            for profile in profiles
            if _profile_why_line(profile).status == "review"
        }
        for item in self.state.reviews_for_run(self.options.run_id):
            if (
                item.stage == stage
                and item.record_id not in invalid_record_ids
                and item.state != "resolved"
            ):
                self.state.add_review(
                    item.model_copy(
                        update={
                            "state": "resolved",
                            "updated_at": datetime.now(timezone.utc),
                        }
                    )
                )
        remaining = len(original_profiles) - len(profiles)
        valid_profiles = sum(profile.record_status == "valid" for profile in profiles)
        valid_lines = sum(
            _profile_why_line(profile).status == "valid" for profile in profiles
        )
        skipped_lines = sum(
            _profile_why_line(profile).status == "skip" for profile in profiles
        )
        counters = {
            "companies": len(original_profiles),
            "processed_companies": len(profiles),
            "new_model_calls": model_calls,
            "cached_companies": current_cached_count,
            "migrated_companies": migrated_count,
            "remaining_companies": remaining,
            "valid_profiles": valid_profiles,
            "review_profiles": len(profiles) - valid_profiles,
            "valid_lines": valid_lines,
            "skipped_lines": skipped_lines,
            "review_lines": len(profiles) - valid_lines - skipped_lines,
        }
        if remaining:
            self.artifacts.write_raw(
                stage,
                f"{WHY_LINE_PROTOCOL_VERSION}/progress.json",
                counters,
            )
            return counters, False
        with self.state.connect() as conn:
            counters["model_calls"] = int(
                conn.execute(
                    "SELECT COUNT(*) FROM v2_provider_attempts WHERE run_id=? AND stage=?",
                    (self.options.run_id, stage),
                ).fetchone()[0]
            )
        self._write_why_line_revision(stage, revision_dir, profiles, counters)
        return counters, True

    def _refresh_company_profile(
        self,
        stage: str,
        profile: CompanyProfile,
        events: list[LeadEvent],
        scores: dict[str, int],
    ) -> tuple[CompanyProfile, bool]:
        cache_name = (
            f"{WHY_LINE_PROTOCOL_VERSION}/{profile.profile_key}-profile.json"
        )
        cache_path = self.artifacts.raw_dir / cache_name
        if cache_path.exists():
            return (
                CompanyProfile.model_validate_json(
                    cache_path.read_text(encoding="utf-8")
                ),
                False,
            )

        anchor = next(
            (
                event
                for event in events
                if event.lead_event_id == profile.anchor_lead_event_id
            ),
            _anchor_event(events, scores),
        )
        event_payload = [
            {
                "lead_event_id": event.lead_event_id,
                "event": event.event,
                "date": str(event.date_posted or ""),
                "location": event.location,
                "summary": event.summary,
                "priority": event.priority,
                "score": scores.get(event.lead_event_id),
                "sources": [item.url for item in event.evidence],
            }
            for event in sorted(events, key=lambda item: item.lead_event_id)
        ]
        prompt = COMPANY_PROMPT.format(
            template_catalog=_template_catalog(),
            profile_key=profile.profile_key,
            company_name=profile.canonical_name,
            domain=profile.domain,
            names=json.dumps(profile.aliases),
            locations=json.dumps(profile.locations),
            anchor_id=anchor.lead_event_id,
            events=json.dumps(event_payload, sort_keys=True),
        )
        attempt_id = stable_uuid(
            "attempt",
            self.options.run_id,
            stage,
            WHY_LINE_PROTOCOL_VERSION,
            profile.profile_key,
        )
        request_name = f"{WHY_LINE_PROTOCOL_VERSION}/{attempt_id}-request.json"
        response_name = f"{WHY_LINE_PROTOCOL_VERSION}/{attempt_id}-response.txt"
        usage_name = f"{WHY_LINE_PROTOCOL_VERSION}/{attempt_id}-usage.json"
        request = self.artifacts.write_raw(
            stage,
            request_name,
            {"model": self.options.model, "prompt": prompt},
        )
        response_path = self.artifacts.raw_dir / response_name
        usage_path = self.artifacts.raw_dir / usage_name
        started = datetime.now(timezone.utc).isoformat()
        called = False
        try:
            if response_path.exists():
                text = response_path.read_text(encoding="utf-8")
                usage = (
                    json.loads(usage_path.read_text(encoding="utf-8"))
                    if usage_path.exists()
                    else {}
                )
            else:
                called = True
                text, usage = self.model_call(
                    self.options.model, prompt, [{"type": "web_search"}]
                )
                self.artifacts.write_raw_text(stage, response_name, text)
                self.artifacts.write_raw(stage, usage_name, usage)
            payload = _parse_object(text)
            why_line = _why_line_from_payload(
                payload,
                allowed_event_ids={event.lead_event_id for event in events},
                known_company_names=[profile.canonical_name, *profile.aliases],
            )
            refreshed = profile.model_copy(
                update={
                    "variants": {"primary": why_line},
                    "record_status": _profile_record_status(why_line),
                    "raw_artifact_path": str(response_path),
                }
            )
            self.state.record_provider_attempt(
                attempt_id=attempt_id,
                run_id=self.options.run_id,
                stage=stage,
                provider="model",
                target_type="company_why_lines",
                target_id=profile.profile_key,
                status="completed",
                token_usage=usage,
                request_artifact_path=request["path"],
                response_artifact_path=str(response_path),
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as exc:
            refreshed = profile.model_copy(
                update={
                    "variants": {"primary": WhyVariant(
                        validation_errors=[
                            f"recipient_contract:{type(exc).__name__}"
                        ]
                    )},
                    "record_status": "review",
                    "raw_artifact_path": str(response_path) if response_path.exists() else "",
                }
            )
            self.state.record_provider_attempt(
                attempt_id=attempt_id,
                run_id=self.options.run_id,
                stage=stage,
                provider="model",
                target_type="company_why_lines",
                target_id=profile.profile_key,
                status="review",
                token_usage=usage if "usage" in locals() else {},
                request_artifact_path=request["path"],
                response_artifact_path=str(response_path) if response_path.exists() else "",
                error={"type": type(exc).__name__, "message": str(exc)},
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        self.artifacts.write_raw(
            stage,
            cache_name,
            refreshed.model_dump(mode="json"),
        )
        return refreshed, called

    def _stage(self, name: str, function: Callable[[], dict]) -> None:
        if self.options.resume and name in self.state.completed_stages(self.options.run_id):
            self._hydrate(name)
            return
        self.state.set_stage_status(self.options.run_id, name, StageStatus.RUNNING)
        self.manifest.stages[name] = {"status": StageStatus.RUNNING.value}
        self.artifacts.write_manifest(self.manifest)
        try:
            counters = function()
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            self.state.set_stage_status(
                self.options.run_id, name, StageStatus.FAILED, error=error
            )
            self.manifest.stages[name] = {
                "status": StageStatus.FAILED.value,
                "error": error,
            }
            self._refresh_manifest()
            raise
        self.state.set_stage_status(
            self.options.run_id, name, StageStatus.COMPLETED, counters=counters
        )
        self.manifest.stages[name] = {
            "status": StageStatus.COMPLETED.value,
            "counters": counters,
        }
        self.manifest.counts.update(
            {f"{name}.{key}": int(value) for key, value in counters.items()}
        )
        self._refresh_manifest()

    def _discover(self) -> dict:
        for source in self.sources:
            self.state.upsert_source(
                source.source_id,
                source.name,
                source.url,
                source.domain,
                source.state,
                source.enabled,
            )
        if self.options.corpus_db:
            return self._discover_from_external_corpus()
        if self.options.reuse_discovery_corpus:
            persisted = [
                item
                for item in self.state.candidates_for_run(self.options.run_id)
                if item.record_status == RecordStatus.VALID
            ]
            if not persisted:
                raise ValueError("saved-corpus reuse requested but this run has no valid candidates")
            counts: dict[str, int] = defaultdict(int)
            for candidate in persisted:
                counts[candidate.source_id] += 1
            self.coverage = [
                SourceCoverage(
                    source_id=source.source_id,
                    source_name=source.name,
                    source_url=source.url,
                    dated_candidates=counts.get(source.source_id, 0),
                    incomplete=True,
                    errors=["reconstructed_from_interrupted_discovery_corpus"],
                )
                for source in self.sources
            ]
            self._write_coverage()
            return {
                "sources": len(self.sources),
                "corpus_reused": len(persisted),
                "distinct_urls": len({item.canonical_url for item in persisted}),
                "incomplete_sources": len(self.sources),
            }
        current = CuratedSiteAdapter(
            self.sources,
            self.state,
            self.artifacts,
            fetch=self.fetch,
            workers=self.options.workers,
        ).discover(
            self.options.run_id,
            self.since,
            max_candidates=0,
            until=self.archive_until,
        )
        persisted_archive = [
            item
            for item in self.state.candidates_for_run(self.options.run_id)
            if item.provider in {"archive", "archive-search"}
            and item.record_status == RecordStatus.VALID
        ]
        persisted_by_source: dict[str, list[DiscoveryCandidate]] = defaultdict(list)
        for candidate in persisted_archive:
            persisted_by_source[candidate.source_id].append(candidate)
        self._persisted_archive_by_source = dict(persisted_by_source)
        self._persisted_archive_by_url = {
            item.canonical_url: item for item in persisted_archive
        }
        with ThreadPoolExecutor(max_workers=self.options.workers) as pool:
            archive_results = list(pool.map(self._discover_source_archive, self.sources))
        archive_candidates = [item for _, rows in archive_results for item in rows]
        self.coverage = [item for item, _ in archive_results]
        direct_urls_by_source: dict[str, set[str]] = defaultdict(set)
        for candidate in current.candidates:
            if candidate.published_at and candidate.record_status == RecordStatus.VALID:
                direct_urls_by_source[candidate.source_id].add(candidate.canonical_url)
        archive_urls_by_source: dict[str, set[str]] = defaultdict(set)
        for candidate in archive_candidates:
            if candidate.published_at:
                archive_urls_by_source[candidate.source_id].add(candidate.canonical_url)
        direct_errors_by_source: dict[str, list[str]] = defaultdict(list)
        for error in current.source_errors:
            source_id = str(error.get("source_id") or "")
            code = str(error.get("error") or "")
            if source_id and code.startswith("direct_listing_"):
                direct_errors_by_source[source_id].append(code)
        for coverage in self.coverage:
            coverage.dated_candidates = len(
                direct_urls_by_source[coverage.source_id]
                | archive_urls_by_source[coverage.source_id]
            )
            if direct_errors_by_source[coverage.source_id]:
                coverage.incomplete = True
                coverage.errors.extend(
                    code
                    for code in direct_errors_by_source[coverage.source_id]
                    if code not in coverage.errors
                )
        fallback_candidates: list[DiscoveryCandidate] = []
        if self.options.search_fallback:
            targets = [item for item in self.coverage if item.dated_candidates == 0 or item.incomplete]
            with ThreadPoolExecutor(max_workers=self.options.workers) as pool:
                fallback_results = list(pool.map(self._fallback_source, targets))
            coverage_by_id = {item.source_id: item for item in self.coverage}
            for coverage, rows in fallback_results:
                coverage_by_id[coverage.source_id] = coverage
                fallback_candidates.extend(rows)
            self.coverage = [coverage_by_id[source.source_id] for source in self.sources]
        all_candidates = [*current.candidates, *archive_candidates, *fallback_candidates]
        selected = dedupe_candidates_exact(all_candidates)
        for candidate in selected:
            self.state.save_candidate(
                candidate.model_copy(
                    update={
                        "metadata": {
                            **candidate.metadata,
                            "selected_for_qualification": True,
                        }
                    }
                )
            )
        for coverage in self.coverage:
            if not coverage.incomplete:
                continue
            self.state.add_review(
                ReviewItem(
                    review_id=stable_uuid(
                        "review", self.options.run_id, "archive-coverage", coverage.source_id
                    ),
                    run_id=self.options.run_id,
                    stage="discover",
                    record_type="source",
                    record_id=coverage.source_id,
                    reason_code="archive_coverage_incomplete",
                    validation_errors=coverage.errors or ["archive_coverage_incomplete"],
                )
            )
        self._write_coverage()
        return {
            "sources": len(self.sources),
            "current_candidates": len(current.candidates),
            "archive_candidates": len(archive_candidates),
            "fallback_candidates": len(fallback_candidates),
            "selected": len(selected),
            "incomplete_sources": sum(item.incomplete for item in self.coverage),
            "uncovered_sources": sum(item.dated_candidates == 0 for item in self.coverage),
        }

    def _discover_from_external_corpus(self) -> dict:
        """Clone a bounded, hash-verified date slice from saved archive evidence."""
        if not self.options.corpus_db or not self.options.corpus_run_id:
            raise ValueError("external corpus reuse requires a database and run ID")
        corpus = StateStore(self.options.corpus_db)
        with corpus.connect() as conn:
            source_run = conn.execute(
                "SELECT manifest_path FROM v2_runs WHERE run_id=?",
                (self.options.corpus_run_id,),
            ).fetchone()
            discover = conn.execute(
                """SELECT status FROM v2_stage_runs
                   WHERE run_id=? AND stage='discover'""",
                (self.options.corpus_run_id,),
            ).fetchone()
        if not source_run or not discover or discover["status"] != "completed":
            raise ValueError("corpus source run must have completed discovery")
        current_source_ids = {item.source_id for item in self.sources}
        selected: list[DiscoveryCandidate] = []
        omitted_undated = omitted_outside = 0
        for candidate in corpus.candidates_for_run(self.options.corpus_run_id):
            if candidate.source_id not in current_source_ids:
                continue
            if candidate.published_at is None:
                omitted_undated += 1
                continue
            published = candidate.published_at.date()
            if published < self.since or published > self.archive_until:
                omitted_outside += 1
                continue
            artifact = Path(candidate.raw_artifact_path)
            if not candidate.raw_artifact_path or not artifact.is_file():
                raise ValueError(
                    f"corpus candidate artifact is missing: {candidate.candidate_id}"
                )
            payload = artifact.read_bytes()
            actual = hashlib.sha256(payload).hexdigest()
            hash_revalidated = False
            if candidate.raw_artifact_hash:
                if actual != candidate.raw_artifact_hash:
                    if not _corpus_artifact_identity_matches(candidate, payload):
                        raise ValueError(
                            "corpus candidate artifact hash and page identity mismatch: "
                            f"{candidate.candidate_id}"
                        )
                    hash_revalidated = True
            imported_artifact = self.artifacts.write_raw_text(
                "corpus-import",
                f"article-{stable_hash(candidate.canonical_url)[:20]}.html",
                payload.decode("utf-8", errors="replace"),
            )
            metadata = {
                key: value
                for key, value in candidate.metadata.items()
                if key
                not in {
                    "bulk_qualified",
                    "bulk_rejection_reason",
                    "exact_duplicate_candidate_ids",
                    "selected_for_qualification",
                }
            }
            cloned = candidate.model_copy(
                update={
                    "run_id": self.options.run_id,
                    "record_status": RecordStatus.VALID,
                    "validation_errors": [],
                    "metadata": {
                        **metadata,
                        "external_corpus_run_id": self.options.corpus_run_id,
                        "corpus_hash_revalidated": hash_revalidated,
                    },
                    "raw_artifact_path": imported_artifact["path"],
                    "raw_artifact_hash": imported_artifact["sha256"],
                }
            )
            self.state.save_candidate(cloned)
            selected.append(cloned)
        if not selected:
            raise ValueError("external corpus contains no dated candidates in the requested window")

        original_coverage: dict[str, SourceCoverage] = {}
        coverage_path = Path(source_run["manifest_path"]).parent / "final" / "coverage.json"
        if coverage_path.is_file():
            original_coverage = {
                item.source_id: item
                for item in (
                    SourceCoverage.model_validate(value)
                    for value in json.loads(coverage_path.read_text(encoding="utf-8"))
                )
            }
        counts: dict[str, int] = defaultdict(int)
        fallback_counts: dict[str, int] = defaultdict(int)
        for candidate in selected:
            counts[candidate.source_id] += 1
            if candidate.provider == "archive-search":
                fallback_counts[candidate.source_id] += 1
        self.coverage = []
        for source in self.sources:
            prior = original_coverage.get(source.source_id)
            self.coverage.append(
                SourceCoverage(
                    source_id=source.source_id,
                    source_name=source.name,
                    source_url=source.url,
                    dated_candidates=counts.get(source.source_id, 0),
                    fallback_used=bool(fallback_counts.get(source.source_id, 0)),
                    fallback_candidates=fallback_counts.get(source.source_id, 0),
                    incomplete=prior.incomplete if prior else True,
                    errors=[
                        *(prior.errors if prior else ["source_coverage_unavailable"]),
                        f"reused_from_corpus_run:{self.options.corpus_run_id}",
                    ],
                )
            )
        self._write_coverage()
        return {
            "sources": len(self.sources),
            "corpus_reused": len(selected),
            "distinct_urls": len({item.canonical_url for item in selected}),
            "omitted_undated": omitted_undated,
            "omitted_outside_window": omitted_outside,
            "incomplete_sources": sum(item.incomplete for item in self.coverage),
        }

    def _discover_source_archive(
        self, source: CuratedSource
    ) -> tuple[SourceCoverage, list[DiscoveryCandidate]]:
        coverage = SourceCoverage(
            source_id=source.source_id,
            source_name=source.name,
            source_url=source.url,
        )
        roots = self._sitemap_roots(source, coverage)
        queue = [(url, 0) for url in roots]
        seen_docs: set[str] = set()
        entries: dict[str, tuple[str, datetime | None]] = {}
        while queue and len(seen_docs) < MAX_SITEMAP_DOCUMENTS:
            url, depth = queue.pop(0)
            if url in seen_docs or depth > MAX_SITEMAP_DEPTH:
                continue
            seen_docs.add(url)
            try:
                response = self.fetch(url)
                root = _parse_sitemap(response.content)
            except Exception as exc:
                coverage.errors.append(f"sitemap:{url}:{type(exc).__name__}")
                continue
            coverage.sitemap_documents += 1
            if _local(root.tag) == "sitemapindex":
                for node in root:
                    child_url = _child_text(node, "loc")
                    if not child_url:
                        continue
                    try:
                        child_url = canonicalize_url(child_url)
                    except ValueError:
                        continue
                    if same_registrable_domain(child_url, source.url):
                        queue.append((child_url, depth + 1))
                continue
            if _local(root.tag) != "urlset":
                coverage.errors.append(f"sitemap:{url}:unsupported_root")
                continue
            for node in root:
                if len(entries) >= MAX_URLS_PER_SOURCE:
                    coverage.incomplete = True
                    break
                raw_url = _child_text(node, "loc")
                if not raw_url:
                    continue
                try:
                    page_url = canonicalize_url(raw_url)
                except ValueError:
                    continue
                if not same_registrable_domain(page_url, source.url):
                    continue
                if not _archive_url_in_scope(source, page_url):
                    coverage.incomplete = True
                    if "regional_scope_requires_search_fallback" not in coverage.errors:
                        coverage.errors.append("regional_scope_requires_search_fallback")
                    continue
                title = _descendant_text(node, "title")
                raw_date = _descendant_text(node, "publication_date") or _child_text(node, "lastmod")
                hint = parse_datetime(raw_date) if raw_date else None
                url_date = date_from_url(page_url)
                if hint and hint.date() < self.since:
                    continue
                if hint and hint.date() > self.archive_until and url_date != self.archive_until:
                    continue
                if url_date and not self.since <= url_date <= self.archive_until:
                    continue
                if article_score(page_url, title, source.url) < 2:
                    continue
                entries.setdefault(page_url, (title, hint))
        if queue:
            coverage.incomplete = True
            coverage.errors.append("sitemap_document_cap_reached")
        coverage.sitemap_urls = len(entries)
        candidates = list(self._persisted_archive_by_source.get(source.source_id, []))
        existing_urls = set(self._persisted_archive_by_url)
        reused = [
            self._persisted_archive_by_url[url]
            for url in entries
            if url in self._persisted_archive_by_url
            and self._persisted_archive_by_url[url] not in candidates
        ]
        candidates.extend(reused)
        pending = [
            (page_url, sitemap_title)
            for page_url, (sitemap_title, _) in entries.items()
            if page_url not in existing_urls
        ]

        def fetch_page(item: tuple[str, str]) -> tuple[DiscoveryCandidate | None, str, bool]:
            page_url, sitemap_title = item
            try:
                with self._archive_fetch_slots:
                    page = self.fetch(page_url)
                canonical = canonicalize_url(page.url)
                published = publication_date(page.text, canonical)
                if not published:
                    return None, "", True
                if not self.since <= published.date() <= self.archive_until:
                    return None, "", False
                artifact = self.artifacts.write_raw_text(
                    "archive-discover",
                    f"article-{stable_hash(canonical)[:20]}.html",
                    page.text,
                )
                title = sitemap_title or _html_title(page.text)
                item = DiscoveryCandidate(
                    candidate_id=candidate_id("archive", "", canonical),
                    run_id=self.options.run_id,
                    provider="archive",
                    discovered_url=page_url,
                    resolved_url=canonical,
                    canonical_url=canonical,
                    title=title,
                    source_id=source.source_id,
                    source_name=source.name,
                    source_domain=source.domain,
                    published_at=published,
                    raw_artifact_path=artifact["path"],
                    raw_artifact_hash=artifact["sha256"],
                    metadata={"archive_method": "sitemap"},
                )
                return item, "", False
            except Exception as exc:
                return None, f"article:{page_url}:{type(exc).__name__}", False

        # A source-level pool eliminates the serial long tail when one large
        # sitemap remains after the outer source pool has drained.
        with ThreadPoolExecutor(max_workers=max(1, self.options.workers)) as pool:
            page_results = list(pool.map(fetch_page, pending))
        coverage.pages_fetched += len(pending)
        for candidate, error, undated in page_results:
            if error:
                coverage.errors.append(error)
            if undated:
                coverage.undated_pages += 1
            if candidate:
                self.state.save_candidate(candidate)
                candidates.append(candidate)
        coverage.dated_candidates = len(candidates)
        return coverage, candidates

    def _screen(self) -> dict:
        candidates = [
            item
            for item in self.state.candidates_for_run(self.options.run_id)
            if item.record_status == RecordStatus.VALID
            and item.provider not in {"seed"}
        ]
        selected = dedupe_candidates_exact(candidates)
        kept = rejected = ambiguous = 0
        for candidate in selected:
            decision = _offline_screen(candidate)
            metadata = {
                **candidate.metadata,
                "bulk_screen": decision,
                "selected_for_qualification": decision != "reject",
            }
            status = candidate.record_status
            if decision == "reject":
                status = RecordStatus.REJECTED
                rejected += 1
            elif decision == "ambiguous":
                ambiguous += 1
                kept += 1
            else:
                kept += 1
            self.state.save_candidate(
                candidate.model_copy(update={"metadata": metadata, "record_status": status})
            )
        return {
            "submitted": len(candidates),
            "distinct_urls": len(selected),
            "selected": kept,
            "ambiguous": ambiguous,
            "rejected": rejected,
        }

    def _sitemap_roots(
        self, source: CuratedSource, coverage: SourceCoverage
    ) -> list[str]:
        base = f"{urlsplit(source.url).scheme}://{urlsplit(source.url).netloc}"
        roots: list[str] = []
        try:
            robots = self.fetch(f"{base}/robots.txt").text
            for line in robots.splitlines():
                if line.casefold().startswith("sitemap:"):
                    value = line.split(":", 1)[1].strip()
                    try:
                        url = canonicalize_url(value)
                    except ValueError:
                        continue
                    if same_registrable_domain(url, source.url):
                        roots.append(url)
        except Exception as exc:
            coverage.errors.append(f"robots:{type(exc).__name__}")
        for path in CONVENTIONAL_SITEMAPS:
            roots.append(canonicalize_url(urljoin(base, path)))
        explicit_source = canonicalize_url(source.url)
        return [url for url in dict.fromkeys(roots) if url != explicit_source]

    def _fallback_source(
        self, coverage: SourceCoverage
    ) -> tuple[SourceCoverage, list[DiscoveryCandidate]]:
        source = next(item for item in self.sources if item.source_id == coverage.source_id)
        prompt = ARCHIVE_FALLBACK_PROMPT.format(
            since=self.options.since,
            until=self.archive_until.isoformat(),
            source_id=source.source_id,
            source_name=source.name,
            source_url=source.url,
        )
        attempt_id = stable_uuid("attempt", self.options.run_id, "archive-fallback", source.source_id)
        request = self.artifacts.write_raw(
            "archive-fallback", f"{attempt_id}-request.json", {"model": self.options.model, "prompt": prompt}
        )
        started = datetime.now(timezone.utc).isoformat()
        rows: list[DiscoveryCandidate] = []
        try:
            text, usage = self.model_call(self.options.model, prompt, [{"type": "web_search"}])
            response = self.artifacts.write_raw_text(
                "archive-fallback", f"{attempt_id}-response.txt", text
            )
            payload = _parse_object(text)
            if str(payload.get("source_id") or "") != source.source_id:
                raise ValueError("fallback source_id mismatch")
            urls = payload.get("urls") or []
            if not isinstance(urls, list):
                raise ValueError("fallback urls must be a list")
            for raw_url in urls[:100]:
                try:
                    url = canonicalize_url(str(raw_url))
                    if not same_registrable_domain(url, source.url):
                        continue
                    page = self.fetch(url)
                    published = publication_date(page.text, page.url)
                    if not published or not self.since <= published.date() <= self.archive_until:
                        continue
                    canonical = canonicalize_url(page.url)
                    artifact = self.artifacts.write_raw_text(
                        "archive-fallback",
                        f"article-{stable_hash(canonical)[:20]}.html",
                        page.text,
                    )
                    candidate = DiscoveryCandidate(
                        candidate_id=candidate_id("archive-search", "", canonical),
                        run_id=self.options.run_id,
                        provider="archive-search",
                        discovered_url=url,
                        resolved_url=canonical,
                        canonical_url=canonical,
                        title=_html_title(page.text),
                        source_id=source.source_id,
                        source_name=source.name,
                        source_domain=source.domain,
                        published_at=published,
                        raw_artifact_path=artifact["path"],
                        raw_artifact_hash=artifact["sha256"],
                        metadata={"archive_method": "grok_search_fallback"},
                    )
                    self.state.save_candidate(candidate)
                    rows.append(candidate)
                except Exception:
                    continue
            self.state.record_provider_attempt(
                attempt_id=attempt_id,
                run_id=self.options.run_id,
                stage="archive-fallback",
                provider="model",
                target_type="source",
                target_id=source.source_id,
                status="completed",
                token_usage=usage,
                request_artifact_path=request["path"],
                response_artifact_path=response["path"],
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as exc:
            coverage.errors.append(f"fallback:{type(exc).__name__}:{exc}")
            coverage.incomplete = True
            self.state.record_provider_attempt(
                attempt_id=attempt_id,
                run_id=self.options.run_id,
                stage="archive-fallback",
                provider="model",
                target_type="source",
                target_id=source.source_id,
                status="failed",
                request_artifact_path=request["path"],
                error={"type": type(exc).__name__, "message": str(exc)},
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        coverage.fallback_used = True
        coverage.fallback_candidates = len(rows)
        coverage.dated_candidates += len(rows)
        return coverage, rows

    def _qualify(self) -> dict:
        candidates = [
            item
            for item in self.state.candidates_for_run(self.options.run_id)
            if item.metadata.get("selected_for_qualification")
            and item.record_status in {RecordStatus.VALID, RecordStatus.REVIEW}
            and not item.metadata.get("bulk_qualified")
        ]
        candidates = [
            item.model_copy(
                update={"record_status": RecordStatus.VALID, "validation_errors": []}
            )
            if item.record_status == RecordStatus.REVIEW else item
            for item in candidates
        ]
        batches = list(_chunks(candidates, self.options.batch_size))
        with ThreadPoolExecutor(max_workers=self.options.workers) as pool:
            outcomes = list(pool.map(self._qualify_batch, batches))
        return {
            "submitted": len(candidates),
            "batches": len(batches),
            "qualified": sum(item["qualified"] for item in outcomes),
            "rejected": sum(item["rejected"] for item in outcomes),
            "reviews": sum(item["reviews"] for item in outcomes),
        }

    def _qualify_batch(self, candidates: list[DiscoveryCandidate]) -> dict[str, int]:
        # A complete binary split needs at most 2n-1 calls. The absolute ceiling
        # also protects direct callers that bypass the CLI's 25-candidate limit.
        budget = _BatchRecoveryBudget(
            min(
                MAX_QUALIFICATION_RECOVERY_CALLS,
                max(1, (2 * len(candidates)) - 1),
            )
        )
        return self._qualify_batch_attempt(candidates, budget)

    def _qualify_batch_attempt(
        self,
        candidates: list[DiscoveryCandidate],
        budget: _BatchRecoveryBudget,
    ) -> dict[str, int]:
        if not candidates:
            return {"qualified": 0, "rejected": 0, "reviews": 0}
        if not budget.claim():
            exc = _QualificationBatchContractError(
                "qualification recovery call budget exhausted"
            )
            for candidate in candidates:
                self._quarantine_bulk_candidate(candidate, "", exc)
            return {"qualified": 0, "rejected": 0, "reviews": len(candidates)}

        batch_id = stable_hash(*(item.candidate_id for item in candidates))[:20]
        payload = [
            {
                "candidate_id": item.candidate_id,
                "url": item.canonical_url,
                "title": item.title,
                "published_at": str(item.published_at or ""),
                "saved_article_excerpt": _candidate_excerpt(item, 2_500),
            }
            for item in candidates
        ]
        prompt = BULK_QUALIFICATION_PROMPT.format(
            since=self.since.isoformat(),
            until=self.until.isoformat(),
            candidates=json.dumps(payload, sort_keys=True, ensure_ascii=False)
        )
        attempt_id = stable_uuid(
            "attempt", self.options.run_id, "bulk-qualify", batch_id
        )
        request = self.artifacts.write_raw(
            "qualify", f"{attempt_id}-request.json",
            {"model": self.options.model, "prompt": prompt},
        )
        started = datetime.now(timezone.utc).isoformat()
        response_path = ""
        usage: dict = {}
        try:
            text, usage = self.model_call(self.options.model, prompt, [])
            response = self.artifacts.write_raw_text(
                "qualify", f"{attempt_id}-response.txt", text
            )
            response_path = response["path"]
            try:
                raw = _parse_object(text)
            except (TypeError, ValueError) as exc:
                raise _QualificationBatchContractError(str(exc)) from exc
            expected = {item.candidate_id for item in candidates}
            if set(raw) != expected:
                raise _QualificationBatchContractError(
                    "qualification IDs must match exactly; "
                    f"missing={sorted(expected - set(raw))}, "
                    f"unknown={sorted(set(raw) - expected)}"
                )
            judgments: dict[str, JudgmentPayload] = {}
            invalid: dict[str, Exception] = {}
            for key, value in raw.items():
                try:
                    judgments[key] = JudgmentPayload.model_validate(
                        _normalize_judgment(value)
                    )
                except Exception as exc:
                    invalid[key] = exc
        except _QualificationBatchContractError as exc:
            self.state.record_provider_attempt(
                attempt_id=attempt_id,
                run_id=self.options.run_id,
                stage="qualify",
                provider="model",
                target_type="discovery_candidate_batch",
                target_id=batch_id,
                status="review",
                token_usage=usage,
                request_artifact_path=request["path"],
                response_artifact_path=response_path,
                error={"type": type(exc).__name__, "message": str(exc)},
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            if len(candidates) > 1 and budget.remaining_calls > 0:
                midpoint = len(candidates) // 2
                left = self._qualify_batch_attempt(candidates[:midpoint], budget)
                right = self._qualify_batch_attempt(candidates[midpoint:], budget)
                return {
                    key: left[key] + right[key]
                    for key in ("qualified", "rejected", "reviews")
                }
            for candidate in candidates:
                self._quarantine_bulk_candidate(candidate, response_path, exc)
            return {"qualified": 0, "rejected": 0, "reviews": len(candidates)}
        except Exception as exc:
            for candidate in candidates:
                self._quarantine_bulk_candidate(candidate, response_path, exc)
            self.state.record_provider_attempt(
                attempt_id=attempt_id,
                run_id=self.options.run_id,
                stage="qualify",
                provider="model",
                target_type="discovery_candidate_batch",
                target_id=batch_id,
                status="review",
                token_usage=usage,
                request_artifact_path=request["path"],
                response_artifact_path=response_path,
                error={"type": type(exc).__name__, "message": str(exc)},
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            return {"qualified": 0, "rejected": 0, "reviews": len(candidates)}
        qualified = rejected = reviews = 0
        for candidate in candidates:
            if candidate.candidate_id in invalid:
                self._quarantine_bulk_candidate(
                    candidate, response_path, invalid[candidate.candidate_id]
                )
                reviews += 1
                continue
            judgment = judgments[candidate.candidate_id]
            metadata = {**candidate.metadata, "bulk_qualified": True}
            if not judgment.qualified:
                self.state.save_candidate(
                    candidate.model_copy(
                        update={"metadata": metadata, "record_status": RecordStatus.REJECTED}
                    )
                )
                rejected += 1
                continue
            if (
                candidate.published_at is not None
                and judgment.date_posted is not None
                and judgment.date_posted != candidate.published_at.date()
            ):
                self._quarantine_bulk_candidate(
                    candidate,
                    response_path,
                    ValueError(
                        "qualified article publication date disagrees with saved "
                        "candidate evidence: "
                        f"candidate={candidate.published_at.date().isoformat()}, "
                        f"model={judgment.date_posted.isoformat()}"
                    ),
                )
                reviews += 1
                continue
            effective_date = (
                candidate.published_at.date()
                if candidate.published_at is not None
                else judgment.date_posted
            )
            if effective_date is None:
                self._quarantine_bulk_candidate(
                    candidate,
                    response_path,
                    ValueError("qualified article publication date is missing"),
                )
                reviews += 1
                continue
            if effective_date < self.since or effective_date > self.until:
                self.state.save_candidate(
                    candidate.model_copy(
                        update={
                            "metadata": {
                                **metadata,
                                "bulk_rejection_reason": "outside_requested_publication_window",
                            },
                            "record_status": RecordStatus.REJECTED,
                        }
                    )
                )
                rejected += 1
                continue
            if not _business_is_grounded(candidate, judgment.business_name):
                self._quarantine_bulk_candidate(
                    candidate,
                    response_path,
                    ValueError("qualified business is not grounded in saved article evidence"),
                )
                reviews += 1
                continue
            judgment = judgment.model_copy(update={"date_posted": effective_date})
            self._save_bulk_event(candidate, judgment)
            self.state.save_candidate(candidate.model_copy(update={"metadata": metadata}))
            qualified += 1
        attempt_status = "review" if invalid else "completed"
        attempt_error = {
            "type": "PartialValidationError",
            "message": f"{len(invalid)} candidate judgments were invalid",
        } if invalid else {}
        self.state.record_provider_attempt(
            attempt_id=attempt_id,
            run_id=self.options.run_id,
            stage="qualify",
            provider="model",
            target_type="discovery_candidate_batch",
            target_id=batch_id,
            status=attempt_status,
            token_usage=usage,
            request_artifact_path=request["path"],
            response_artifact_path=response_path,
            error=attempt_error,
            started_at=started,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        return {"qualified": qualified, "rejected": rejected, "reviews": reviews}

    def _qualification_audit(self) -> dict:
        """Fail closed before dedup when persisted qualified events are ungrounded."""
        candidates = {
            item.candidate_id: item
            for item in self.state.candidates_for_run(self.options.run_id)
        }
        organizations = {
            item.organization_id: item for item in self.state.organizations()
        }
        events = self.state.events_for_run(self.options.run_id)
        invalid_ids: list[str] = []
        for event in events:
            if event.record_status != RecordStatus.VALID:
                continue
            candidate = candidates.get(event.primary_candidate_id)
            organization = organizations.get(event.organization_id)
            errors: list[str] = []
            if event.date_posted is None:
                errors.append("bulk_event_date_missing")
            elif event.date_posted < self.since or event.date_posted > self.until:
                errors.append("bulk_event_outside_requested_window")
            if candidate is None:
                errors.append("bulk_event_primary_candidate_missing")
            else:
                if (
                    event.date_posted is not None
                    and candidate.published_at is not None
                    and event.date_posted != candidate.published_at.date()
                ):
                    errors.append("bulk_event_candidate_date_mismatch")
                if organization is None or not _business_is_grounded(
                    candidate, organization.canonical_name
                ):
                    errors.append("bulk_event_business_not_grounded")
            if not errors:
                continue
            invalid_ids.append(event.lead_event_id)
            updated = event.model_copy(
                update={
                    "record_status": RecordStatus.REVIEW,
                    "validation_errors": list(
                        dict.fromkeys([*event.validation_errors, *errors])
                    ),
                }
            )
            self.state.save_lead_event(updated)
            self.state.add_review(
                ReviewItem(
                    review_id=stable_uuid(
                        "review",
                        self.options.run_id,
                        "qualification-audit",
                        event.lead_event_id,
                    ),
                    run_id=self.options.run_id,
                    stage="qualification-audit",
                    record_type="lead_event",
                    record_id=event.lead_event_id,
                    reason_code=errors[0],
                    validation_errors=errors,
                )
            )
        invalid_kept: list[str] = []
        if invalid_ids:
            placeholders = ",".join("?" for _ in invalid_ids)
            with self.state.connect() as conn:
                invalid_kept = [
                    row["kept_event_id"]
                    for row in conn.execute(
                        f"""SELECT DISTINCT kept_event_id FROM v2_event_merges
                            WHERE run_id=? AND kept_event_id IN ({placeholders})""",
                        (self.options.run_id, *invalid_ids),
                    )
                ]
        if invalid_kept:
            raise ValueError(
                "qualification audit found invalid deduplication anchors; "
                "start a clean run from the saved discovery corpus: "
                + ", ".join(sorted(invalid_kept))
            )
        return {
            "submitted": len(events),
            "valid": len(events) - len(invalid_ids),
            "reviews": len(invalid_ids),
        }

    def _save_bulk_event(
        self, candidate: DiscoveryCandidate, payload: JudgmentPayload
    ) -> None:
        org_id = organization_id(payload.business_name, "", payload.location)
        support_ids = candidate.metadata.get("exact_duplicate_candidate_ids") or [
            candidate.candidate_id
        ]
        support_candidates = {
            item.candidate_id: item for item in self.state.candidates_by_ids(support_ids)
        }
        evidence = [
            Evidence(
                url=support_candidates[item].canonical_url,
                supports="Saved source article for the qualified property event.",
                provider=support_candidates[item].provider,
            )
            for item in support_ids if item in support_candidates
        ] or [
            Evidence(
                url=candidate.canonical_url,
                supports="Saved source article for the qualified property event.",
                provider=candidate.provider,
            )
        ]
        self.state.save_organization(
            Organization(
                organization_id=org_id,
                canonical_name=payload.business_name.strip(),
                location=payload.location.strip(),
                evidence=evidence,
            )
        )
        lead_id = event_id(
            org_id, payload.event, payload.location,
            payload.date_posted or (
                candidate.published_at.date() if candidate.published_at else ""
            ),
        )
        self.state.save_lead_event(
            LeadEvent(
                lead_event_id=lead_id,
                run_id=self.options.run_id,
                organization_id=org_id,
                primary_candidate_id=candidate.candidate_id,
                supporting_candidate_ids=list(support_ids),
                event=payload.event.strip(),
                location=payload.location.strip(),
                date_posted=payload.date_posted or (
                    candidate.published_at.date() if candidate.published_at else None
                ),
                summary=payload.summary.strip(),
                priority=payload.priority,
                property_type=payload.property_type.strip() or "other",
                service_angle=payload.service_angle.strip(),
                filter_reason=payload.filter_reason.strip(),
                confidence=payload.confidence,
                evidence=evidence,
            )
        )

    def _quarantine_bulk_candidate(
        self, candidate: DiscoveryCandidate, response_path: str, exc: Exception
    ) -> None:
        error = f"{type(exc).__name__}:{exc}"
        self.state.save_candidate(
            candidate.model_copy(
                update={
                    "record_status": RecordStatus.REVIEW,
                    "validation_errors": [*candidate.validation_errors, error],
                }
            )
        )
        self.state.add_review(
            ReviewItem(
                review_id=stable_uuid(
                    "review", self.options.run_id, "bulk-qualify", candidate.candidate_id
                ),
                run_id=self.options.run_id,
                stage="qualify",
                record_type="discovery_candidate",
                record_id=candidate.candidate_id,
                reason_code="bulk_model_contract_invalid",
                validation_errors=[error],
                raw_artifact_path=response_path,
            )
        )

    def _seed(self) -> dict:
        if not self.options.seed_db:
            return {"events": 0, "organizations": 0, "candidates": 0}
        _verify_seed_manifest(
            self.options.seed_db,
            self.options.seed_run_id,
            overall_since=self.since,
            overall_until=self.until,
            archive_until=self.archive_until,
        )
        seed = StateStore(self.options.seed_db)
        events = seed.active_events_for_run(self.options.seed_run_id)
        candidate_ids = {
            candidate_id_value
            for event in events
            for candidate_id_value in event.supporting_candidate_ids
        }
        candidates = seed.candidates_by_ids(candidate_ids)
        organization_ids = {item.organization_id for item in events}
        organizations = seed.organizations(organization_ids)
        source_ids = {item.source_id for item in candidates}
        with seed.connect() as conn:
            source_rows = list(
                conn.execute(
                    f"SELECT * FROM v2_sources WHERE source_id IN ({', '.join('?' for _ in source_ids)})",
                    sorted(source_ids),
                )
            ) if source_ids else []
        for row in source_rows:
            self.state.upsert_source(
                row["source_id"], row["name"], row["url"], row["domain"], row["state"], bool(row["enabled"])
            )
        for candidate in candidates:
            self.state.save_candidate(candidate.model_copy(update={"run_id": self.options.run_id}))
        for organization in organizations:
            self.state.save_organization(organization)
        for event in events:
            self.state.save_lead_event(event.model_copy(update={"run_id": self.options.run_id}))
        return {
            "events": len(events),
            "organizations": len(organizations),
            "candidates": len(candidates),
        }

    def _dedup(self) -> dict:
        events = self.state.active_events_for_run(self.options.run_id)
        organizations = {
            item.organization_id: item for item in self.state.organizations()
        }
        buckets: dict[str, list[LeadEvent]] = defaultdict(list)
        for event in events:
            city = normalize_text(event.location.split(",", 1)[0]) or "unknown"
            buckets[city].append(event)
        batches: list[list[LeadEvent]] = []
        for rows in buckets.values():
            ordered = sorted(
                rows,
                key=lambda item: (
                    normalize_text(
                        organizations.get(item.organization_id).canonical_name
                        if organizations.get(item.organization_id)
                        else item.organization_id
                    ),
                    normalize_text(item.event),
                    item.lead_event_id,
                ),
            )
            batches.extend(_chunks(ordered, 40))
        outcomes = [
            self._dedup_batch(batch, organizations)
            for batch in batches if len(batch) > 1
        ]
        return {
            "submitted": len(events),
            "batches": len(outcomes),
            "events": len(self.state.active_events_for_run(self.options.run_id)),
            "reviews": sum(item["reviews"] for item in outcomes),
        }

    def _dedup_batch(
        self,
        events: list[LeadEvent],
        organizations: dict[str, Organization],
    ) -> dict[str, int]:
        batch_id = stable_hash(*(item.lead_event_id for item in events))[:20]
        inputs = [
            {
                "lead_event_id": event.lead_event_id,
                "organization": (
                    organizations[event.organization_id].canonical_name
                    if event.organization_id in organizations else event.organization_id
                ),
                "event": event.event,
                "location": event.location,
                "date_posted": str(event.date_posted or ""),
            }
            for event in events
        ]
        prompt = FUZZY_PROMPT.format(events=json.dumps(inputs, sort_keys=True))
        attempt_id = stable_uuid("attempt", self.options.run_id, "bulk-dedup", batch_id)
        request = self.artifacts.write_raw(
            "dedup", f"{attempt_id}-request.json",
            {"model": self.options.model, "prompt": prompt},
        )
        started = datetime.now(timezone.utc).isoformat()
        response_path = ""
        try:
            text, usage = self.model_call(self.options.model, prompt, [])
            response = self.artifacts.write_raw_text(
                "dedup", f"{attempt_id}-response.txt", text
            )
            response_path = response["path"]
            match = re.search(r"\[.*\]", re.sub(r"<<ccr:[^>]+>>", "", text), re.DOTALL)
            if not match:
                raise ValueError("dedup response did not contain a JSON list")
            groups = validate_fuzzy_groups(
                [item.lead_event_id for item in events], json.loads(match.group())
            )
        except Exception as exc:
            for event in events:
                self.state.add_review(
                    ReviewItem(
                        review_id=stable_uuid(
                            "review", self.options.run_id, "bulk-dedup", event.lead_event_id
                        ),
                        run_id=self.options.run_id,
                        stage="dedup",
                        record_type="lead_event",
                        record_id=event.lead_event_id,
                        reason_code="bulk_fuzzy_dedup_contract_invalid",
                        validation_errors=[f"{type(exc).__name__}:{exc}"],
                    )
                )
            self.state.record_provider_attempt(
                attempt_id=attempt_id, run_id=self.options.run_id, stage="dedup",
                provider="model", target_type="lead_event_batch", target_id=batch_id,
                status="review", request_artifact_path=request["path"],
                response_artifact_path=response_path,
                error={"type": type(exc).__name__, "message": str(exc)},
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            return {"events": len(events), "reviews": len(events)}
        by_id = {item.lead_event_id: item for item in events}
        for group in groups:
            kept = by_id[group.kept_id]
            members = [by_id[item] for item in group.member_ids]
            merged = kept.model_copy(
                update={
                    "supporting_candidate_ids": list(dict.fromkeys(
                        candidate_id_value
                        for member in members
                        for candidate_id_value in member.supporting_candidate_ids
                    )),
                    "evidence": list({
                        (evidence.url, evidence.supports, evidence.provider): evidence
                        for member in members for evidence in member.evidence
                    }.values()),
                }
            )
            self.state.save_lead_event(merged)
            for member in members:
                self.state.save_event_merge(
                    self.options.run_id, member.lead_event_id, kept.lead_event_id
                )
        self.state.record_provider_attempt(
            attempt_id=attempt_id, run_id=self.options.run_id, stage="dedup",
            provider="model", target_type="lead_event_batch", target_id=batch_id,
            status="completed", token_usage=usage,
            request_artifact_path=request["path"], response_artifact_path=response_path,
            started_at=started, completed_at=datetime.now(timezone.utc).isoformat(),
        )
        return {"events": len(groups), "reviews": 0}

    def _score(self) -> dict:
        events = self.state.active_events_for_run(self.options.run_id)
        existing = {
            item.lead_event_id for item in self.state.scores_for_run(self.options.run_id)
        }
        pending = [item for item in events if item.lead_event_id not in existing]
        batches = list(_chunks(pending, 40))
        outcomes = [self._score_batch(batch) for batch in batches]
        return {
            "submitted": len(events),
            "pending": len(pending),
            "batches": len(batches),
            "scored": len(existing) + sum(item["scored"] for item in outcomes),
            "reviews": sum(item["reviews"] for item in outcomes),
        }

    def _score_batch(self, events: list[LeadEvent]) -> dict[str, int]:
        budget = _BatchRecoveryBudget(
            min(
                MAX_SCORE_RECOVERY_CALLS,
                max(1, (2 * len(events)) - 1),
            )
        )
        return self._score_batch_attempt(events, budget)

    def _score_batch_attempt(
        self,
        events: list[LeadEvent],
        budget: _BatchRecoveryBudget,
    ) -> dict[str, int]:
        if not events:
            return {"scored": 0, "reviews": 0}
        if not budget.claim():
            exc = _ScoreBatchContractError("score recovery call budget exhausted")
            for event in events:
                self._quarantine_bulk_score(event, "", exc)
            return {"scored": 0, "reviews": len(events)}

        batch_id = stable_hash(*(item.lead_event_id for item in events))[:20]
        inputs = [
            {
                "lead_event_id": item.lead_event_id,
                "event": item.event,
                "location": item.location,
                "date_posted": str(item.date_posted or ""),
                "summary": item.summary,
                "priority": item.priority,
                "property_type": item.property_type,
                "service_angle": item.service_angle,
            }
            for item in events
        ]
        prompt = BULK_SCORE_PROMPT.format(events=json.dumps(inputs, sort_keys=True))
        attempt_id = stable_uuid("attempt", self.options.run_id, "bulk-score", batch_id)
        request = self.artifacts.write_raw(
            "score", f"{attempt_id}-request.json",
            {"model": self.options.model, "prompt": prompt},
        )
        started = datetime.now(timezone.utc).isoformat()
        response_path = ""
        usage: dict = {}
        try:
            text, usage = self.model_call(self.options.model, prompt, [])
            response = self.artifacts.write_raw_text(
                "score", f"{attempt_id}-response.txt", text
            )
            response_path = response["path"]
            try:
                parsed = _parse_object(text)
            except (TypeError, ValueError) as exc:
                raise _ScoreBatchContractError(str(exc)) from exc
            expected = {item.lead_event_id for item in events}
            actual = set(parsed)
            if actual != expected:
                raise _ScoreBatchContractError(
                    "score IDs must match exactly; "
                    f"missing={sorted(expected - actual)}, "
                    f"unknown={sorted(actual - expected)}"
                )
        except _ScoreBatchContractError as exc:
            self.state.record_provider_attempt(
                attempt_id=attempt_id, run_id=self.options.run_id, stage="score",
                provider="model", target_type="lead_event_batch", target_id=batch_id,
                status="review", token_usage=usage,
                request_artifact_path=request["path"],
                response_artifact_path=response_path,
                error={"type": type(exc).__name__, "message": str(exc)},
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            if len(events) > 1 and budget.remaining_calls > 0:
                midpoint = len(events) // 2
                left = self._score_batch_attempt(events[:midpoint], budget)
                right = self._score_batch_attempt(events[midpoint:], budget)
                return {
                    key: left[key] + right[key]
                    for key in ("scored", "reviews")
                }
            for event in events:
                self._quarantine_bulk_score(event, response_path, exc)
            return {"scored": 0, "reviews": len(events)}
        except Exception as exc:
            for event in events:
                self._quarantine_bulk_score(event, response_path, exc)
            self.state.record_provider_attempt(
                attempt_id=attempt_id, run_id=self.options.run_id, stage="score",
                provider="model", target_type="lead_event_batch", target_id=batch_id,
                status="review", token_usage=usage,
                request_artifact_path=request["path"],
                response_artifact_path=response_path,
                error={"type": type(exc).__name__, "message": str(exc)},
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            return {"scored": 0, "reviews": len(events)}
        scored = reviews = 0
        for event in events:
            try:
                score = _validate_bulk_score(
                    event.lead_event_id, parsed[event.lead_event_id]
                )
            except Exception as exc:
                self._quarantine_bulk_score(event, response_path, exc)
                reviews += 1
                continue
            self.state.save_score(
                LeadScore(
                    run_id=self.options.run_id,
                    lead_event_id=event.lead_event_id,
                    score=score,
                    model=self.options.model,
                    attempt_id=attempt_id,
                )
            )
            scored += 1
        attempt_status = "review" if reviews else "completed"
        attempt_error = {
            "type": "PartialValidationError",
            "message": f"{reviews} event scores were invalid",
        } if reviews else {}
        self.state.record_provider_attempt(
            attempt_id=attempt_id, run_id=self.options.run_id, stage="score",
            provider="model", target_type="lead_event_batch", target_id=batch_id,
            status=attempt_status, token_usage=usage,
            request_artifact_path=request["path"], response_artifact_path=response_path,
            error=attempt_error,
            started_at=started, completed_at=datetime.now(timezone.utc).isoformat(),
        )
        return {"scored": scored, "reviews": reviews}

    def _quarantine_bulk_score(
        self,
        event: LeadEvent,
        response_path: str,
        exc: Exception,
    ) -> None:
        self.state.add_review(
            ReviewItem(
                review_id=stable_uuid(
                    "review", self.options.run_id, "bulk-score", event.lead_event_id
                ),
                run_id=self.options.run_id,
                stage="score",
                record_type="lead_event",
                record_id=event.lead_event_id,
                reason_code="bulk_score_contract_invalid",
                validation_errors=[f"{type(exc).__name__}:{exc}"],
                raw_artifact_path=response_path,
            )
        )

    def _companies(self) -> dict:
        events = self.state.active_events_for_run(self.options.run_id)
        organizations = {
            item.organization_id: item
            for item in self.state.organizations({event.organization_id for event in events})
        }
        scores = {item.lead_event_id: item.score for item in self.state.scores_for_run(self.options.run_id)}
        groups: dict[str, list[LeadEvent]] = defaultdict(list)
        for event in events:
            org = organizations.get(event.organization_id)
            groups[normalize_text(org.canonical_name if org else event.organization_id)].append(event)
        raw_dir = self.artifacts.raw_dir / "company-profiles"
        raw_dir.mkdir(parents=True, exist_ok=True)

        def enrich(group_item: tuple[str, list[LeadEvent]]) -> CompanyProfile:
            key, group_events = group_item
            profile_key = stable_uuid("bulk-company-profile", key)
            path = raw_dir / f"{profile_key}.json"
            if self.options.resume and path.exists():
                cached = CompanyProfile.model_validate_json(path.read_text(encoding="utf-8"))
                if "primary" in cached.variants:
                    return cached
            profile = self._enrich_company(profile_key, group_events, organizations, scores)
            path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
            return profile

        with ThreadPoolExecutor(max_workers=self.options.workers) as pool:
            profiles = list(pool.map(enrich, sorted(groups.items())))
        profiles = _dedupe_profiles(profiles)
        for profile in profiles:
            why_line = _profile_why_line(profile)
            if why_line.status != "review":
                continue
            self.state.add_review(
                ReviewItem(
                    review_id=stable_uuid(
                        "review", self.options.run_id, "company-why", profile.profile_key
                    ),
                    run_id=self.options.run_id,
                    stage="companies",
                    record_type="company_profile",
                    record_id=profile.profile_key,
                    reason_code="company_why_line_invalid",
                    validation_errors=why_line.validation_errors or ["company_why_line_invalid"],
                    raw_artifact_path=profile.raw_artifact_path,
                )
            )
        self.profiles = profiles
        self._write_profiles(profiles)
        return {
            "submitted": len(groups),
            "companies": len(profiles),
            "valid": sum(item.record_status == "valid" for item in profiles),
            "reviews": sum(item.record_status != "valid" for item in profiles),
        }

    def _enrich_company(
        self,
        profile_key: str,
        events: list[LeadEvent],
        organizations: dict[str, Organization],
        scores: dict[str, int],
    ) -> CompanyProfile:
        anchor = _anchor_event(events, scores)
        orgs = [organizations[item] for item in {event.organization_id for event in events} if item in organizations]
        names = list(dict.fromkeys(item.canonical_name for item in orgs))
        locations = list(dict.fromkeys([*(item.location for item in orgs), *(event.location for event in events)]))
        event_payload = [
            {
                "lead_event_id": event.lead_event_id,
                "event": event.event,
                "date": str(event.date_posted or ""),
                "location": event.location,
                "summary": event.summary,
                "priority": event.priority,
                "score": scores.get(event.lead_event_id),
                "sources": [item.url for item in event.evidence],
            }
            for event in events
        ]
        prompt = COMPANY_PROMPT.format(
            template_catalog=_template_catalog(),
            profile_key=profile_key,
            company_name=names[0] if names else profile_key,
            domain="",
            names=json.dumps(names),
            locations=json.dumps(locations),
            anchor_id=anchor.lead_event_id,
            events=json.dumps(event_payload, sort_keys=True),
        )
        attempt_id = stable_uuid("attempt", self.options.run_id, "companies", profile_key)
        request = self.artifacts.write_raw(
            "companies", f"{attempt_id}-request.json", {"model": self.options.model, "prompt": prompt}
        )
        started = datetime.now(timezone.utc).isoformat()
        try:
            text, usage = self.model_call(self.options.model, prompt, [{"type": "web_search"}])
            response = self.artifacts.write_raw_text(
                "companies", f"{attempt_id}-response.txt", text
            )
            payload = _parse_object(text)
            event_evidence_urls = list(dict.fromkeys(
                item.url for event in events for item in event.evidence
            ))
            canonical_name = str(payload.get("canonical_name") or names[0]).strip()
            aliases = list(dict.fromkeys([
                *names,
                *(alias for org in orgs for alias in org.aliases),
            ]))
            why_line = _why_line_from_payload(
                payload,
                allowed_event_ids={event.lead_event_id for event in events},
                known_company_names=[canonical_name, *aliases],
            )
            domain = _domain(str(payload.get("domain") or ""))
            profile = CompanyProfile(
                profile_key=profile_key,
                canonical_name=canonical_name,
                domain=domain,
                aliases=aliases,
                locations=[item for item in locations if item],
                employee_count=str(payload.get("employee_count") or ""),
                organization_ids=sorted({event.organization_id for event in events}),
                lead_event_ids=sorted(event.lead_event_id for event in events),
                anchor_lead_event_id=anchor.lead_event_id,
                relationship_type=_normalized_choice(
                    payload.get("relationship_type"),
                    {"operator", "owner", "property_manager", "developer", "contractor", "broker", "tenant", "project", "unknown"},
                    "unknown",
                ),
                relationship_confidence=_normalized_choice(
                    payload.get("relationship_confidence"),
                    {"high", "medium", "low"},
                    "low",
                ),
                relationship_sources=list(dict.fromkeys(
                    str(value).strip()
                    for value in payload.get("relationship_sources", [])
                    if isinstance(value, str) and str(value).strip()
                )),
                outreach_route=_normalized_choice(
                    payload.get("outreach_route"),
                    {"direct_facility", "construction_closeout", "referral", "review"},
                    "review",
                ),
                variants={"primary": why_line},
                evidence_urls=event_evidence_urls,
                record_status=_profile_record_status(why_line),
                raw_artifact_path=response["path"],
            )
            self.state.record_provider_attempt(
                attempt_id=attempt_id,
                run_id=self.options.run_id,
                stage="companies",
                provider="model",
                target_type="company_profile",
                target_id=profile_key,
                status="completed",
                token_usage=usage,
                request_artifact_path=request["path"],
                response_artifact_path=response["path"],
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            return profile
        except Exception as exc:
            self.state.record_provider_attempt(
                attempt_id=attempt_id,
                run_id=self.options.run_id,
                stage="companies",
                provider="model",
                target_type="company_profile",
                target_id=profile_key,
                status="review",
                request_artifact_path=request["path"],
                error={"type": type(exc).__name__, "message": str(exc)},
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            return CompanyProfile(
                profile_key=profile_key,
                canonical_name=names[0] if names else profile_key,
                aliases=names,
                locations=[item for item in locations if item],
                organization_ids=sorted({event.organization_id for event in events}),
                lead_event_ids=sorted(event.lead_event_id for event in events),
                anchor_lead_event_id=anchor.lead_event_id,
                variants={"primary": WhyVariant(validation_errors=[f"company_contract:{type(exc).__name__}"])},
                evidence_urls=list(dict.fromkeys([item.url for event in events for item in event.evidence])),
                record_status="review",
            )

    def _export(self) -> dict:
        events = self.state.active_events_for_run(self.options.run_id)
        organizations = {item.organization_id: item for item in self.state.organizations()}
        candidates = {
            item.candidate_id: item for item in self.state.candidates_for_run(self.options.run_id)
        }
        scores = {item.lead_event_id: item.score for item in self.state.scores_for_run(self.options.run_id)}
        profiles = self._load_profiles()
        profile_by_event = {
            event_id: profile for profile in profiles for event_id in profile.lead_event_ids
        }
        lead_fields = [
            "lead_event_id", "company_id", "business_name", "event", "date_posted",
            "location", "priority", "score", "property_type", "service_angle", "summary",
            "article_url", "why_line", "why_template_key", "why_confidence",
            "why_sources", "why_line_status", "supporting_candidate_ids", "run_id",
            "record_status", "provenance_json",
        ]
        lead_rows = []
        for event in events:
            profile = profile_by_event.get(event.lead_event_id)
            org = organizations.get(event.organization_id)
            primary = candidates.get(event.primary_candidate_id)
            lead_rows.append(
                {
                    "lead_event_id": event.lead_event_id,
                    "company_id": profile.company_id if profile else "",
                    "business_name": profile.canonical_name if profile else (org.canonical_name if org else ""),
                    "event": event.event,
                    "date_posted": str(event.date_posted or ""),
                    "location": event.location,
                    "priority": event.priority,
                    "score": scores.get(event.lead_event_id, ""),
                    "property_type": event.property_type,
                    "service_angle": event.service_angle,
                    "summary": event.summary,
                    "article_url": primary.canonical_url if primary else (event.evidence[0].url if event.evidence else ""),
                    "why_line": _why_line_text(profile),
                    "why_template_key": _profile_why_line(profile).template_key,
                    "why_confidence": _profile_why_line(profile).confidence,
                    "why_sources": _why_line_sources(profile),
                    "why_line_status": _profile_why_line(profile).status,
                    "supporting_candidate_ids": ",".join(event.supporting_candidate_ids),
                    "run_id": self.options.run_id,
                    "record_status": event.record_status.value,
                    "provenance_json": json.dumps([item.model_dump(mode="json") for item in event.evidence], default=str, sort_keys=True),
                }
            )
        lead_rows.sort(key=lambda item: (-int(item["score"] or -1), item["lead_event_id"]))
        company_fields = [
            "company_id", "business_name", "domain", "aliases", "locations", "employee_count",
            "lead_event_ids", "lead_event_count", "anchor_lead_event_id", "why_line",
            "why_template_key", "why_confidence", "why_sources", "why_line_status",
            "record_status", "run_id", "provenance_json",
        ]
        company_rows = [_company_row(item, self.options.run_id) for item in profiles]
        company_rows.sort(key=lambda item: (-int(item["lead_event_count"]), item["business_name"]))
        final_dir = self.artifacts.final_dir
        _write_csv(final_dir / "leads.csv", lead_fields, lead_rows)
        _write_csv(final_dir / "companies.csv", company_fields, company_rows)
        _write_jsonl(final_dir / "lead_events.jsonl", events)
        _write_jsonl(final_dir / "company_profiles.jsonl", profiles)
        _write_jsonl(
            final_dir / "reviews.jsonl",
            self.state.reviews_for_run(self.options.run_id),
        )
        for path, kind in (
            (final_dir / "leads.csv", "csv"),
            (final_dir / "companies.csv", "csv"),
            (final_dir / "lead_events.jsonl", "jsonl"),
            (final_dir / "company_profiles.jsonl", "jsonl"),
            (final_dir / "reviews.jsonl", "jsonl"),
            (final_dir / "coverage.csv", "csv"),
        ):
            self.artifacts.record_existing("export", kind, path)
        return {
            "leads": len(lead_rows),
            "companies": len(company_rows),
            "reviews": len(self.state.reviews_for_run(self.options.run_id)),
        }

    def _write_why_line_revision(
        self,
        stage: str,
        revision_dir: Path,
        profiles: list[CompanyProfile],
        counters: dict[str, int],
    ) -> None:
        profile_by_company = {profile.company_id: profile for profile in profiles}
        source_leads = self.artifacts.final_dir / "leads.csv"
        if not source_leads.exists():
            raise ValueError("completed run is missing final/leads.csv")
        with source_leads.open(newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            lead_fields = list(reader.fieldnames or [])
            lead_rows = list(reader)
        legacy_fields = {
            "why_line_a", "why_line_b", "why_line_c",
            "why_sources_a", "why_sources_b", "why_sources_c",
        }
        lead_fields = [field for field in lead_fields if field not in legacy_fields]
        why_fields = (
            "why_line", "why_template_key", "why_confidence",
            "why_sources", "why_line_status",
        )
        for field in why_fields:
            if field in lead_fields:
                lead_fields.remove(field)
        insert_at = lead_fields.index("article_url") + 1 if "article_url" in lead_fields else 0
        lead_fields[insert_at:insert_at] = list(why_fields)
        for row in lead_rows:
            profile = profile_by_company.get(row.get("company_id", ""))
            why_line = _profile_why_line(profile)
            row["why_line"] = _why_line_text(profile)
            row["why_template_key"] = why_line.template_key
            row["why_confidence"] = why_line.confidence
            row["why_sources"] = _why_line_sources(profile)
            row["why_line_status"] = why_line.status

        company_fields = [
            "company_id", "business_name", "domain", "aliases", "locations",
            "employee_count", "lead_event_ids", "lead_event_count",
            "anchor_lead_event_id", "why_line", "why_template_key",
            "why_confidence", "why_sources", "why_line_status", "record_status",
            "run_id", "provenance_json",
        ]
        company_rows = [_company_row(profile, self.options.run_id) for profile in profiles]
        company_rows.sort(
            key=lambda item: (-int(item["lead_event_count"]), item["business_name"])
        )
        revision_dir.mkdir(parents=True, exist_ok=True)
        _write_csv(revision_dir / "leads.csv", lead_fields, lead_rows)
        _write_csv(revision_dir / "companies.csv", company_fields, company_rows)
        _write_jsonl(revision_dir / "company_profiles.jsonl", profiles)
        invalid_record_ids = {
            profile.profile_key
            for profile in profiles
            if _profile_why_line(profile).status == "review"
        }
        revision_reviews = [
            item
            for item in self.state.reviews_for_run(self.options.run_id)
            if item.stage == stage and item.record_id in invalid_record_ids
        ]
        _write_jsonl(revision_dir / "reviews.jsonl", revision_reviews)

        usage: dict[str, int] = {"provider_attempts": 0}
        with self.state.connect() as conn:
            rows = list(
                conn.execute(
                    "SELECT token_usage_json FROM v2_provider_attempts "
                    "WHERE run_id=? AND stage=?",
                    (self.options.run_id, stage),
                )
            )
        usage["provider_attempts"] = len(rows)
        for row in rows:
            for key, value in json.loads(row["token_usage_json"] or "{}").items():
                if isinstance(value, (int, float)):
                    usage[key] = usage.get(key, 0) + int(value)
        summary = {
            "run_id": self.options.run_id,
            "protocol_version": WHY_LINE_PROTOCOL_VERSION,
            "model": self.options.model,
            "status": "completed",
            "one_model_call_per_business": True,
            "model_repair_calls": False,
            "counts": counters,
            "usage": usage,
            "output": str(revision_dir),
            "source_output_preserved": str(self.artifacts.final_dir),
        }
        self.artifacts.write_json(
            stage,
            f"{WHY_LINE_PROTOCOL_VERSION}/summary.json",
            summary,
        )
        for path, kind in (
            (revision_dir / "leads.csv", "csv"),
            (revision_dir / "companies.csv", "csv"),
            (revision_dir / "company_profiles.jsonl", "jsonl"),
            (revision_dir / "reviews.jsonl", "jsonl"),
        ):
            self.artifacts.record_existing(stage, kind, path)

    def _write_coverage(self) -> None:
        fields = list(SourceCoverage.model_fields)
        rows = []
        for item in self.coverage:
            value = item.model_dump(mode="json")
            value["errors"] = " | ".join(item.errors)
            rows.append(value)
        _write_csv(self.artifacts.final_dir / "coverage.csv", fields, rows)
        (self.artifacts.final_dir / "coverage.json").write_text(
            json.dumps([item.model_dump(mode="json") for item in self.coverage], indent=2),
            encoding="utf-8",
        )
        self.artifacts.record_existing("discover", "csv", self.artifacts.final_dir / "coverage.csv")
        self.artifacts.record_existing("discover", "json", self.artifacts.final_dir / "coverage.json")

    def _write_profiles(self, profiles: list[CompanyProfile]) -> None:
        _write_jsonl(self.artifacts.final_dir / "company_profiles.jsonl", profiles)

    def _load_profiles(self) -> list[CompanyProfile]:
        path = self.artifacts.final_dir / "company_profiles.jsonl"
        if not path.exists():
            return self.profiles
        return [
            CompanyProfile.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _hydrate(self, stage: str) -> None:
        if stage == "discover":
            path = self.artifacts.final_dir / "coverage.json"
            if path.exists():
                self.coverage = [SourceCoverage.model_validate(item) for item in json.loads(path.read_text())]
        elif stage == "companies":
            self.profiles = self._load_profiles()

    def _refresh_manifest(self) -> None:
        self.manifest.usage = self.state.usage_summary(self.options.run_id)
        self.manifest.artifacts = self.state.artifacts_for_run(self.options.run_id)
        self.artifacts.write_manifest(self.manifest)


def _parse_sitemap(content: bytes) -> ET.Element:
    payload = gzip.decompress(content) if content[:2] == b"\x1f\x8b" else content
    return ET.fromstring(payload)


def _offline_screen(candidate: DiscoveryCandidate) -> str:
    """Conservatively remove obvious non-Arizona/non-AEC pages without model spend."""
    headline = normalize_text(f"{candidate.title} {candidate.canonical_url}")
    body = ""
    if candidate.raw_artifact_path:
        try:
            raw = Path(candidate.raw_artifact_path).read_text(
                encoding="utf-8", errors="replace"
            )[:200_000]
            raw = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", raw)
            body = normalize_text(html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw)))
        except OSError:
            return "ambiguous"
    combined = f"{headline} {body[:40_000]}"
    headline_places = {term for term in ARIZONA_TERMS if _place_present(headline, term)}
    body_places = {term for term in ARIZONA_TERMS if _place_present(combined, term)}
    event_hits = {term for term in AEC_EVENT_TERMS if _prefix_present(combined, term)}
    if not event_hits:
        return "ambiguous"
    if not body_places:
        non_arizona = {
            term for term in NON_ARIZONA_STATE_TERMS if _term_present(combined, term)
        }
        return "reject" if non_arizona else "ambiguous"
    if headline_places or len(body_places) >= 2:
        return "keep"
    return "ambiguous"


def _term_present(value: str, term: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", value))


def _place_present(value: str, term: str) -> bool:
    if term not in {"mesa", "surprise", "gilbert"}:
        return _term_present(value, term)
    place_context = (
        rf"(?:\b(?:in|near|at|city of)\s+{re.escape(term)}\b)"
        rf"|(?:\b{re.escape(term)}(?:\s*,?\s*(?:arizona|az)\b"
        rf"|\s+(?:property|site|development|warehouse|industrial|retail|office|hotel)\b))"
    )
    return bool(re.search(place_context, value))


def _prefix_present(value: str, term: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(term)}", value))


def _archive_url_in_scope(source: CuratedSource, page_url: str) -> bool:
    """Prevent a regional curated entry from expanding into a national domain crawl."""
    source_parts = urlsplit(source.url)
    page_parts = urlsplit(page_url)
    host = source_parts.netloc.casefold()
    local_host_hints = (
        "arizona", "phoenix", "tucson", "azbigmedia", "azbex", "azcentral",
        "arizcc", "roselawgroupreporter",
    )
    if any(term in host for term in local_host_hints):
        return True
    scope_terms = {
        term
        for term in ("arizona", "phoenix", "tucson", "southwest")
        if term in normalize_text(source_parts.path)
        or term in normalize_text(source.name)
    }
    if not scope_terms:
        return source_parts.path in {"", "/"}
    page_scope = normalize_text(page_parts.path)
    return bool(scope_terms & {term for term in scope_terms if term in page_scope})


def _candidate_excerpt(candidate: DiscoveryCandidate, limit: int) -> str:
    if not candidate.raw_artifact_path:
        return ""
    try:
        raw = Path(candidate.raw_artifact_path).read_text(
            encoding="utf-8", errors="replace"
        )[:200_000]
        value = extract_main_text(
            raw,
            url=candidate.canonical_url,
            fast=True,
            favor_precision=True,
            include_comments=False,
            include_tables=False,
            deduplicate=True,
            prune_xpath=ARTICLE_PRUNE_XPATHS,
        )
    except Exception:
        return ""
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit]


def _normalize_judgment(value: object) -> object:
    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    raw_date = normalized.get("date_posted")
    if isinstance(raw_date, str) and raw_date.strip():
        parsed = parse_datetime(raw_date)
        if parsed:
            normalized["date_posted"] = parsed.date().isoformat()
    return normalized


def _validate_bulk_score(event_id: str, value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or int(value) != value
    ):
        raise ValueError(f"score for {event_id} must be an integer")
    score = int(value)
    if not 0 <= score <= 100:
        raise ValueError(f"score for {event_id} is outside 0-100")
    return score


_BUSINESS_GROUNDING_STOPWORDS = {
    "and", "asset", "assets", "buyer", "city", "company", "companies",
    "corp", "corporation", "development", "developments", "group", "inc",
    "llc", "multiple", "owner", "project", "projects", "properties",
    "property", "seller", "site", "the", "undisclosed", "unknown",
    "unnamed", "various",
}


def _business_is_grounded(
    candidate: DiscoveryCandidate, business_name: str
) -> bool:
    """Require one distinctive business token in that candidate's saved evidence."""
    business_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", normalize_text(business_name))
        if len(token) > 2 and token not in _BUSINESS_GROUNDING_STOPWORDS
    }
    if not business_tokens:
        return False
    evidence_tokens = set(
        re.findall(
            r"[a-z0-9]+",
            normalize_text(
                f"{candidate.title} {_candidate_excerpt(candidate, 2_500)}"
            ),
        )
    )
    return bool(business_tokens & evidence_tokens)


def _single_sendable_company_name(value: str) -> bool:
    """One outreach sequence must resolve to one operating company."""
    return " and " not in f" {normalize_text(value)} "


def _corpus_artifact_identity_matches(
    candidate: DiscoveryCandidate, payload: bytes
) -> bool:
    """Revalidate dynamic HTML by canonical URL and exact publication date."""
    if candidate.published_at is None:
        return False
    text = payload.decode("utf-8", errors="replace")
    published = publication_date(text, candidate.canonical_url)
    if published is None or published.date() != candidate.published_at.date():
        return False
    canonical_forms = {
        candidate.canonical_url,
        candidate.canonical_url.replace("https://", "http://", 1),
    }
    return any(value in text for value in canonical_forms)


def _chunks(items: list, size: int) -> Iterable[list]:
    for start in range(0, len(items), max(1, size)):
        yield items[start : start + max(1, size)]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _child_text(node: ET.Element, name: str) -> str:
    for child in node:
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _descendant_text(node: ET.Element, name: str) -> str:
    for child in node.iter():
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _html_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return " ".join(re.sub(r"<[^>]+>", " ", match.group(1)).split()) if match else ""


def _parse_object(text: str) -> dict:
    cleaned = re.sub(r"<<ccr:[^>]+>>", "", str(text or ""))
    match = re.search(r"\{.*\}", cleaned, re.S)
    if not match:
        raise ValueError("model response did not contain a JSON object")
    payload = json.loads(match.group())
    if not isinstance(payload, dict):
        raise ValueError("model response must be an object")
    return payload


def _word_count(value: str) -> int:
    return len(value.split())


def _sentence_count(value: str) -> int:
    normalized = re.sub(r"(?<=\d)\.(?=\d)", "", value)
    normalized = re.sub(
        r"\b(?:Inc|Corp|Co|Ltd|LLC|L\.L\.C|U\.S|U\.S\.A)\.(?=\s)",
        lambda match: match.group(0).replace(".", ""),
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"\b([A-Z])\.(?=\s+[A-Z])", r"\1", normalized)
    return len(re.findall(r"[.!?](?=\s+[A-Z]|$)", normalized))


def _clean_slot(value: object, *, lowercase: bool = True) -> str:
    cleaned = " ".join(str(value or "").split()).strip(" ,.;:!?")
    return cleaned.lower() if lowercase else cleaned


def _locality_reference(value: object) -> str:
    """Return one leaf locality without a state, parent place, or road detail."""
    locality = _clean_slot(value)
    if not locality:
        return ""
    locality = locality.split(",", 1)[0].strip()
    locality = re.split(r"\s+(?:and|/)\s+", locality, maxsplit=1)[0].strip()
    locality = re.split(r"\s+near\s+", locality, maxsplit=1)[0].strip()
    locality = re.sub(r"\s+(?:az|arizona)$", "", locality).strip()
    locality = re.sub(r"\s+(?:area|region|outskirts)$", "", locality).strip()
    locality = {
        "phoenix deer valley": "deer valley",
    }.get(locality, locality)
    broad_locations = {
        "arizona",
        "arizona cities",
        "east valley",
        "maricopa county",
        "metro phoenix",
        "phoenix metro",
        "pinal county",
        "west valley",
    }
    if (
        not locality
        or locality in broad_locations
        or locality.endswith(" county")
        or locality.endswith(" cities")
        or "," in locality
        or _word_count(locality) > 3
    ):
        return ""
    return locality


def _known_company_reference(
    value: object,
    known_company_names: Iterable[str],
) -> str:
    """Resolve a short company reference to casing supplied by a known name."""
    requested = _clean_slot(value, lowercase=False)
    requested_key = normalize_text(requested)
    if not requested_key or _word_count(requested) > 3:
        return ""
    candidates: list[str] = []
    for known_name in known_company_names:
        name = _clean_slot(known_name, lowercase=False)
        words = list(re.finditer(r"[A-Za-z0-9]+(?:[&'’.-][A-Za-z0-9]+)*", name))
        for start in range(len(words)):
            for width in range(1, min(3, len(words) - start) + 1):
                phrase = name[words[start].start():words[start + width - 1].end()]
                if normalize_text(phrase) == requested_key:
                    candidates.append(phrase)
    if not candidates:
        return ""
    return max(
        candidates,
        key=lambda candidate: (
            sum(character.isupper() for character in candidate),
            sum(
                character.isupper()
                for index, character in enumerate(candidate)
                if index > 0
            ),
        ),
    )


def _uses_sentence_case_only(
    value: str,
    *,
    company_references: Iterable[str] = (),
) -> bool:
    allowed_uppercase = {0}
    allowed_uppercase.update(
        match.start(1)
        for match in re.finditer(r"[.!?]\s+([a-z])", value, re.IGNORECASE)
    )
    allowed_uppercase.update(
        match.start()
        for match in re.finditer(r"\bI\b", value)
    )
    for reference in company_references:
        allowed_uppercase.update(
            index
            for match in re.finditer(re.escape(reference), value)
            for index in range(match.start(), match.end())
        )
    return all(
        not character.isupper() or index in allowed_uppercase
        for index, character in enumerate(value)
    )


def _why_line_from_payload(
    payload: dict,
    *,
    allowed_event_ids: set[str],
    known_company_names: Iterable[str] = (),
) -> WhyVariant:
    raw = payload.get("selection") or {}
    template_key = str(raw.get("template_key") or "").strip()
    lead_event_id = str(raw.get("lead_event_id") or "").strip()
    confidence = str(raw.get("confidence") or "low").casefold()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    sources = []
    for value in raw.get("source_urls") or []:
        try:
            sources.append(canonicalize_url(str(value)))
        except ValueError:
            continue
    sources = list(dict.fromkeys(sources))
    template = WHY_TEMPLATES.get(template_key)
    slots_raw = raw.get("slots") or {}
    errors: list[str] = []
    slots: dict[str, str] = {}
    if isinstance(slots_raw, dict):
        for key, value in slots_raw.items():
            slot_key = str(key)
            if slot_key == "company":
                if _word_count(_clean_slot(value, lowercase=False)) > 3:
                    errors.append("why_line_reference_length")
                resolved = _known_company_reference(value, known_company_names)
                slots[slot_key] = resolved
                if not resolved:
                    errors.append("why_line_company_reference")
            elif slot_key == "location":
                resolved = _locality_reference(value)
                slots[slot_key] = resolved
                if not resolved:
                    errors.append("why_line_location")
            else:
                slots[slot_key] = _clean_slot(value)
    if not template:
        errors.append("why_template_unknown")
    if not lead_event_id or lead_event_id not in allowed_event_ids:
        errors.append("why_line_event_id")
    if not sources:
        errors.append("why_line_unsourced")
    text = ""
    status = "review"
    if template:
        required = set(template["slots"])
        supplied = set(slots)
        if supplied != required:
            errors.append("why_line_slots")
        if any(not slots.get(key) for key in required):
            errors.append("why_line_slot_missing")
        if any(
            _word_count(value) > 3
            for key, value in slots.items()
            if key in WHY_SHORT_REFERENCE_SLOTS
        ):
            errors.append("why_line_reference_length")
        if any(
            _word_count(value) > 16
            for key, value in slots.items()
            if key not in WHY_SHORT_REFERENCE_SLOTS
        ):
            errors.append("why_line_slot_length")
        if any(re.search(r"(?:https?://|www\.)", value, re.IGNORECASE) for value in slots.values()):
            errors.append("why_line_slot_url")
        if template["sendable"] and not errors:
            text = str(template["text"]).format(**slots)
            if not 20 <= _word_count(text) <= 55:
                errors.append("why_line_word_count")
            if "—" in text or "–" in text:
                errors.append("why_line_dash")
            if _sentence_count(text) != 2 or not text.endswith("?"):
                errors.append("why_line_sentence_count")
            expected_prefix = (
                "Hi [first name] just wanted to reach out since I saw on the news that "
            )
            if not text.startswith(expected_prefix):
                errors.append("why_line_opener")
            company_references = [slots["company"]] if slots.get("company") else []
            if not _uses_sentence_case_only(
                text,
                company_references=company_references,
            ):
                errors.append("why_line_case")
            if not errors:
                status = "valid"
        elif not template["sendable"] and not errors:
            status = "skip"
    if errors:
        return WhyVariant(
            text="",
            template_key=template_key,
            lead_event_id=lead_event_id,
            slots=slots,
            confidence=confidence,
            source_urls=sources,
            status="review",
            validation_errors=errors,
        )
    return WhyVariant(
        text=text,
        template_key=template_key,
        lead_event_id=lead_event_id,
        slots=slots,
        confidence=confidence,
        source_urls=sources,
        status=status,
    )


def _profile_why_line(profile: CompanyProfile | None) -> WhyVariant:
    if not profile:
        return WhyVariant(validation_errors=["company_profile_missing"])
    return profile.variants.get(
        "primary", WhyVariant(validation_errors=["why_line_missing"])
    )


def _cap_people_by_company(people: list[Person], limit: int) -> list[Person]:
    counts: dict[str, int] = defaultdict(int)
    kept = []
    for person in people:
        if counts[person.organization_id] >= limit:
            continue
        counts[person.organization_id] += 1
        kept.append(person)
    return kept


def _first_name(full_name: str) -> str:
    parts = [part for part in re.split(r"\s+", full_name.strip()) if part]
    while parts and parts[0].casefold().rstrip(".") in {
        "dr", "mr", "mrs", "ms", "miss", "prof",
    }:
        parts.pop(0)
    if not parts:
        return ""
    return parts[0].strip(" ,")


def _personalize_why_line(text: str, first_name: str) -> str:
    if not first_name:
        raise ValueError("recipient has no usable first name")
    placeholder = "Hi [first name]"
    if not text.startswith(placeholder):
        raise ValueError("why line is missing the first-name placeholder")
    personalized = f"Hi {first_name}{text[len(placeholder):]}"
    if "[first name]" in personalized:
        raise ValueError("why line contains an unresolved first-name placeholder")
    return personalized


def _recipient_status(contact: ContactCandidate | None) -> str:
    if contact and contact.email:
        return "email"
    if contact and contact.phone:
        return "phone_only"
    if contact and contact.linkedin:
        return "linkedin_only"
    return "no_contact"


def _profile_record_status(why_line: WhyVariant) -> str:
    return "valid" if why_line.status in {"valid", "skip"} else "review"


def _anchor_event(events: list[LeadEvent], scores: dict[str, int]) -> LeadEvent:
    return shared_outreach.anchor_event(events, scores)


# The bulk workflow retains its local Pydantic projection for compatibility, but
# delegates the actual v4 contract and personalization rules to shared production
# code used by the daily pipeline.
def _why_line_from_payload(
    payload: dict,
    *,
    allowed_event_ids: set[str],
    known_company_names: Iterable[str] = (),
) -> WhyVariant:
    value = shared_outreach.parse_why_line_selection(
        payload,
        allowed_event_ids=allowed_event_ids,
        known_company_names=known_company_names,
    )
    return WhyVariant.model_validate(value.model_dump(mode="json"))


def _first_name(full_name: str) -> str:
    return shared_outreach.first_name(full_name)


def _personalize_why_line(text: str, first_name: str) -> str:
    return shared_outreach.personalize_why_line(text, first_name)


def _bulk_sales_handoff(
    *,
    run_id: str,
    profiles: list[CompanyProfile],
    events: list[LeadEvent],
    candidates: dict[str, DiscoveryCandidate],
    scores: dict[str, int],
    people: list[Person],
    contacts: list[ContactCandidate],
    open_review_ids: set[str],
) -> SalesHandoff:
    """Project a completed bulk revision into the production handoff contract."""
    events_by_id = {item.lead_event_id: item for item in events}
    profiles_by_id = {item.company_id: item for item in profiles}
    company_models = [
        CompanySync(
            company_id=profile.company_id,
            canonical_name=profile.canonical_name,
            domain=profile.domain,
            aliases=profile.aliases,
            legacy_ids=profile.organization_ids,
            relationship_type=profile.relationship_type,
            relationship_confidence=profile.relationship_confidence,
            relationship_sources=profile.relationship_sources,
            outreach_route=profile.outreach_route,
        )
        for profile in profiles
    ]

    event_models: list[LeadEventSync] = []
    for profile in profiles:
        if profile.anchor_lead_event_id not in events_by_id:
            raise ValueError(
                f"company {profile.company_id} is missing anchor event "
                f"{profile.anchor_lead_event_id}"
            )
        for lead_event_id in profile.lead_event_ids:
            event = events_by_id.get(lead_event_id)
            if event is None:
                raise ValueError(
                    f"company {profile.company_id} is missing event {lead_event_id}"
                )
            reasons: list[str] = []
            if event.record_status != RecordStatus.VALID:
                reasons.append("event_record_not_valid")
            if event.confidence != "high":
                reasons.append("event_confidence_not_high")
            if scores.get(event.lead_event_id, 0) <= 0:
                reasons.append("event_score_zero")
            if profile.company_id in open_review_ids or event.lead_event_id in open_review_ids:
                reasons.append("blocking_open_review")
            primary = candidates.get(event.primary_candidate_id)
            event_models.append(
                LeadEventSync(
                    run_id=run_id,
                    lead_event_id=event.lead_event_id,
                    company_id=profile.company_id,
                    organization_name=profile.canonical_name,
                    event_role=(
                        EventRole.ANCHOR
                        if event.lead_event_id == profile.anchor_lead_event_id
                        else EventRole.SUPPORTING
                    ),
                    event=event.event,
                    location=event.location,
                    date_posted=str(event.date_posted or ""),
                    summary=event.summary,
                    article_url=primary.canonical_url if primary else "",
                    score=scores.get(event.lead_event_id, 0),
                    confidence=event.confidence,
                    record_status=event.record_status.value,
                    actionable_route=True,
                    supporting_event_ids=[
                        item for item in profile.lead_event_ids if item != event.lead_event_id
                    ],
                    crm_eligible=not reasons,
                    crm_exclusion_reasons=reasons,
                )
            )

    people_by_id = {
        item.person_id: item
        for item in people
        if item.organization_id in profiles_by_id
    }
    preferred_contacts: dict[tuple[str, str], ContactCandidate] = {}
    for contact in contacts:
        if (
            not contact.selected
            or not contact.email
            or contact.person_id not in people_by_id
            or contact.organization_id not in profiles_by_id
            or not _contact_can_reach_warmy_verification(contact)
        ):
            continue
        key = (contact.organization_id, contact.person_id)
        prior = preferred_contacts.get(key)
        if prior is None or _bulk_contact_preference(contact) > _bulk_contact_preference(prior):
            preferred_contacts[key] = contact

    ranked: dict[str, list[tuple[int, list[str], ContactCandidate, Person]]] = defaultdict(list)
    for (company_id, person_id), contact in preferred_contacts.items():
        person = people_by_id[person_id]
        role_score, rationale = score_recipient_role(person.title, person.scope)
        if contact.provider.casefold() != "apollo":
            role_score += 2
            rationale = [*rationale, "non_apollo_source_contact_candidate"]
        ranked[company_id].append((role_score, rationale, contact, person))

    recipient_models: list[RecipientSync] = []
    recipient_reasons: dict[str, list[str]] = {}
    recipients_by_company: dict[str, list[RecipientSync]] = defaultdict(list)
    for company_id, rows in sorted(ranked.items()):
        profile = profiles_by_id[company_id]
        rows.sort(
            key=lambda row: candidate_rank_key(
                current_employer=current_employer_match(row[2].email, profile.domain),
                role_score=row[0],
                local_scope=any(value in normalize_text(row[3].scope) for value in ("arizona", "phoenix", "local", "regional")),
                non_fallback_provider=row[2].provider.casefold() != "apollo",
                verification_status=row[2].verification_status.value,
                evidence_count=len(row[2].evidence),
                stable_id=row[3].person_id,
            )
        )
        anchor = events_by_id[profile.anchor_lead_event_id]
        why_line = _profile_why_line(profile)
        sole_email = len({row[2].email.strip().casefold() for row in rows}) == 1
        for rank, (role_score, rationale, contact, person) in enumerate(rows, start=1):
            assessment = assess_recipient(
                verification_status=contact.verification_status.value,
                verification_reason=contact.verification_reason,
                primary=rank == 1,
                role_score=role_score,
                alternative_email_count=1 if sole_email else len(rows),
            )
            reasons: list[str] = [
                *assessment.hard_failures,
                *assessment.selection_blocks,
            ]
            rationale = [
                *rationale,
                *assessment.quality_signals,
                f"company_relationship_{profile.relationship_type}",
                f"outreach_route_{profile.outreach_route}",
            ]
            if profile.relationship_confidence == "low":
                rationale.append("company_relationship_confidence_low")
            if why_line.status != "valid":
                reasons.append(f"why_line_status_{why_line.status}")
            if why_line.confidence not in {"high", "medium"}:
                reasons.append("why_line_confidence_low")
            if anchor.record_status != RecordStatus.VALID:
                reasons.append("anchor_record_not_valid")
            if anchor.confidence != "high":
                reasons.append("anchor_confidence_not_high")
            if scores.get(anchor.lead_event_id, 0) <= 0:
                reasons.append("anchor_score_zero")
            if {
                company_id,
                anchor.lead_event_id,
                person.person_id,
                contact.contact_candidate_id,
            } & open_review_ids:
                reasons.append("blocking_open_review")
            first_name = 'team' if person.scope.startswith('company_mailbox:') else _first_name(person.name)
            if not first_name:
                reasons.append("recipient_first_name_missing")
                first_name = "unknown"
            recipient = RecipientSync(
                recipient_id=stable_uuid("recipient", company_id, person.person_id),
                company_id=company_id,
                person_id=person.person_id,
                contact_candidate_id=contact.contact_candidate_id,
                full_name=person.name,
                first_name=first_name,
                title=person.title,
                scope=person.scope,
                email=contact.email,
                source_provider=contact.provider,
                source_verification_status=contact.verification_status.value,
                source_verification_reason=contact.verification_reason,
                role_score=role_score,
                rank=rank,
                primary=rank == 1,
                selection_rationale=rationale,
            )
            recipient_models.append(recipient)
            recipients_by_company[company_id].append(recipient)
            recipient_reasons[recipient.recipient_id] = reasons

    sequences: list[OutreachSequenceSync] = []
    for profile in profiles:
        primary = next(
            (item for item in recipients_by_company.get(profile.company_id, []) if item.primary),
            None,
        )
        if primary is None:
            continue
        why_line = _profile_why_line(profile)
        personalized = _personalize_why_line(why_line.text, primary.first_name)
        merge_snapshot = {
            "firstName": primary.first_name,
            "company": profile.canonical_name,
            "whyLine": personalized,
            "unsubscribeUrl": "__integration_generated__",
        }
        merge_hash = hashlib.sha256(
            json.dumps(
                merge_snapshot,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        reasons = recipient_reasons[primary.recipient_id]
        sequences.append(
            OutreachSequenceSync(
                sequence_id=stable_uuid(
                    "outreach-sequence",
                    profile.company_id,
                    HANDOFF_PROTOCOL_VERSION,
                ),
                run_id=run_id,
                company_id=profile.company_id,
                campaign_protocol=HANDOFF_PROTOCOL_VERSION,
                anchor_lead_event_id=profile.anchor_lead_event_id,
                supporting_event_ids=[
                    item
                    for item in profile.lead_event_ids
                    if item != profile.anchor_lead_event_id
                ],
                primary_recipient_id=primary.recipient_id,
                why_template_key=why_line.template_key,
                why_slots=why_line.slots,
                why_sources=why_line.source_urls,
                why_confidence=why_line.confidence,
                company_why_line=why_line.text,
                personalized_why_line=personalized,
                merge_snapshot=merge_snapshot,
                merge_hash=merge_hash,
                eligibility_status=(
                    EligibilityStatus.READY if not reasons else EligibilityStatus.BLOCKED
                ),
                eligibility_reasons=reasons,
            )
        )

    value = SalesHandoff(
        schema_version=HANDOFF_SCHEMA_VERSION,
        protocol_version=HANDOFF_PROTOCOL_VERSION,
        run_id=run_id,
        companies=company_models,
        lead_events=event_models,
        recipients=recipient_models,
        sequences=sequences,
        content_hash="pending",
    )
    return value.model_copy(update={"content_hash": handoff_content_hash(value)})


def _contact_can_reach_warmy_verification(contact: ContactCandidate) -> bool:
    return contact.verification_status in {
        VerificationStatus.VERIFIED,
        VerificationStatus.UNKNOWN,
    }


def _block_cross_run_duplicate_events(
    handoff: SalesHandoff, sales_db: Path
) -> tuple[SalesHandoff, int]:
    """Fail closed on likely repeat stories already represented in sales state."""
    history: dict[str, list[dict]] = defaultdict(list)
    with sqlite3.connect(sales_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT e.lead_event_id, e.payload, c.canonical_name
               FROM sales_lead_events e
               JOIN sales_companies c ON c.company_id=e.company_id"""
        ).fetchall()
    for row in rows:
        payload = json.loads(row["payload"] or "{}")
        history[normalize_text(row["canonical_name"])].append(
            {
                "lead_event_id": row["lead_event_id"],
                "event": str(payload.get("event") or ""),
                "summary": str(payload.get("summary") or ""),
                "location": str(payload.get("location") or ""),
                "date_posted": str(payload.get("date_posted") or ""),
                "article_url": str(payload.get("article_url") or ""),
            }
        )

    company_names = {
        item.company_id: item.canonical_name for item in handoff.companies
    }
    blocked_ids: set[str] = set()
    events: list[LeadEventSync] = []
    for event in sorted(
        handoff.lead_events,
        key=lambda item: (item.date_posted, item.lead_event_id),
    ):
        company_key = normalize_text(company_names[event.company_id])
        current = event.model_dump(mode="json")
        duplicate = next(
            (
                prior
                for prior in history.get(company_key, [])
                if prior["lead_event_id"] != event.lead_event_id
                and _likely_duplicate_event(current, prior, company_key)
            ),
            None,
        )
        if duplicate is not None:
            reason = (
                "potential_cross_run_duplicate:"
                + str(duplicate["lead_event_id"])
            )
            reasons = list(
                dict.fromkeys([*event.crm_exclusion_reasons, reason])
            )
            event = event.model_copy(
                update={
                    "crm_eligible": False,
                    "crm_exclusion_reasons": reasons,
                }
            )
            blocked_ids.add(event.lead_event_id)
        else:
            history[company_key].append(current)
        events.append(event)

    sequences: list[OutreachSequenceSync] = []
    for sequence in handoff.sequences:
        if sequence.anchor_lead_event_id not in blocked_ids:
            sequences.append(sequence)
            continue
        reasons = list(
            dict.fromkeys(
                [*sequence.eligibility_reasons, "anchor_event_potential_duplicate"]
            )
        )
        sequences.append(
            sequence.model_copy(
                update={
                    "eligibility_status": EligibilityStatus.BLOCKED,
                    "eligibility_reasons": reasons,
                }
            )
        )
    value = handoff.model_copy(
        update={"lead_events": events, "sequences": sequences, "content_hash": "pending"}
    )
    return (
        value.model_copy(update={"content_hash": handoff_content_hash(value)}),
        len(blocked_ids),
    )


def _block_duplicate_primary_emails(
    handoff: SalesHandoff,
) -> tuple[SalesHandoff, int]:
    """Allow at most one READY sequence per normalized recipient mailbox."""
    companies = {item.company_id: item for item in handoff.companies}
    events = {item.lead_event_id: item for item in handoff.lead_events}
    recipients = {item.recipient_id: item for item in handoff.recipients}
    ready_by_email: dict[str, list[OutreachSequenceSync]] = defaultdict(list)
    for sequence in handoff.sequences:
        if sequence.eligibility_status != EligibilityStatus.READY:
            continue
        recipient = recipients[sequence.primary_recipient_id]
        ready_by_email[recipient.email.strip().casefold()].append(sequence)

    loser_to_winner: dict[str, str] = {}
    for sequences in ready_by_email.values():
        if len(sequences) < 2:
            continue

        def priority(sequence: OutreachSequenceSync) -> tuple:
            company = companies[sequence.company_id]
            recipient = recipients[sequence.primary_recipient_id]
            event = events[sequence.anchor_lead_event_id]
            email_domain = recipient.email.rsplit("@", 1)[-1].strip().casefold()
            company_domain = company.domain.strip().casefold()
            company_tokens = {
                token
                for token in re.findall(
                    r"[a-z0-9]+", normalize_text(company.canonical_name)
                )
                if len(token) > 2 and token not in _BUSINESS_GROUNDING_STOPWORDS
            }
            scope_tokens = set(
                re.findall(r"[a-z0-9]+", normalize_text(recipient.scope))
            )
            try:
                anchor_date = date.fromisoformat(event.date_posted).toordinal()
            except ValueError:
                anchor_date = date.min.toordinal()
            return (
                -int(bool(company_domain) and email_domain == company_domain),
                -len(company_tokens & scope_tokens),
                -anchor_date,
                -event.score,
                -recipient.role_score,
                sequence.sequence_id,
            )

        winner = min(sequences, key=priority)
        for sequence in sequences:
            if sequence.sequence_id != winner.sequence_id:
                loser_to_winner[sequence.sequence_id] = winner.sequence_id

    if not loser_to_winner:
        return handoff, 0

    updated_sequences: list[OutreachSequenceSync] = []
    for sequence in handoff.sequences:
        winner_id = loser_to_winner.get(sequence.sequence_id)
        if not winner_id:
            updated_sequences.append(sequence)
            continue
        reason = f"duplicate_primary_email:{winner_id}"
        updated_sequences.append(
            sequence.model_copy(
                update={
                    "eligibility_status": EligibilityStatus.BLOCKED,
                    "eligibility_reasons": list(
                        dict.fromkeys([*sequence.eligibility_reasons, reason])
                    ),
                }
            )
        )
    value = handoff.model_copy(
        update={"sequences": updated_sequences, "content_hash": "pending"}
    )
    return (
        value.model_copy(update={"content_hash": handoff_content_hash(value)}),
        len(loser_to_winner),
    )


def _align_handoff_to_existing_companies(
    handoff: SalesHandoff, sales_db: Path
) -> tuple[SalesHandoff, int, int]:
    """Reuse canonical sales company IDs and preserve one sequence per protocol."""
    with sqlite3.connect(sales_db) as conn:
        conn.row_factory = sqlite3.Row
        companies = conn.execute(
            "SELECT company_id, canonical_name, domain FROM sales_companies"
        ).fetchall()
        aliases = conn.execute(
            "SELECT alias_type, alias_value, company_id FROM sales_company_aliases"
        ).fetchall()
        existing_sequences = {
            (row["company_id"], row["campaign_protocol"])
            for row in conn.execute(
                "SELECT company_id, campaign_protocol FROM outreach_sequences"
            )
        }
    by_domain: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for row in companies:
        if str(row["domain"] or "").strip():
            by_domain[str(row["domain"]).strip().casefold()] = row["company_id"]
        by_name[normalize_text(row["canonical_name"])] = row["company_id"]
    for row in aliases:
        if row["alias_type"] == "domain":
            by_domain[str(row["alias_value"]).strip().casefold()] = row["company_id"]
        elif row["alias_type"] == "name":
            by_name[normalize_text(row["alias_value"])] = row["company_id"]

    id_map: dict[str, str] = {}
    current_by_domain: dict[str, str] = {}
    current_by_name: dict[str, str] = {}
    ordered_companies = sorted(
        handoff.companies,
        key=lambda item: (-int(bool(item.domain)), item.company_id),
    )
    for company in ordered_companies:
        supplied_names = [company.canonical_name, *company.aliases]
        existing_matches = {
            value
            for value in (
                by_domain.get(company.domain.strip().casefold()) if company.domain else None,
                *(
                    by_name.get(normalize_text(name))
                    for name in supplied_names
                    if normalize_text(name)
                ),
            )
            if value
        }
        if len(existing_matches) > 1:
            raise ValueError(
                f"existing sales identity conflict for {company.canonical_name}"
            )
        current_matches = {
            value
            for value in (
                (
                    current_by_domain.get(company.domain.strip().casefold())
                    if company.domain
                    else None
                ),
                *(
                    current_by_name.get(normalize_text(name))
                    for name in supplied_names
                    if normalize_text(name)
                ),
            )
            if value
        }
        if len(current_matches) > 1:
            raise ValueError(
                f"current handoff identity conflict for {company.canonical_name}"
            )
        existing_id = next(iter(existing_matches), "")
        current_id = next(iter(current_matches), "")
        if existing_id and current_id and existing_id != current_id:
            raise ValueError(
                f"current/existing identity conflict for {company.canonical_name}"
            )
        company_id = existing_id or current_id or company.company_id
        id_map[company.company_id] = company_id
        if company.domain:
            current_by_domain[company.domain.strip().casefold()] = company_id
        for name in supplied_names:
            normalized_name = normalize_text(name)
            if normalized_name:
                current_by_name[normalized_name] = company_id

    consolidated: dict[str, CompanySync] = {}
    for company in handoff.companies:
        company_id = id_map[company.company_id]
        prior = consolidated.get(company_id)
        legacy_ids = list(
            dict.fromkeys(
                [
                    *(prior.legacy_ids if prior else []),
                    *company.legacy_ids,
                    *([company.company_id] if company.company_id != company_id else []),
                ]
            )
        )
        aliases_value = list(
            dict.fromkeys([*(prior.aliases if prior else []), *company.aliases])
        )
        consolidated[company_id] = company.model_copy(
            update={
                "company_id": company_id,
                "aliases": aliases_value,
                "legacy_ids": legacy_ids,
            }
        )

    events = [
        item.model_copy(update={"company_id": id_map[item.company_id]})
        for item in handoff.lead_events
    ]
    recipient_id_map: dict[str, str] = {}
    recipients: list[RecipientSync] = []
    for recipient in handoff.recipients:
        company_id = id_map[recipient.company_id]
        recipient_id = stable_uuid("recipient", company_id, recipient.person_id)
        recipient_id_map[recipient.recipient_id] = recipient_id
        recipients.append(
            recipient.model_copy(
                update={"company_id": company_id, "recipient_id": recipient_id}
            )
        )

    sequences: list[OutreachSequenceSync] = []
    skipped = 0
    for sequence in handoff.sequences:
        company_id = id_map[sequence.company_id]
        if (company_id, sequence.campaign_protocol) in existing_sequences:
            skipped += 1
            continue
        sequences.append(
            sequence.model_copy(
                update={
                    "sequence_id": stable_uuid(
                        "outreach-sequence",
                        company_id,
                        sequence.campaign_protocol,
                    ),
                    "company_id": company_id,
                    "primary_recipient_id": recipient_id_map[
                        sequence.primary_recipient_id
                    ],
                }
            )
        )
    value = handoff.model_copy(
        update={
            "companies": list(consolidated.values()),
            "lead_events": events,
            "recipients": recipients,
            "sequences": sequences,
            "content_hash": "pending",
        }
    )
    return (
        value.model_copy(update={"content_hash": handoff_content_hash(value)}),
        sum(old != new for old, new in id_map.items()),
        skipped,
    )
def _likely_duplicate_event(current: dict, prior: dict, company_key: str) -> bool:
    current_url = canonicalize_url(str(current.get("article_url") or "")) if current.get("article_url") else ""
    prior_url = canonicalize_url(str(prior.get("article_url") or "")) if prior.get("article_url") else ""
    if current_url and current_url == prior_url:
        return True
    try:
        current_date = date.fromisoformat(str(current.get("date_posted") or ""))
        prior_date = date.fromisoformat(str(prior.get("date_posted") or ""))
    except ValueError:
        return False
    day_gap = abs((current_date - prior_date).days)
    if day_gap > 21:
        return False
    current_location = _duplicate_tokens(str(current.get("location") or ""), "")
    prior_location = _duplicate_tokens(str(prior.get("location") or ""), "")
    if current_location and prior_location and not (current_location & prior_location):
        return False
    current_event = normalize_text(str(current.get("event") or ""))
    prior_event = normalize_text(str(prior.get("event") or ""))
    current_tokens = _duplicate_tokens(current_event, company_key)
    prior_tokens = _duplicate_tokens(prior_event, company_key)
    shared = current_tokens & prior_tokens
    if len(shared) >= 2:
        return True
    if any(token.isdigit() for token in shared) and len(shared) >= 1:
        return True
    if current_event and prior_event and SequenceMatcher(
        None, current_event, prior_event
    ).ratio() >= 0.58:
        return True
    return day_gap <= 7 and "develop" in current_tokens and "develop" in prior_tokens


def _duplicate_tokens(value: str, company_key: str) -> set[str]:
    stop = {
        "a", "an", "and", "at", "for", "in", "is", "of", "on", "the", "to",
        "new", "may", "plans", "planned", "project", "arizona", "az",
    }
    company_tokens = set(re.findall(r"[a-z0-9]+", normalize_text(company_key)))
    aliases = {
        "approved": "approv", "approval": "approv", "approves": "approv",
        "build": "develop", "building": "develop", "built": "develop",
        "construction": "develop", "construct": "develop",
        "development": "develop", "develops": "develop", "developed": "develop",
        "expansion": "develop", "expand": "develop", "expands": "develop",
        "fab": "develop", "fabs": "develop",
        "condominium": "condo", "condominiums": "condo", "condos": "condo",
        "units": "unit",
    }
    output = set()
    for token in re.findall(r"[a-z0-9]+", normalize_text(value)):
        if token in stop or token in company_tokens or len(token) < 3:
            continue
        output.add(aliases.get(token, token.rstrip("s")))
    return output


def _bulk_contact_preference(contact: ContactCandidate) -> tuple[int, int, int, str]:
    return (
        int(not is_organization_mismatch(contact)),
        int(contact.provider.casefold() != "apollo"),
        len(contact.evidence),
        contact.contact_candidate_id,
    )


def _normalized_choice(value: object, allowed: set[str], default: str) -> str:
    normalized = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    return normalized if normalized in allowed else default


def _domain(value: str) -> str:
    raw = value.strip().casefold()
    if not raw:
        return ""
    try:
        return urlsplit(canonicalize_url(raw if "://" in raw else f"https://{raw}")).hostname or ""
    except ValueError:
        return ""


def _dedupe_profiles(profiles: list[CompanyProfile]) -> list[CompanyProfile]:
    groups: dict[str, list[CompanyProfile]] = defaultdict(list)
    for profile in profiles:
        groups[profile.domain or normalize_text(profile.canonical_name)].append(profile)
    output = []
    for identity, rows in groups.items():
        winner = sorted(
            rows,
            key=lambda item: (
                -{"valid": 2, "skip": 1, "review": 0}.get(
                    _profile_why_line(item).status, 0
                ),
                -bool(item.employee_count),
                -len(_profile_why_line(item).source_urls),
                item.profile_key,
            ),
        )[0]
        merged = winner.model_copy(
            update={
                "company_id": stable_uuid("bulk-company", identity),
                "aliases": list(dict.fromkeys(alias for row in rows for alias in [row.canonical_name, *row.aliases])),
                "locations": list(dict.fromkeys(value for row in rows for value in row.locations)),
                "organization_ids": sorted({value for row in rows for value in row.organization_ids}),
                "lead_event_ids": sorted({value for row in rows for value in row.lead_event_ids}),
                "evidence_urls": list(dict.fromkeys(value for row in rows for value in row.evidence_urls)),
            }
        )
        output.append(merged)
    return sorted(output, key=lambda item: item.company_id)


def _verify_seed_manifest(
    db_path: Path,
    run_id: str,
    *,
    overall_since: date | None = None,
    overall_until: date | None = None,
    archive_until: date | None = None,
) -> None:
    state = StateStore(db_path)
    with state.connect() as conn:
        row = conn.execute(
            "SELECT status, manifest_path, stamp, since_date FROM v2_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    if not row or row["status"] != StageStatus.COMPLETED.value:
        raise ValueError("seed run is not completed")
    seed_since = date.fromisoformat(row["since_date"])
    seed_until = date.fromisoformat(row["stamp"])
    if overall_since and seed_since < overall_since:
        raise ValueError("seed run begins before the bulk range")
    if overall_until and seed_until > overall_until:
        raise ValueError("seed run ends after the bulk range")
    if archive_until and seed_since <= archive_until:
        raise ValueError("seed run overlaps the archive discovery range")
    manifest_path = Path(row["manifest_path"])
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("status") != StageStatus.COMPLETED.value:
        raise ValueError("seed manifest is not completed")
    for artifact in payload.get("artifacts") or []:
        path = Path(str(artifact.get("path") or ""))
        expected = str(artifact.get("sha256") or "")
        if path.resolve() == manifest_path.resolve():
            continue
        if not path.exists() or not expected:
            raise ValueError(f"seed artifact missing: {path}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"seed artifact hash mismatch: {path}")


def _why_line_text(profile: CompanyProfile | None) -> str:
    why_line = _profile_why_line(profile)
    return why_line.text if why_line.status == "valid" else ""


def _why_line_sources(profile: CompanyProfile | None) -> str:
    why_line = _profile_why_line(profile)
    return " ".join(why_line.source_urls) if why_line.status in {"valid", "skip"} else ""


def _company_row(profile: CompanyProfile, run_id: str) -> dict:
    row = {
        "company_id": profile.company_id,
        "business_name": profile.canonical_name,
        "domain": profile.domain,
        "aliases": "; ".join(profile.aliases),
        "locations": "; ".join(profile.locations),
        "employee_count": profile.employee_count,
        "lead_event_ids": ",".join(profile.lead_event_ids),
        "lead_event_count": len(profile.lead_event_ids),
        "anchor_lead_event_id": profile.anchor_lead_event_id,
        "record_status": profile.record_status,
        "run_id": run_id,
        "provenance_json": json.dumps(
            {"organization_ids": profile.organization_ids, "evidence_urls": profile.evidence_urls},
            sort_keys=True,
        ),
    }
    why_line = _profile_why_line(profile)
    row["why_line"] = _why_line_text(profile)
    row["why_template_key"] = why_line.template_key
    row["why_confidence"] = why_line.confidence
    row["why_sources"] = _why_line_sources(profile)
    row["why_line_status"] = why_line.status
    return row


def _write_csv(path: Path, fields: list[str], rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_jsonl(path: Path, rows: Iterable[BaseModel | dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            value = row.model_dump(mode="json") if isinstance(row, BaseModel) else row
            file.write(json.dumps(value, sort_keys=True, default=str) + "\n")


def _default_model_call(model: str, prompt: str, tools: list[dict]) -> tuple[str, dict]:
    import llm

    return llm.call(model, prompt, tools=tools, text_format="json_object", with_usage=True)
