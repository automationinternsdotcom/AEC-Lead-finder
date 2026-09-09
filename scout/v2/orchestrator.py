"""In-process, resumable canonical Scout V2 pipeline."""
from __future__ import annotations

import json
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import logbook

from .apollo import ApolloFatalError, ApolloResolver, ApolloTransientError
from .artifacts import ArtifactStore, new_manifest
from .contracts import (
    ContactCandidate,
    DiscoveryCandidate,
    Evidence,
    RecordStatus,
    ReviewItem,
    RunManifest,
    StageStatus,
)
from .dedup import FuzzyEventDeduper, dedupe_candidates_exact
from .discovery import (
    CuratedSiteAdapter,
    DiscoveryBatch,
    FeedRegistry,
    load_curated_sources,
    parse_index,
    parse_datetime,
)
from .exports import ExportService
from .http import FetchResponse, HttpFetcher
from .ids import candidate_id, canonicalize_url, stable_hash, stable_uuid
from .providers import ApifyFacebookAdapter, NewsApiAdapter, ProviderAdapter, ProviderRecord
from .qualification import QualificationService
from .research import ContactResearchService, DecisionMakerService
from .scoring import ScoringService
from .state import SCHEMA_VERSION, StateStore
from .verification import ContactVerifier, select_best
from .outreach import CompanyOutreachService


ModelCall = Callable[[str, str, list[dict]], tuple[str, dict]]
PIPELINE_PROTOCOL_VERSION = "aether-aec-v2-run-v6"


@dataclass(slots=True)
class PipelineOptions:
    db_path: str
    results_dir: str
    sources_csv: str
    stamp: str
    since: str
    workers: int = 5
    max_articles: int = 0
    run_id: str = ""
    resume: bool = False
    retry_review: bool = False
    apollo_go: bool = False
    apollo_phones: bool = False
    phone_webhook: str = ""
    newsapi: bool = False
    apify: bool = False
    treg_go: bool = False
    treg_budget_usd: float = 5.0
    grok_model: str = "grok-4.3"
    extractor_model: str = "grok-4.3"


@dataclass(slots=True)
class PipelineResult:
    run_id: str
    manifest_path: str
    lead_count: int
    contact_count: int
    review_count: int
    paths: dict[str, str]


class PipelineRunner:
    STAGES = (
        "discover",
        "qualify",
        "dedup",
        "decision-makers",
        "contacts",
        "apollo",
        "score",
        "company-outreach",
        "export",
    )

    def __init__(
        self,
        options: PipelineOptions,
        *,
        fetch: Callable[[str], FetchResponse] | None = None,
        model_call: ModelCall | None = None,
        mx_lookup: Callable[[str], bool | None] | None = None,
        apollo_request: Callable[[str, dict], dict] | None = None,
        newsapi_adapter: ProviderAdapter | None = None,
        apify_adapter: ProviderAdapter | None = None,
    ):
        if options.resume and not options.run_id:
            raise ValueError("--resume requires --run-id")
        self.options = options
        self.run_id = options.run_id or str(uuid.uuid4())
        self.fetch = fetch or HttpFetcher()
        self.model_call = model_call
        self.mx_lookup = mx_lookup
        self.apollo_request = apollo_request
        self.newsapi_adapter = newsapi_adapter
        self.apify_adapter = apify_adapter
        self.state = StateStore(options.db_path)
        self.state.migrate()
        self.artifacts = ArtifactStore(
            options.results_dir, options.stamp, self.run_id, self.state
        )
        configuration = {
            "schema_version": SCHEMA_VERSION,
            "pipeline_protocol": PIPELINE_PROTOCOL_VERSION,
            "stamp": options.stamp,
            "since": options.since,
            "sources_sha256": stable_hash(
                Path(options.sources_csv).read_text(encoding="utf-8")
            ),
            "workers": options.workers,
            "max_articles": options.max_articles,
            "apollo_go": options.apollo_go,
            "apollo_phones": options.apollo_phones,
            "newsapi": options.newsapi,
            "apify": options.apify,
            "treg_go": options.treg_go,
            "treg_budget_usd": options.treg_budget_usd,
            "grok_model": options.grok_model,
            "extractor_model": options.extractor_model,
        }
        configuration["configuration_hash"] = stable_hash(
            json.dumps(configuration, sort_keys=True, separators=(",", ":"))
        )
        existing_run = self.state.get_run(self.run_id)
        if options.resume:
            if existing_run is None:
                raise ValueError(f"cannot resume unknown run: {self.run_id}")
            prior_configuration = existing_run["configuration"]
            if prior_configuration != configuration:
                raise ValueError(
                    "cannot resume run with a different or obsolete pipeline configuration"
                )
        elif existing_run is not None:
            raise ValueError(
                f"run ID already exists; use --resume with identical configuration: {self.run_id}"
            )
        self.state.create_run(
            self.run_id,
            options.stamp,
            options.since,
            configuration,
            str(self.artifacts.manifest_path),
        )
        if options.resume and self.artifacts.manifest_path.exists():
            self.manifest = self.artifacts.load_manifest()
            if self.manifest.configuration != configuration:
                raise ValueError("run manifest configuration does not match durable run state")
        else:
            self.manifest = new_manifest(
                self.run_id, options.stamp, options.since, configuration
            )
            self.artifacts.write_manifest(self.manifest)
        self.sources = load_curated_sources(options.sources_csv)
        self.candidates: list[DiscoveryCandidate] = []
        self.events = []
        self.people = []
        self.contacts: list[ContactCandidate] = []
        self.scores = []
        self.company_profiles = []
        self.outreach_recipients = []
        self.export_result: dict[str, object] = {}
        self._retry_from = self._retry_from_stage()

    def run(self) -> PipelineResult:
        self.manifest.status = StageStatus.RUNNING
        self.state.set_run_status(self.run_id, StageStatus.RUNNING)
        self.artifacts.write_manifest(self.manifest)
        try:
            self._stage("discover", "step 1/6: scout fetch (daily, AEC websites)", self._discover)
            self._stage("qualify", "step 1/6: qualify articles", self._qualify)
            self._stage("dedup", "step 1/6: deduplicate lead events", self._dedup)
            self._stage("decision-makers", "step 2/6: decision makers", self._decision_makers)
            self._stage("contacts", "step 3/6: contact enrichment", self._contacts)
            self._stage("apollo", "step 4/6: apollo fallback lookup", self._apollo)
            self._stage("score", "step 5/6: score leads", self._score)
            self._stage(
                "company-outreach",
                "step 6/7: consolidate companies and select outreach recipients",
                self._company_outreach,
            )
            self._stage("export", "step 6/6: build lead email", self._export)
        except Exception as exc:
            self.manifest.status = StageStatus.FAILED
            self.manifest.errors.append(
                {"type": type(exc).__name__, "message": str(exc)}
            )
            self._refresh_manifest()
            raise
        lead_count = int(self.export_result.get("lead_count", len(self.events)))
        if lead_count == 0:
            error = ZeroLeadError("0 leads found - failing the run")
            self.manifest.status = StageStatus.FAILED
            self.manifest.errors.append(
                {"type": type(error).__name__, "message": str(error)}
            )
            self._refresh_manifest()
            raise error
        # A successful retry supersedes run-level terminal errors from an earlier
        # failed attempt. Per-stage attempt history remains durable in SQLite.
        self.manifest.errors = []
        self.manifest.status = StageStatus.COMPLETED
        self._refresh_manifest()
        print(self.summary(), file=sys.stderr)
        return PipelineResult(
            run_id=self.run_id,
            manifest_path=str(self.artifacts.manifest_path),
            lead_count=lead_count,
            contact_count=int(self.export_result.get("contact_count", len(self.contacts))),
            review_count=int(
                self.export_result.get(
                    "review_count", len(self.state.reviews_for_run(self.run_id))
                )
            ),
            paths=dict(self.export_result.get("paths", {})),
        )

    def summary(self) -> str:
        top = sorted(self.scores, key=lambda item: -item.score)[:3]
        open_reviews = self.state.reviews_for_run(self.run_id, state="open")
        active_event_ids = {
            event.lead_event_id for event in self.state.active_events_for_run(self.run_id)
        }
        active_contacts = [
            item for item in self.contacts if item.lead_event_id in active_event_ids
        ]
        return (
            f"== summary {self.options.stamp} ==\n"
            f"leads: {len(self.events)} sales-ready "
            f"(+{len(open_reviews)} open review)\n"
            f"top scores: {', '.join(f'{item.lead_event_id} {item.score}' for item in top)}\n"
            f"decision makers: {len(self.people)}, contacts: "
            f"{sum(1 for item in active_contacts if item.selected)} selected\n"
            f"files: {self.export_result.get('paths', {})}"
        )

    def _stage(self, name: str, banner: str, function: Callable[[], dict]) -> None:
        completed = self.state.completed_stages(self.run_id)
        should_retry = (
            self.options.retry_review
            and self._retry_from is not None
            and self.STAGES.index(name) >= self.STAGES.index(self._retry_from)
            and name != "discover"
        )
        if self.options.resume and name in completed and not should_retry:
            self.artifacts.verify_stage(name)
            print(f"== {banner} [resume: already completed] ==", file=sys.stderr)
            self._hydrate(name)
            return
        print(f"== {banner} ==", file=sys.stderr)
        logbook.log(name, "started")
        self.state.set_stage_status(self.run_id, name, StageStatus.RUNNING)
        self.manifest.stages[name] = {"status": StageStatus.RUNNING.value}
        self.artifacts.write_manifest(self.manifest)
        try:
            counters = function()
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            self.state.set_stage_status(
                self.run_id, name, StageStatus.FAILED, error=error
            )
            self.manifest.stages[name] = {
                "status": StageStatus.FAILED.value,
                "error": error,
            }
            self.artifacts.write_manifest(self.manifest)
            logbook.log(name, f"failed: {error}")
            raise
        self.state.set_stage_status(
            self.run_id, name, StageStatus.COMPLETED, counters=counters
        )
        self.manifest.stages[name] = {
            "status": StageStatus.COMPLETED.value,
            "counters": counters,
        }
        self.manifest.counts.update(
            {f"{name}.{key}": int(value) for key, value in counters.items()}
        )
        self._refresh_manifest()
        logbook.log(name, f"completed: {counters}")

    def _discover(self) -> dict:
        known_before = self.state.candidate_ids()
        terminal_before = self.state.completed_qualification_candidate_ids(
            since_date=self.options.since,
            stamp=self.options.stamp,
        )
        curated = CuratedSiteAdapter(
            self.sources,
            self.state,
            self.artifacts,
            fetch=self.fetch,
            workers=self.options.workers,
        ).discover(
            self.run_id,
            date.fromisoformat(self.options.since),
            max_candidates=0,
            until=date.fromisoformat(self.options.stamp),
        )
        batches = [curated, self._discover_rss()]
        if self.options.newsapi:
            adapter = self.newsapi_adapter or NewsApiAdapter()
            batches.append(self._discover_provider(adapter))
        if self.options.apify:
            adapter = self.apify_adapter or ApifyFacebookAdapter()
            batches.append(self._discover_provider(adapter))
        all_candidates = [
            candidate for batch in batches for candidate in batch.candidates
        ]
        new_candidates = [
            candidate
            for candidate in all_candidates
            if candidate.candidate_id not in terminal_before
        ]
        exact = dedupe_candidates_exact(new_candidates)
        if self.options.max_articles > 0:
            selected, deferred = (
                exact[: self.options.max_articles],
                exact[self.options.max_articles :],
            )
        else:
            selected, deferred = exact, []
        self.candidates = []
        for candidate in selected:
            selected_candidate = candidate.model_copy(
                update={
                    "metadata": {
                        **candidate.metadata,
                        "selected_for_qualification": True,
                    }
                }
            )
            self.state.save_candidate(selected_candidate)
            self.candidates.append(selected_candidate)
        for candidate in deferred:
            deferred_candidate = candidate.model_copy(
                update={
                    "record_status": RecordStatus.REVIEW,
                    "validation_errors": [
                        *candidate.validation_errors,
                        "deferred_by_max_articles",
                    ],
                    "metadata": {
                        **candidate.metadata,
                        "selected_for_qualification": False,
                    },
                }
            )
            self.state.save_candidate(deferred_candidate)
            self.state.add_review(
                ReviewItem(
                    review_id=stable_uuid(
                        "review", self.run_id, "discover", candidate.candidate_id, "spend-cap"
                    ),
                    run_id=self.run_id,
                    stage="discover",
                    record_type="discovery_candidate",
                    record_id=candidate.candidate_id,
                    reason_code="deferred_by_max_articles",
                    validation_errors=["deferred_by_max_articles"],
                )
            )
        return {
            "sources": len(self.sources),
            "discovered": len(all_candidates),
            "novel": sum(
                candidate.candidate_id not in known_before
                for candidate in all_candidates
            ),
            "previously_seen": sum(
                candidate.candidate_id in known_before
                for candidate in all_candidates
            ),
            "qualification_eligible": len(new_candidates),
            "selected": len(selected),
            "deferred": len(deferred),
            "discovery_reviews_created": sum(
                len(batch.reviews) for batch in batches
            ),
            "source_errors": sum(len(batch.source_errors) for batch in batches),
        }

    def _discover_rss(self) -> DiscoveryBatch:
        registry = FeedRegistry(self.state, self.artifacts, fetch=self.fetch)
        batch = DiscoveryBatch()
        with ThreadPoolExecutor(max_workers=self.options.workers) as pool:
            discovered = list(
                pool.map(
                    lambda source: self._rss_targets_for_source(registry, source),
                    self.sources,
                )
            )
        unique_targets = {}
        for targets, errors in discovered:
            batch.source_errors.extend(errors)
            for source, url, method in targets:
                unique_targets.setdefault(url, (source, url, method))

        def validate(target):
            source, url, method = target
            status, entries = registry.validate_and_store(source, url, method)
            if status.value != "active":
                return DiscoveryBatch()
            return registry.candidates(
                self.run_id,
                source,
                entries,
                date.fromisoformat(self.options.since),
                until=date.fromisoformat(self.options.stamp),
            )

        with ThreadPoolExecutor(max_workers=self.options.workers) as pool:
            feed_batches = list(pool.map(validate, unique_targets.values()))
        for feed_batch in feed_batches:
            batch.candidates.extend(feed_batch.candidates)
            batch.reviews.extend(feed_batch.reviews)
        return batch

    def _rss_targets_for_source(self, registry: FeedRegistry, source):
        existing = [
            row
            for row in self.state.feeds(("active", "degraded", "pending"))
            if row["source_id"] == source.source_id
        ]
        if existing:
            return (
                [
                    (source, row["url"], row["discovery_method"])
                    for row in existing
                ],
                [],
            )
        try:
            index = self.fetch(source.url)
            parsed = parse_index(index.text, source.url)
            alternates = set(parsed.feed_links)
            discovered = registry.discover_for_source(source, index.text)
            return (
                [
                    (
                        source,
                        url,
                        "autodiscovery" if url in alternates else "conventional_path",
                    )
                    for url in discovered
                ],
                [],
            )
        except Exception as exc:
            return (
                [],
                [
                    {
                        "source_id": source.source_id,
                        "stage": "rss_discovery",
                        "error": repr(exc),
                    }
                ],
            )

    def _discover_provider(self, adapter: ProviderAdapter) -> DiscoveryBatch:
        adapter.preflight()
        records = adapter.discover(
            date.fromisoformat(self.options.since), date.fromisoformat(self.options.stamp)
        )
        raw = self.artifacts.write_raw(
            "discover-provider",
            f"{adapter.name}-{self.run_id}.json",
            [record.raw or asdict(record) for record in records],
        )
        batch = DiscoveryBatch()
        for record in records:
            candidate = self._provider_candidate(record, raw)
            self.state.save_candidate(candidate)
            batch.candidates.append(candidate)
            if candidate.record_status == RecordStatus.REVIEW:
                review = ReviewItem(
                    review_id=stable_uuid(
                        "review", self.run_id, "discover", candidate.candidate_id
                    ),
                    run_id=self.run_id,
                    stage="discover",
                    record_type="discovery_candidate",
                    record_id=candidate.candidate_id,
                    reason_code="provider_candidate_incomplete",
                    validation_errors=candidate.validation_errors,
                    raw_artifact_path=raw["path"],
                )
                self.state.add_review(review)
                batch.reviews.append(review)
        return batch

    def _provider_candidate(self, record: ProviderRecord, raw: dict) -> DiscoveryCandidate:
        canonical = canonicalize_url(record.url)
        text = f"{record.title} {json.dumps(record.raw or {})}".casefold()
        geography = any(
            value in text
            for value in ("arizona", "phoenix", "tucson", "mesa", "tempe", "scottsdale", "chandler", "gilbert")
        )
        signal = any(
            value in text
            for value in ("open", "lease", "tenant", "occup", "construction", "redevelop", "management", "expansion", "facility", "property")
        )
        published = parse_datetime(record.published_at)
        errors = []
        status = RecordStatus.VALID
        if not geography or not signal:
            status = RecordStatus.REJECTED
            errors.append("deterministic_aec_prescreen_failed")
        elif published is None:
            status = RecordStatus.REVIEW
            errors.append("publication_date_missing")
        domain = (urlsplit(canonical).hostname or "").casefold()
        source_id = stable_uuid("source", record.provider, domain)
        self.state.upsert_source(
            source_id,
            record.source_name or record.provider,
            f"{urlsplit(canonical).scheme}://{urlsplit(canonical).netloc}/",
            domain,
        )
        return DiscoveryCandidate(
            candidate_id=candidate_id(record.provider, record.provider_id, canonical),
            run_id=self.run_id,
            provider=record.provider,
            provider_id=record.provider_id,
            discovered_url=canonical,
            resolved_url=canonical,
            canonical_url=canonical,
            title=record.title,
            source_id=source_id,
            source_name=record.source_name or record.provider,
            source_domain=domain,
            published_at=published,
            raw_artifact_path=raw["path"],
            raw_artifact_hash=raw["sha256"],
            record_status=status,
            validation_errors=errors,
            metadata={"provider_record_hash": stable_hash(json.dumps(record.raw or {}, sort_keys=True))},
        )

    def _qualify(self) -> dict:
        candidates = self.candidates
        retried_reviews = []
        if self.options.retry_review:
            retried_reviews = [
                *[
                    item
                    for item in self.state.eligible_reviews(
                        "discover", run_id=self.run_id
                    )
                    if item.reason_code == "deferred_by_max_articles"
                ],
                *self.state.eligible_reviews("qualify", run_id=self.run_id),
            ]
            review_ids = {
                item.record_id
                for item in retried_reviews
                if item.record_type == "discovery_candidate"
            }
            candidates = [
                item
                for item in candidates
                if item.record_status == RecordStatus.VALID
                or item.candidate_id in review_ids
            ]
            candidates = list(
                {item.candidate_id: item for item in [*candidates, *self.state.candidates_by_ids(review_ids)]}.values()
            )
            for review in retried_reviews:
                self.state.mark_review_retried(review.review_id)
        service = QualificationService(
            self.state,
            self.artifacts,
            self.options.grok_model,
            call_model=self.model_call,
            workers=self.options.workers,
            window_start=date.fromisoformat(self.options.since),
            window_end=date.fromisoformat(self.options.stamp),
        )
        result = service.qualify(candidates, retry_review=self.options.retry_review)
        still_open_records = {item.record_id for item in result.reviews}
        for review in retried_reviews:
            if review.record_id not in still_open_records:
                self.state.resolve_review(review.review_id)
        self.events = self.state.active_events_for_run(self.run_id)
        return {
            "qualified": len(result.events),
            "rejected": len(result.rejected_candidate_ids),
            "reviews": len(result.reviews),
        }

    def _dedup(self) -> dict:
        self.events = self.state.active_events_for_run(self.run_id)
        service = FuzzyEventDeduper(
            self.state,
            self.artifacts,
            self.options.extractor_model,
            call_model=self.model_call,
        )
        self.events, reviews = service.dedupe(self.events)
        return {"events": len(self.events), "reviews": len(reviews)}

    def _decision_makers(self) -> dict:
        organizations = self.state.organizations(
            {event.organization_id for event in self.events}
        )
        verifier = ContactVerifier(self.state, mx_lookup=self.mx_lookup)
        service = DecisionMakerService(
            self.state,
            self.artifacts,
            self.options.grok_model,
            call_model=self.model_call,
            verifier=verifier,
            events=self.events,
        )
        self.people, reviews = service.research(organizations, attempts=2)
        return {"organizations": len(organizations), "people": len(self.people), "reviews": len(reviews)}

    def _contacts(self) -> dict:
        organizations = self.state.organizations(
            {event.organization_id for event in self.events}
        )
        organization_ids = {item.organization_id for item in organizations}
        self.people = [
            person
            for person in self.state.people()
            if person.organization_id in organization_ids
        ]
        verifier = ContactVerifier(self.state, mx_lookup=self.mx_lookup)
        service = ContactResearchService(
            self.state,
            self.artifacts,
            self.options.grok_model,
            verifier,
            call_model=self.model_call,
        )
        self.contacts, reviews = service.research(
            self.people, organizations, self.events, attempts=2
        )
        if self.options.treg_go:
            from .treg import TregClient, recover_state
            client = TregClient(Path(self.options.db_path).parent / f"treg-cache-{self.options.stamp[:7]}.sqlite",
                                budget_usd=self.options.treg_budget_usd)
            try:
                self.people, self.contacts = recover_state(
                    self.state, self.artifacts, organizations, self.people,
                    self.events, self.contacts, client=client)
            finally:
                client.close()
        return {
            "people": len(self.people),
            "candidates": len(self.contacts),
            "selected": sum(1 for item in self.contacts if item.selected),
            "reviews": len(reviews),
        }

    def _apollo(self) -> dict:
        organizations = {
            item.organization_id: item
            for item in self.state.organizations(
                {event.organization_id for event in self.events}
            )
        }
        organization_ids = set(organizations)
        self.people = [
            person
            for person in self.state.people()
            if person.organization_id in organization_ids
        ]
        active_event_ids = {event.lead_event_id for event in self.events}
        self.contacts = [
            item
            for item in self.state.contacts_for_run(self.run_id)
            if item.lead_event_id in active_event_ids
        ]
        reachable = {
            item.person_id
            for item in self.contacts
            if item.selected
            and item.verification_status.value != "rejected"
            and any((item.email, item.phone))
        }
        events_by_org: dict[str, list] = {}
        for event in self.events:
            events_by_org.setdefault(event.organization_id, []).append(event)
        resolver = ApolloResolver(
            self.state,
            request_match=self.apollo_request,
        )
        verifier = ContactVerifier(self.state, mx_lookup=self.mx_lookup)
        created: list[ContactCandidate] = []
        reviews = 0
        billed = 0
        for person in self.people:
            if person.person_id in reachable:
                continue
            organization = organizations.get(person.organization_id)
            if not organization:
                continue
            attempt_number = self.state.next_provider_attempt_number(
                self.run_id, "apollo", "person", person.person_id
            )
            attempt_id = stable_uuid(
                "attempt", self.run_id, "apollo", person.person_id, attempt_number
            )
            try:
                found = resolver.resolve(
                    person.name,
                    organization.canonical_name,
                    spend=self.options.apollo_go,
                    reveal_phone=self.options.apollo_phones,
                    phone_webhook=self.options.phone_webhook,
                )
                if found.status == "fatal":
                    raise ApolloFatalError(found.error or "cached fatal Apollo result")
            except ApolloTransientError as exc:
                review = ReviewItem(
                    review_id=stable_uuid("review", self.run_id, "apollo", person.person_id),
                    run_id=self.run_id,
                    stage="apollo",
                    record_type="person",
                    record_id=person.person_id,
                    reason_code="apollo_transient_failure",
                    validation_errors=[str(exc)],
                )
                self.state.add_review(review)
                self.state.record_provider_attempt(
                    attempt_id=attempt_id,
                    run_id=self.run_id,
                    stage="apollo",
                    provider="apollo",
                    target_type="person",
                    target_id=person.person_id,
                    status="review",
                    error={"type": type(exc).__name__, "message": str(exc)},
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                reviews += 1
                continue
            response = self.artifacts.write_raw(
                "apollo", f"{attempt_id}-response.json", asdict(found)
            )
            self.state.record_provider_attempt(
                attempt_id=attempt_id,
                run_id=self.run_id,
                stage="apollo",
                provider="apollo",
                target_type="person",
                target_id=person.person_id,
                status=found.status,
                billable=found.billable and not found.cached,
                response_artifact_path=response["path"],
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            billed += int(found.billable and not found.cached)
            if found.status != "found":
                continue
            verification = verifier.verify(
                email=found.email,
                phone=found.phone,
                linkedin=found.linkedin,
                organization_domain=organization.domain,
            )
            evidence = [
                Evidence(
                    url="https://app.apollo.io/",
                    supports=f"Apollo match for {person.name} at {organization.canonical_name}.",
                    provider="apollo",
                )
            ]
            for event in events_by_org.get(person.organization_id, []):
                created.append(
                    ContactCandidate(
                        contact_candidate_id=stable_uuid(
                            "contact-candidate", event.lead_event_id, person.person_id, "apollo"
                        ),
                        run_id=self.run_id,
                        lead_event_id=event.lead_event_id,
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
                        evidence=evidence,
                    )
                )
        self.contacts = select_best([*self.contacts, *created])
        for contact in self.contacts:
            self.state.save_contact(contact)
        return {"created": len(created), "billed": billed, "reviews": reviews}

    def _score(self) -> dict:
        self.events = self.state.active_events_for_run(self.run_id)
        active_event_ids = {event.lead_event_id for event in self.events}
        self.contacts = [
            item
            for item in self.state.contacts_for_run(self.run_id)
            if item.lead_event_id in active_event_ids
        ]
        service = ScoringService(
            self.state,
            self.artifacts,
            self.options.grok_model,
            call_model=self.model_call,
        )
        self.scores, reviews = service.score(self.events, self.contacts, attempts=2)
        return {"scored": len(self.scores), "reviews": len(reviews)}

    def _company_outreach(self) -> dict:
        self.events = self.state.active_events_for_run(self.run_id)
        organizations = self.state.organizations(
            {event.organization_id for event in self.events}
        )
        organization_ids = {event.organization_id for event in self.events}
        self.people = [
            person
            for person in self.state.people()
            if person.organization_id in organization_ids
        ]
        active_event_ids = {event.lead_event_id for event in self.events}
        self.contacts = [
            item
            for item in self.state.contacts_for_run(self.run_id)
            if item.lead_event_id in active_event_ids
        ]
        self.scores = [
            item
            for item in self.state.scores_for_run(self.run_id)
            if item.lead_event_id in active_event_ids
        ]
        service = CompanyOutreachService(
            self.state,
            self.artifacts,
            self.options.grok_model,
            call_model=self.model_call,
        )
        self.company_profiles, self.outreach_recipients, reviews = service.build(
            self.events,
            organizations,
            self.people,
            self.contacts,
            self.scores,
        )
        self.events = self.state.active_events_for_run(self.run_id)
        return {
            "companies": len(self.company_profiles),
            "valid_why_lines": sum(
                item.why_line_status == "valid" for item in self.company_profiles
            ),
            "recipients": len(self.outreach_recipients),
            "primaries_ready": sum(
                item.primary and item.eligibility_status == "ready"
                for item in self.outreach_recipients
            ),
            "reviews": len(reviews),
        }

    def _export(self) -> dict:
        self.export_result = ExportService(
            self.state,
            self.artifacts,
            self.options.results_dir,
            self.options.stamp,
        ).export()
        return {
            "leads": int(self.export_result["lead_count"]),
            "contacts": int(self.export_result["contact_count"]),
            "reviews": int(self.export_result["review_count"]),
        }

    def _hydrate(self, stage: str) -> None:
        if stage == "discover":
            retry_ids = {
                item.record_id
                for item in self.state.eligible_reviews(
                    "discover", run_id=self.run_id
                )
                if item.record_type == "discovery_candidate"
                and item.reason_code == "deferred_by_max_articles"
            } if self.options.retry_review else set()
            self.candidates = [
                item
                for item in self.state.candidates_for_run(self.run_id)
                if item.metadata.get("selected_for_qualification")
                or item.candidate_id in retry_ids
            ]
        elif stage in {"qualify", "dedup"}:
            self.events = self.state.active_events_for_run(self.run_id)
        elif stage == "decision-makers":
            organization_ids = {event.organization_id for event in self.events}
            self.people = [
                person
                for person in self.state.people()
                if person.organization_id in organization_ids
            ]
        elif stage in {"contacts", "apollo"}:
            self.contacts = self.state.contacts_for_run(self.run_id)
        elif stage == "score":
            self.scores = self.state.scores_for_run(self.run_id)
        elif stage == "company-outreach":
            self.company_profiles = self.state.company_profiles_for_run(self.run_id)
            self.outreach_recipients = self.state.outreach_recipients_for_run(self.run_id)
        elif stage == "export":
            day = Path(self.options.results_dir) / self.options.stamp
            active_event_ids = {
                event.lead_event_id
                for event in self.state.active_events_for_run(self.run_id)
            }
            selected_contacts = [
                item
                for item in self.state.contacts_for_run(self.run_id)
                if item.lead_event_id in active_event_ids
                and item.selected
                and item.verification_status.value != "rejected"
            ]
            self.export_result = {
                "lead_count": len(self.state.active_events_for_run(self.run_id)),
                "contact_count": len(selected_contacts),
                "review_count": len(
                    self.state.reviews_for_run(self.run_id, state="open")
                ),
                "paths": {
                    "raw_leads": str(day / "raw_leads.csv"),
                    "contacts": str(day / "contacts.csv"),
                    "uncertain_leads": str(day / "uncertain_leads.csv"),
                    "html": str(day / "leads_email.html"),
                    "sales_handoff": str(self.artifacts.final_dir / "sales_handoff.json"),
                },
            }

    def _refresh_manifest(self) -> None:
        events = self.state.active_events_for_run(self.run_id)
        active_event_ids = {event.lead_event_id for event in events}
        organization_ids = {event.organization_id for event in events}
        contacts = [
            item
            for item in self.state.contacts_for_run(self.run_id)
            if item.lead_event_id in active_event_ids
        ]
        reviews = self.state.reviews_for_run(self.run_id)
        self.manifest.counts = {
            "lead_events": len(events),
            "relevant_people": sum(
                person.organization_id in organization_ids
                for person in self.state.people()
            ),
            "contact_candidates": len(contacts),
            "selected_contacts": sum(
                item.selected and item.verification_status.value != "rejected"
                for item in contacts
            ),
            "open_reviews": sum(item.state == "open" for item in reviews),
            "resolved_reviews": sum(item.state == "resolved" for item in reviews),
        }
        self.manifest.usage = self.state.usage_summary(self.run_id)
        self.manifest.artifacts = [
            artifact
            for artifact in self.state.artifacts_for_run(self.run_id)
            if artifact["stage"] != "manifest"
        ]
        self.artifacts.write_manifest(self.manifest)

    def _retry_from_stage(self) -> str | None:
        if not (self.options.resume and self.options.retry_review):
            return None
        stage_alias = {"discover": "qualify", "why-lines": "company-outreach"}
        candidates = []
        for review in self.state.eligible_reviews(run_id=self.run_id):
            if (
                review.stage == "discover"
                and review.reason_code != "deferred_by_max_articles"
            ):
                continue
            stage = stage_alias.get(review.stage, review.stage)
            if stage in self.STAGES:
                candidates.append(stage)
        return min(candidates, key=self.STAGES.index) if candidates else None


class ZeroLeadError(RuntimeError):
    pass
