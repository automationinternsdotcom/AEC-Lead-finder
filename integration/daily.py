"""Resumable daily Grok discovery and run-scoped Pipedrive/Warmy ingestion.

Run with the production env and absolute DB_PATH, RESULTS_DIR and
AETHER_SALES_DB_PATH. --enroll-draft is standing authorization to add each
ready batch to the configured draft; it never starts a campaign.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Settings
from .database import Database
from .handoff import handoff_content_hash, ingest_handoff, load_handoff
from .history import reconcile_history
from .models import ApprovalBatch
from .worker import run_once
from .workflows import SalesWorkflows

REPO = Path(__file__).resolve().parents[1]
CHECKPOINT = "daily:grok-pipedrive-warmy:v1"


def next_window(checkpoint: dict, today: date) -> tuple[str, str]:
    until = today - timedelta(days=1)
    last = date.fromisoformat(checkpoint.get("through", "2026-09-01"))
    return (min(last - timedelta(days=2), until).isoformat(), until.isoformat())


def preserve_existing_enrollments(db: Database, handoff):
    """Keep existing campaign merge fields stable during overlapping crawls."""
    recipients = {r.recipient_id: r for r in handoff.recipients}
    with db.connection() as conn:
        rows = conn.execute("""SELECT s.sequence_id, s.anchor_lead_event_id, r.recipient_id, r.normalized_email
            FROM outreach_sequences s JOIN sales_recipients r
            ON r.recipient_id=s.primary_recipient_id
            WHERE s.approval_state IN ('enrolled','replied')""").fetchall()
    ids = {r["sequence_id"] for r in rows}
    recipient_ids = {r["recipient_id"] for r in rows}
    enrolled_anchors = {r["anchor_lead_event_id"] for r in rows}
    emails = {r["normalized_email"].strip().casefold() for r in rows}
    sequences = [s for s in handoff.sequences if s.sequence_id not in ids
                 and recipients[s.primary_recipient_id].email.strip().casefold() not in emails]
    # A duplicate of an enrolled anchor must not reset its CRM outreach state.
    # New, distinct events for that company remain eligible for CRM ingestion.
    needed_anchors = {s.anchor_lead_event_id for s in sequences}
    events = [e for e in handoff.lead_events
              if e.lead_event_id not in enrolled_anchors or e.lead_event_id in needed_anchors]
    retained_recipients = [r for r in handoff.recipients if r.recipient_id not in recipient_ids]
    return handoff.model_copy(update={"sequences": sequences, "recipients": retained_recipients,
                                     "lead_events": events}), len(handoff.sequences) - len(sequences)


def ingest(settings: Settings, db: Database, path: Path, *, enroll_draft: bool) -> dict:
    handoff, history_reused = reconcile_history(db, load_handoff(path))
    handoff, preserved = preserve_existing_enrollments(db, handoff)
    handoff = handoff.model_copy(update={"content_hash": handoff_content_hash(handoff)})
    prior_leads = {e.lead_event_id for e in handoff.lead_events
                   if (db.get_lead_event(e.lead_event_id) or {}).get("pipedrive_lead_id")}
    prior_prospects = {r.recipient_id for r in handoff.recipients
                       if (db.get_recipient(recipient_id=r.recipient_id) or {}).get("warmy_prospect_id")}
    result = ingest_handoff(db, handoff, source_file=str(path))
    result["existing_campaign_records_preserved"] = preserved
    result["historical_events_reused"] = history_reused
    for _ in range(120):
        with db.connection() as conn:
            pending = conn.execute("""SELECT status, count(*) AS n FROM work_items
                WHERE kind IN ('scout.lead.sync','scout.sequence.sync')
                AND (json_extract(payload,'$.lead_event.run_id')=?
                     OR json_extract(payload,'$.sequence.run_id')=?)
                AND status NOT IN ('completed','superseded') GROUP BY status""",
                (handoff.run_id, handoff.run_id)).fetchall()
        if not pending:
            break
        if any(r["status"] == "dead_letter" for r in pending):
            raise RuntimeError("This run has dead-letter sync jobs; inspect before retrying")
        if not run_once(settings, db=db):
            time.sleep(5)
    else:
        raise RuntimeError("Sync still pending; resume this run after provider recovery")

    ready = []
    for item in handoff.sequences:
        sequence = db.get_sequence(item.sequence_id)
        recipient = db.get_recipient(recipient_id=item.primary_recipient_id)
        if (sequence and sequence["eligibility_status"] == "ready"
                and sequence["approval_state"] in {"draft", "approved"}
                and recipient and recipient.get("warmy_prospect_id")):
            ready.append(sequence)
    result["ready_in_warmy"] = len(ready)
    linked_leads = {e.lead_event_id for e in handoff.lead_events
                    if (db.get_lead_event(e.lead_event_id) or {}).get("pipedrive_lead_id")}
    linked_prospects = {r.recipient_id for r in handoff.recipients
                        if (db.get_recipient(recipient_id=r.recipient_id) or {}).get("warmy_prospect_id")}
    result["new_pipedrive_mappings"] = len(linked_leads - prior_leads)
    result["new_warmy_mappings"] = len(linked_prospects - prior_prospects)
    result["pipedrive_linked"] = len(linked_leads)
    result["warmy_linked"] = len(linked_prospects)
    result["enrolled"] = 0
    if enroll_draft and ready:
        fingerprint = json.loads((REPO / "config/daily_campaign_fingerprint.json").read_text())
        if fingerprint["campaign_id"] != settings.warmy_campaign_id:
            raise RuntimeError("Daily campaign fingerprint belongs to a different campaign")
        settings = replace(settings, warmy_campaign_manifest_hash=fingerprint["manifest_hash"])
        settings = replace(settings, warmy_enrollment_enabled=True, campaign_start_enabled=False)
        workflows = SalesWorkflows(settings, db)
        try:
            settings.require_campaign_enrollment(draft_only=True)
            campaign = workflows.warmy.get_campaign(settings.warmy_campaign_id)
            if str((campaign.get("data") or campaign).get("status")) != "draft":
                raise RuntimeError("Campaign is no longer draft; workspace ingestion completed")
            workflows._validate_live_campaign(campaign, for_enrollment=True)
            now = datetime.now(UTC)
            batch = ApprovalBatch(
                batch_id=f"daily-{handoff.run_id}-{uuid.uuid4().hex[:12]}",
                campaign_id=settings.warmy_campaign_id,
                campaign_manifest_hash=settings.warmy_campaign_manifest_hash,
                sequence_ids=[s["sequence_id"] for s in ready],
                merge_hashes={s["sequence_id"]: s["merge_hash"] for s in ready},
                maximum_recipient_count=len(ready),
                approved_by="User standing instruction: daily Grok ingestion into Pipedrive and Warmy (2026-09-07)",
                approved_at=now, expires_at=now + timedelta(hours=24),
            )
            db.save_approval_batch(batch)
            for sequence in ready:
                workflows.enroll_sequence({"sequence_id": sequence["sequence_id"], "draft_only": True})
                result["enrolled"] += 1
        finally:
            workflows.close()
    result["run_id"] = handoff.run_id
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--run-id")
    parser.add_argument("--retry-review", action="store_true")
    parser.add_argument("--handoff", type=Path)
    parser.add_argument("--enroll-draft", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    db = Database(settings.database_path)
    with open(str(Path(settings.database_path).resolve()) + ".daily.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "already_running"}))
            return 0
        checkpoint = db.get_state(CHECKPOINT) or {}
        if args.handoff:
            path = args.handoff
            until = args.until
        else:
            active = checkpoint.get("active") or {}
            since, until = next_window(checkpoint, datetime.now(ZoneInfo("America/Phoenix")).date())
            since = args.since or active.get("since") or since
            until = args.until or active.get("until") or until
            run_id = args.run_id or active.get("run_id") or f"daily-{since}-{until}"
            checkpoint["active"] = {"since": since, "until": until, "run_id": run_id}
            db.set_state(CHECKPOINT, checkpoint)
            results = Path(os.environ.get("RESULTS_DIR", str(REPO / "results")))
            manifest_path = results / until / "runs" / run_id / "manifest.json"
            command = ["uv", "run", "scout/pipeline.py", "--since", since, "--stamp", until,
                       "--run-id", run_id, "--treg-go", "--treg-budget-usd", "5"]
            if manifest_path.exists():
                command.append("--resume")
            if args.retry_review:
                command.append("--retry-review")
            env = dict(os.environ, AETHER_INTEGRATION_ENABLED="false", GROK_MODEL="grok-4.3", EXTRACTOR_MODEL="grok-4.3")
            completed = subprocess.run(command, cwd=REPO, env=env)
            if not manifest_path.exists():
                raise RuntimeError("Pipeline produced no manifest")
            manifest = json.loads(manifest_path.read_text())
            if completed.returncode and not (manifest.get("errors") and all(
                    error.get("type") == "ZeroLeadError" for error in manifest["errors"])):
                raise RuntimeError(f"Pipeline failed; resume run {run_id}")
            path = manifest_path.parent / "sales_handoff.json"
            if not path.exists():
                matches = list(manifest_path.parent.rglob("sales_handoff.json"))
                if len(matches) != 1:
                    raise RuntimeError("Expected one exported sales handoff")
                path = matches[0]
        result = ingest(settings, db, path, enroll_draft=args.enroll_draft)
        if until:
            checkpoint = {"through": max(until, checkpoint.get("through", until)),
                          "last_run_id": result["run_id"], "last_result": result}
            db.set_state(CHECKPOINT, checkpoint)
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
