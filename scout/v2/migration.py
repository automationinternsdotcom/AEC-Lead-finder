"""Idempotent local import of legacy Scout URL history and dated CSV artifacts."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .artifacts import ArtifactStore, new_manifest
from .contracts import (
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
from .ids import (
    candidate_id,
    canonicalize_url,
    event_id,
    normalize_text,
    organization_id,
    person_id,
    stable_hash,
    stable_uuid,
)
from .state import SCHEMA_VERSION, StateStore


DAY_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")
URL_RE = re.compile(r"https?://[^\s,;]+")


@dataclass(slots=True)
class MigrationReport:
    applied: bool
    database_path: str
    backup_path: str = ""
    schema_version: int = SCHEMA_VERSION
    input_files: list[dict] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    warnings: list[str] = field(default_factory=list)
    synthetic_runs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "applied": self.applied,
            "database_path": self.database_path,
            "backup_path": self.backup_path,
            "schema_version": self.schema_version,
            "input_files": self.input_files,
            "counts": dict(self.counts),
            "warnings": self.warnings,
            "synthetic_runs": self.synthetic_runs,
        }


class LegacyMigrator:
    def __init__(self, db_path: str | Path, results_dir: str | Path):
        self.db_path = Path(db_path)
        self.results_dir = Path(results_dir)

    def inventory(self) -> MigrationReport:
        report = MigrationReport(applied=False, database_path=str(self.db_path))
        if self.db_path.exists():
            report.input_files.append(_file_info(self.db_path))
        for day_dir in self._day_dirs():
            for name in ("raw_leads.csv", "uncertain_leads.csv", "contacts.csv"):
                path = day_dir / name
                if path.exists():
                    report.input_files.append(_file_info(path))
        return report

    def apply(self) -> MigrationReport:
        report = self.inventory()
        report.applied = True
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = self.db_path.with_name(f"{self.db_path.name}.backup-{timestamp}")
        shadow_path = self.db_path.with_name(f".{self.db_path.name}.v2-migration-{timestamp}.tmp")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.db_path.exists():
                _sqlite_backup(self.db_path, backup_path)
                _sqlite_backup(self.db_path, shadow_path)
                report.backup_path = str(backup_path)
            else:
                sqlite3.connect(shadow_path).close()
            store = StateStore(shadow_path)
            store.migrate()
            self._import_legacy_history(store, report)
            for day_dir in self._day_dirs():
                self._import_day(store, day_dir, report)
            _checkpoint_for_replace(shadow_path)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(self.db_path) + suffix)
                if sidecar.exists() and report.backup_path:
                    os.replace(sidecar, Path(report.backup_path + suffix))
            os.replace(shadow_path, self.db_path)
        except Exception:
            for path in (shadow_path, Path(str(shadow_path) + "-wal"), Path(str(shadow_path) + "-shm")):
                path.unlink(missing_ok=True)
            raise
        report_path = self.results_dir / f"migration-v2-{timestamp}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(report_path, report.as_dict())
        return report

    def _import_legacy_history(self, store: StateStore, report: MigrationReport) -> None:
        run_id = stable_uuid("legacy-run", "url-history")
        store.create_run(
            run_id,
            "1970-01-01",
            "1970-01-01",
            {"migration": True, "source": "legacy-scout-tables"},
        )
        report.synthetic_runs.append(run_id)
        with store.connect() as conn:
            tables = {
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            accepted = list(conn.execute("SELECT * FROM articles")) if "articles" in tables else []
            rejected = list(conn.execute("SELECT * FROM rejected")) if "rejected" in tables else []
        for row in accepted:
            self._history_candidate(store, run_id, dict(row), RecordStatus.VALID, report)
        for row in rejected:
            payload = dict(row)
            link = payload.get("link") or payload.get("url")
            self._history_candidate(
                store, run_id, {"link": link}, RecordStatus.REJECTED, report
            )
        store.set_run_status(run_id, StageStatus.COMPLETED)

    def _history_candidate(
        self,
        store: StateStore,
        run_id: str,
        row: dict,
        status: RecordStatus,
        report: MigrationReport,
    ) -> None:
        link = str(row.get("link") or "").strip()
        try:
            canonical = canonicalize_url(link)
        except ValueError:
            if link:
                report.warnings.append(f"invalid legacy URL skipped: {link}")
            return
        source_id, source_name, domain = _ensure_source(
            store, str(row.get("source_site") or ""), canonical
        )
        published = _date_time(row.get("date_posted"))
        candidate = DiscoveryCandidate(
            candidate_id=candidate_id("legacy", "", canonical),
            run_id=run_id,
            provider="legacy",
            discovered_url=canonical,
            resolved_url=canonical,
            canonical_url=canonical,
            title=str(row.get("business_name") or row.get("event") or ""),
            source_id=source_id,
            source_name=source_name,
            source_domain=domain,
            published_at=published,
            record_status=status,
            metadata={"inferred_legacy_identity": True},
        )
        store.save_candidate(candidate)
        report.counts[f"history_{status.value}"] += 1

    def _import_day(self, store: StateStore, day_dir: Path, report: MigrationReport) -> None:
        day = day_dir.name
        run_id = stable_uuid("legacy-run", day)
        manifest_path = day_dir / "runs" / run_id / "manifest.json"
        store.create_run(
            run_id,
            day,
            day,
            {"migration": True, "source": "dated-csv", "inferred_identifiers": True},
            str(manifest_path),
        )
        report.synthetic_runs.append(run_id)
        artifacts = ArtifactStore(self.results_dir, day, run_id, store)
        raw_path = day_dir / "raw_leads.csv"
        uncertain_path = day_dir / "uncertain_leads.csv"
        contacts_path = day_dir / "contacts.csv"
        events_by_business: dict[str, list[LeadEvent]] = defaultdict(list)
        orgs: dict[str, Organization] = {}
        if raw_path.exists():
            for row in _read_csv(raw_path):
                imported = self._import_lead_row(
                    store, run_id, day, raw_path, row, report
                )
                if imported:
                    event, organization = imported
                    events_by_business[normalize_text(organization.canonical_name)].append(event)
                    orgs[organization.organization_id] = organization
        if uncertain_path.exists():
            for row in _read_csv(uncertain_path):
                self._import_uncertain_row(
                    store, run_id, day, uncertain_path, row, report
                )
        if contacts_path.exists():
            for row in _read_csv(contacts_path):
                self._import_contact_row(
                    store,
                    run_id,
                    contacts_path,
                    row,
                    events_by_business,
                    orgs,
                    report,
                )
        manifest = new_manifest(
            run_id,
            day,
            day,
            {"migration": True, "source": "dated-csv", "schema_version": SCHEMA_VERSION},
        )
        manifest.status = StageStatus.COMPLETED
        manifest.counts = {
            "lead_events": len(store.events_for_run(run_id)),
            "contacts": len(store.contacts_for_run(run_id)),
            "reviews": len(store.reviews_for_run(run_id)),
        }
        manifest.artifacts = [
            _file_info(path)
            for path in (raw_path, uncertain_path, contacts_path)
            if path.exists()
        ]
        artifacts.write_manifest(manifest)

    def _import_lead_row(
        self,
        store: StateStore,
        run_id: str,
        day: str,
        source_path: Path,
        row: dict,
        report: MigrationReport,
    ) -> tuple[LeadEvent, Organization] | None:
        link = str(row.get("link") or "").strip()
        try:
            canonical = canonicalize_url(link)
        except ValueError:
            report.warnings.append(f"{source_path}: lead row missing valid link")
            return None
        source_id, source_name, domain = _ensure_source(
            store, str(row.get("source_site") or ""), canonical
        )
        artifact_hash = _sha256(source_path)
        candidate = DiscoveryCandidate(
            candidate_id=candidate_id("legacy", "", canonical),
            run_id=run_id,
            provider="legacy",
            discovered_url=canonical,
            resolved_url=canonical,
            canonical_url=canonical,
            title=str(row.get("business_name") or row.get("event") or ""),
            source_id=source_id,
            source_name=source_name,
            source_domain=domain,
            published_at=_date_time(row.get("date_posted")),
            raw_artifact_path=str(source_path),
            raw_artifact_hash=artifact_hash,
            metadata={"inferred_legacy_identity": True},
        )
        store.save_candidate(candidate)
        name = str(row.get("business_name") or "Legacy unknown organization").strip()
        location = str(row.get("location") or "Arizona").strip()
        oid = organization_id(name, "", location)
        organization = Organization(
            organization_id=oid,
            canonical_name=name,
            location=location,
            aliases=[item.strip() for item in str(row.get("aka") or "").split(",") if item.strip()],
            employee_count=_employee_count(row.get("Employee_Count") or row.get("employee_count")),
            evidence=[Evidence(url=canonical, supports="Imported legacy lead source.", provider="legacy")],
            inferred_identity=True,
        )
        store.save_organization(organization)
        event_text = str(row.get("event") or f"Legacy lead for {name}").strip()
        eid = event_id(oid, event_text, location, row.get("date_posted") or day)
        priority = str(row.get("priority") or "medium").casefold()
        if priority not in {"high", "medium", "low"}:
            priority = "medium"
        event = LeadEvent(
            lead_event_id=eid,
            run_id=run_id,
            organization_id=oid,
            source_provider=candidate.provider,
            primary_candidate_id=candidate.candidate_id,
            supporting_candidate_ids=[candidate.candidate_id],
            event=event_text,
            location=location,
            date_posted=_date_value(row.get("date_posted")),
            summary=str(row.get("summary") or ""),
            priority=priority,
            property_type=str(row.get("property_type") or "other"),
            service_angle=str(row.get("service_angle") or ""),
            filter_reason=str(row.get("filter_reason") or "Imported legacy lead."),
            evidence=organization.evidence,
        )
        store.save_lead_event(event)
        for name_value, title in _decision_makers(row):
            person = Person(
                person_id=person_id(name_value, oid),
                organization_id=oid,
                name=name_value,
                title=title,
                evidence=_row_evidence(row, canonical, "Imported legacy decision maker."),
                inferred_identity=True,
            )
            store.save_person(person)
        score = _integer_score(row.get("score"))
        if score is not None:
            store.save_score(
                LeadScore(
                    run_id=run_id,
                    lead_event_id=eid,
                    score=score,
                    model="legacy-import",
                    attempt_id=stable_uuid("legacy-score", run_id, eid),
                )
            )
        report.counts["lead_events"] += 1
        return event, organization

    def _import_uncertain_row(
        self,
        store: StateStore,
        run_id: str,
        day: str,
        source_path: Path,
        row: dict,
        report: MigrationReport,
    ) -> None:
        link = str(row.get("link") or "").strip()
        try:
            canonical = canonicalize_url(link)
        except ValueError:
            report.warnings.append(f"{source_path}: uncertain row missing valid link")
            return
        source_id, source_name, domain = _ensure_source(
            store, str(row.get("source_site") or ""), canonical
        )
        candidate = DiscoveryCandidate(
            candidate_id=candidate_id("legacy", "", canonical),
            run_id=run_id,
            provider="legacy",
            discovered_url=canonical,
            resolved_url=canonical,
            canonical_url=canonical,
            title=str(row.get("business_name") or row.get("event") or ""),
            source_id=source_id,
            source_name=source_name,
            source_domain=domain,
            published_at=_date_time(row.get("date_posted")),
            raw_artifact_path=str(source_path),
            raw_artifact_hash=_sha256(source_path),
            record_status=RecordStatus.REVIEW,
            validation_errors=["legacy_uncertain_row"],
            metadata={"inferred_legacy_identity": True},
        )
        store.save_candidate(candidate)
        review = ReviewItem(
            review_id=stable_uuid("review", run_id, "migration", candidate.candidate_id),
            run_id=run_id,
            stage="migration",
            record_type="discovery_candidate",
            record_id=candidate.candidate_id,
            reason_code="legacy_uncertain_row",
            validation_errors=["Imported from uncertain_leads.csv"],
            raw_artifact_path=str(source_path),
        )
        store.add_review(review)
        report.counts["review_items"] += 1

    def _import_contact_row(
        self,
        store: StateStore,
        run_id: str,
        source_path: Path,
        row: dict,
        events_by_business: dict[str, list[LeadEvent]],
        organizations: dict[str, Organization],
        report: MigrationReport,
    ) -> None:
        name = str(row.get("person") or "").strip()
        business = normalize_text(row.get("business_name") or "")
        if not name or not business:
            return
        events = events_by_business.get(business, [])
        if not events:
            report.warnings.append(
                f"{source_path}: contact {name!r} has no matching imported lead"
            )
            return
        for event in events:
            organization = organizations[event.organization_id]
            evidence = _row_evidence(
                row,
                event.evidence[0].url if event.evidence else "https://app.apollo.io/",
                "Imported legacy contact detail.",
            )
            person = Person(
                person_id=person_id(name, organization.organization_id),
                organization_id=organization.organization_id,
                name=name,
                title=str(row.get("title") or ""),
                evidence=evidence,
                inferred_identity=True,
            )
            store.save_person(person)
            methods = {
                "email": str(row.get("email") or "").strip(),
                "phone": str(row.get("phone") or "").strip(),
                "linkedin": str(row.get("linkedin") or "").strip(),
            }
            if not any(methods.values()):
                continue
            contact = ContactCandidate(
                contact_candidate_id=stable_uuid(
                    "legacy-contact", run_id, event.lead_event_id, person.person_id
                ),
                run_id=run_id,
                lead_event_id=event.lead_event_id,
                organization_id=organization.organization_id,
                person_id=person.person_id,
                person_name=person.name,
                title=person.title,
                provider="legacy",
                verification_status=VerificationStatus.UNKNOWN,
                verification_reason="imported_without_reverification",
                selected=True,
                evidence=evidence,
                **methods,
            )
            store.save_contact(contact)
            report.counts["contacts"] += 1

    def _day_dirs(self) -> list[Path]:
        if not self.results_dir.exists():
            return []
        return sorted(
            path
            for path in self.results_dir.iterdir()
            if path.is_dir() and DAY_RE.fullmatch(path.name)
        )


def _sqlite_backup(source: Path, target: Path) -> None:
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()


def _checkpoint_for_replace(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        conn.close()


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as file:
        return list(csv.DictReader(file))


def _file_info(path: Path) -> dict:
    return {"path": str(path), "sha256": _sha256(path), "byte_count": path.stat().st_size}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")
    os.replace(temp, path)


def _ensure_source(store: StateStore, source_site: str, canonical: str) -> tuple[str, str, str]:
    try:
        root = canonicalize_url(source_site) if source_site else ""
    except ValueError:
        root = ""
    parts = urlsplit(root or canonical)
    domain = (parts.hostname or "").casefold()
    source_url = f"{parts.scheme}://{parts.netloc}/"
    source_id = stable_uuid("source", source_url)
    store.upsert_source(source_id, domain or "Legacy source", source_url, domain)
    return source_id, domain or "Legacy source", domain


def _date_value(value: object) -> date | None:
    raw = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _date_time(value: object) -> datetime | None:
    parsed = _date_value(value)
    return datetime.combine(parsed, time.min, tzinfo=timezone.utc) if parsed else None


def _decision_makers(row: dict) -> list[tuple[str, str]]:
    out = []
    for entry in str(row.get("Decision_Makers") or "").split(";"):
        name, _, title = entry.strip().partition(" — ")
        if name:
            out.append((name.strip(), title.strip()))
    if not out and str(row.get("person") or "").strip():
        out.append((str(row["person"]).strip(), ""))
    return out


def _row_evidence(row: dict, fallback_url: str, supports: str) -> list[Evidence]:
    urls = URL_RE.findall(
        " ".join(
            str(row.get(key) or "")
            for key in ("sources", "Decision_Maker_Sources")
        )
    )
    if not urls:
        urls = [fallback_url]
    evidence = []
    for url in dict.fromkeys(urls):
        try:
            evidence.append(Evidence(url=url.rstrip(".)]"), supports=supports, provider="legacy"))
        except Exception:
            continue
    return evidence or [
        Evidence(url="https://app.apollo.io/", supports=supports, provider="legacy")
    ]


def _employee_count(value: object) -> dict | None:
    raw = str(value or "").strip()
    return {"value": raw, "scope": "", "as_of": "", "confidence": "low"} if raw else None


def _integer_score(value: object) -> int | None:
    try:
        score = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return score if 0 <= score <= 100 else None
