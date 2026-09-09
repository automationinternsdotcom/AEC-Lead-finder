"""Durable Mac-local SQLite queue, mappings, and suppression state."""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import (
    ApprovalBatch,
    CompanySync,
    LeadEventSync,
    MappingRecord,
    OutreachSequenceSync,
    RecipientSync,
    SequenceApprovalState,
    SuppressionReason,
    WorkItem,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS webhook_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL CHECK (provider IN ('warmy', 'pipedrive')),
  provider_event_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  signature_valid INTEGER NOT NULL DEFAULT 0,
  received_at TEXT NOT NULL,
  UNIQUE (provider, provider_event_id)
);

CREATE TABLE IF NOT EXISTS work_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  dedupe_key TEXT NOT NULL UNIQUE,
  payload TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'running', 'completed', 'dead_letter', 'superseded')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  run_after TEXT NOT NULL,
  lease_owner TEXT,
  lease_until TEXT,
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE INDEX IF NOT EXISTS work_items_ready_idx
  ON work_items (status, run_after, id);

CREATE TABLE IF NOT EXISTS lead_mappings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  outreach_id TEXT NOT NULL UNIQUE,
  source_contact_candidate_id TEXT NOT NULL DEFAULT '',
  source_verification_status TEXT NOT NULL DEFAULT 'unknown',
  source_verification_reason TEXT NOT NULL DEFAULT '',
  source_provider TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL,
  lead_event_id TEXT NOT NULL DEFAULT '',
  organization_id TEXT NOT NULL DEFAULT '',
  person_id TEXT NOT NULL DEFAULT '',
  why_line TEXT NOT NULL DEFAULT '',
  pipedrive_organization_id INTEGER,
  pipedrive_person_id INTEGER,
  pipedrive_lead_id TEXT UNIQUE,
  pipedrive_deal_id INTEGER UNIQUE,
  warmy_prospect_id TEXT,
  warmy_campaign_id TEXT,
  warmy_mailbox_id TEXT,
  gmail_thread_id TEXT,
  verification_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (verification_status IN ('pending', 'valid', 'invalid', 'catch_all', 'unknown')),
  reply_disposition TEXT
    CHECK (reply_disposition IS NULL OR reply_disposition IN
      ('pending_review', 'positive', 'negative', 'out_of_office', 'unsubscribe', 'other')),
  reply_received_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS lead_mappings_email_idx ON lead_mappings (email);
CREATE INDEX IF NOT EXISTS lead_mappings_warmy_prospect_idx
  ON lead_mappings (warmy_prospect_id);

CREATE TABLE IF NOT EXISTS suppressions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE,
  reason TEXT NOT NULL CHECK (reason IN
    ('bounce', 'unsubscribe', 'negative_reply', 'invalid', 'manual', 'complaint')),
  source TEXT NOT NULL,
  external_event_id TEXT NOT NULL DEFAULT '',
  suppressed_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unsubscribe_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash TEXT NOT NULL UNIQUE,
  email TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_operations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,
  operation_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'started'
    CHECK (status IN ('started', 'completed', 'failed', 'uncertain')),
  request_payload TEXT NOT NULL DEFAULT '{}',
  response_payload TEXT NOT NULL DEFAULT '{}',
  external_id TEXT NOT NULL DEFAULT '',
  last_error TEXT NOT NULL DEFAULT '',
  lease_owner TEXT,
  lease_until TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE (provider, operation_key)
);

CREATE TABLE IF NOT EXISTS app_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS integration_runs (
  run_id TEXT PRIMARY KEY,
  source_file TEXT NOT NULL DEFAULT '',
  discovered_count INTEGER NOT NULL DEFAULT 0,
  enqueued_count INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS sales_companies (
  company_id TEXT PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  domain TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{}',
  pipedrive_organization_id INTEGER UNIQUE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sales_company_aliases (
  alias_type TEXT NOT NULL,
  alias_value TEXT NOT NULL,
  company_id TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (alias_type, alias_value),
  FOREIGN KEY (company_id) REFERENCES sales_companies(company_id)
);

CREATE TABLE IF NOT EXISTS sales_lead_events (
  lead_event_id TEXT PRIMARY KEY,
  company_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  crm_state TEXT NOT NULL DEFAULT 'local_review',
  pipedrive_lead_id TEXT UNIQUE,
  pipedrive_deal_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (company_id) REFERENCES sales_companies(company_id)
);

CREATE INDEX IF NOT EXISTS sales_lead_events_company_idx
  ON sales_lead_events(company_id, crm_state);

CREATE TABLE IF NOT EXISTS sales_recipients (
  recipient_id TEXT PRIMARY KEY,
  company_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  normalized_email TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  verification_status TEXT NOT NULL DEFAULT 'pending',
  verification_policy_version TEXT NOT NULL DEFAULT '',
  verification_reason TEXT NOT NULL DEFAULT '',
  pipedrive_person_id INTEGER,
  warmy_prospect_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (company_id, person_id),
  FOREIGN KEY (company_id) REFERENCES sales_companies(company_id)
);

CREATE INDEX IF NOT EXISTS sales_recipients_email_idx
  ON sales_recipients(normalized_email);
CREATE INDEX IF NOT EXISTS sales_recipients_warmy_idx
  ON sales_recipients(warmy_prospect_id);

CREATE TABLE IF NOT EXISTS outreach_sequences (
  sequence_id TEXT PRIMARY KEY,
  company_id TEXT NOT NULL,
  campaign_protocol TEXT NOT NULL,
  anchor_lead_event_id TEXT NOT NULL,
  primary_recipient_id TEXT NOT NULL,
  merge_hash TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  approval_state TEXT NOT NULL DEFAULT 'draft'
    CHECK (approval_state IN ('draft', 'approved', 'enrolled', 'replied', 'superseded')),
  eligibility_status TEXT NOT NULL DEFAULT 'review'
    CHECK (eligibility_status IN ('ready', 'review', 'blocked')),
  eligibility_reasons TEXT NOT NULL DEFAULT '[]',
  approval_batch_id TEXT,
  warmy_campaign_id TEXT,
  warmy_mailbox_id TEXT,
  pipedrive_deal_id INTEGER,
  reply_disposition TEXT,
  reply_received_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (company_id, campaign_protocol),
  FOREIGN KEY (company_id) REFERENCES sales_companies(company_id),
  FOREIGN KEY (anchor_lead_event_id) REFERENCES sales_lead_events(lead_event_id),
  FOREIGN KEY (primary_recipient_id) REFERENCES sales_recipients(recipient_id)
);

CREATE TABLE IF NOT EXISTS outreach_sequence_events (
  sequence_id TEXT NOT NULL,
  lead_event_id TEXT NOT NULL,
  event_role TEXT NOT NULL CHECK (event_role IN ('anchor', 'supporting')),
  PRIMARY KEY (sequence_id, lead_event_id),
  FOREIGN KEY (sequence_id) REFERENCES outreach_sequences(sequence_id),
  FOREIGN KEY (lead_event_id) REFERENCES sales_lead_events(lead_event_id)
);

CREATE TABLE IF NOT EXISTS approval_batches (
  batch_id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL,
  campaign_manifest_hash TEXT NOT NULL,
  payload TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  approved_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eligibility_decisions (
  decision_id TEXT PRIMARY KEY,
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ready', 'review', 'blocked', 'superseded')),
  reasons TEXT NOT NULL DEFAULT '[]',
  evidence TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS eligibility_decisions_subject_idx
  ON eligibility_decisions(subject_type, subject_id, created_at);

CREATE TABLE IF NOT EXISTS email_verifications (
  normalized_email TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'valid', 'invalid', 'catch_all', 'unknown')),
  reason TEXT NOT NULL DEFAULT '',
  provider_payload TEXT NOT NULL DEFAULT '{}',
  verified_at TEXT NOT NULL,
  PRIMARY KEY (normalized_email, policy_version)
);

CREATE TABLE IF NOT EXISTS recipient_address_history (
  recipient_id TEXT NOT NULL,
  normalized_email TEXT NOT NULL,
  snapshot TEXT NOT NULL,
  archived_at TEXT NOT NULL,
  PRIMARY KEY (recipient_id, normalized_email, archived_at)
);

CREATE TABLE IF NOT EXISTS provider_rate_slots (
  name TEXT PRIMARY KEY,
  next_at REAL NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _timestamp(value: datetime | None) -> str:
    if value is None:
        return _now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


class Database:
    """Small transactional state store intended for one persistent Mac.

    Every operation opens a short-lived connection. WAL mode plus an immediate
    transaction for work claims allows the webhook server and worker process to
    safely share the same file.
    """

    def __init__(self, path: str | Path):
        if not str(path).strip():
            raise ValueError("AETHER_SALES_DB_PATH is required")
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        self._migrate(conn)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Apply additive migrations for databases created by earlier previews."""
        work_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='work_items'"
        ).fetchone()
        work_sql = str(work_sql_row["sql"] or "") if work_sql_row else ""
        if work_sql and "superseded" not in work_sql:
            conn.executescript(
                """
                ALTER TABLE work_items RENAME TO work_items_legacy;
                CREATE TABLE work_items (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  kind TEXT NOT NULL,
                  dedupe_key TEXT NOT NULL UNIQUE,
                  payload TEXT NOT NULL DEFAULT '{}',
                  status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'running', 'completed', 'dead_letter', 'superseded')),
                  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                  run_after TEXT NOT NULL,
                  lease_owner TEXT,
                  lease_until TEXT,
                  last_error TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  completed_at TEXT
                );
                INSERT INTO work_items(
                  id, kind, dedupe_key, payload, status, attempt_count, run_after,
                  lease_owner, lease_until, last_error, created_at, updated_at,
                  completed_at
                )
                SELECT id, kind, dedupe_key, payload, status, attempt_count, run_after,
                       lease_owner, lease_until, last_error, created_at, updated_at,
                       completed_at
                FROM work_items_legacy;
                DROP TABLE work_items_legacy;
                CREATE INDEX work_items_ready_idx
                  ON work_items(status, run_after, id);
                """
            )
        operation_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='provider_operations'"
        ).fetchone()
        operation_sql = (
            str(operation_sql_row["sql"] or "") if operation_sql_row else ""
        )
        if operation_sql and "uncertain" not in operation_sql:
            conn.executescript(
                """
                ALTER TABLE provider_operations RENAME TO provider_operations_legacy;
                CREATE TABLE provider_operations (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  provider TEXT NOT NULL,
                  operation_key TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'started'
                    CHECK (status IN ('started', 'completed', 'failed', 'uncertain')),
                  request_payload TEXT NOT NULL DEFAULT '{}',
                  response_payload TEXT NOT NULL DEFAULT '{}',
                  external_id TEXT NOT NULL DEFAULT '',
                  last_error TEXT NOT NULL DEFAULT '',
                  lease_owner TEXT,
                  lease_until TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  completed_at TEXT,
                  UNIQUE (provider, operation_key)
                );
                INSERT INTO provider_operations(
                  id, provider, operation_key, status, request_payload,
                  response_payload, external_id, last_error, lease_owner,
                  lease_until, created_at, updated_at, completed_at
                )
                SELECT id, provider, operation_key, status, request_payload,
                       response_payload, external_id, last_error, lease_owner,
                       lease_until, created_at, updated_at, completed_at
                FROM provider_operations_legacy;
                DROP TABLE provider_operations_legacy;
                """
            )
        additions = {
            "lead_mappings": {
                "source_verification_status": "TEXT NOT NULL DEFAULT 'unknown'",
                "source_verification_reason": "TEXT NOT NULL DEFAULT ''",
                "source_provider": "TEXT NOT NULL DEFAULT ''",
                "why_line": "TEXT NOT NULL DEFAULT ''",
            },
            "provider_operations": {
                "lease_owner": "TEXT",
                "lease_until": "TEXT",
            },
            "outreach_sequences": {
                "reply_disposition": "TEXT",
                "reply_received_at": "TEXT",
            },
        }
        for table, columns in additions.items():
            existing = {
                str(row["name"])
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, definition in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    @contextmanager
    def connection(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def healthcheck(self) -> bool:
        with self.connection() as conn:
            return conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1

    def reserve_provider_slot(self, name: str, interval_seconds: float) -> float:
        """Return the wait for a cross-process request slot on this account."""
        now = datetime.now(UTC).timestamp()
        with self.connection(immediate=True) as conn:
            row = conn.execute("SELECT next_at FROM provider_rate_slots WHERE name=?", (name,)).fetchone()
            slot = max(now, row[0] if row else now)
            conn.execute("INSERT INTO provider_rate_slots VALUES (?,?) ON CONFLICT(name) DO UPDATE SET next_at=excluded.next_at", (name, slot + interval_seconds))
        return slot - now

    def enqueue_work(
        self,
        kind: str,
        dedupe_key: str,
        payload: dict[str, Any],
        *,
        run_after: datetime | None = None,
    ) -> bool:
        now = _now()
        with self.connection() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO work_items(
                       kind, dedupe_key, payload, run_after, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (kind, dedupe_key, _json(payload), _timestamp(run_after), now, now),
            )
        return cursor.rowcount > 0

    def accept_webhook(
        self,
        provider: str,
        event_id: str,
        event_type: str,
        payload: dict[str, Any],
        payload_hash: str,
        *,
        signature_valid: bool,
    ) -> bool:
        now = _now()
        with self.connection(immediate=True) as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO webhook_events(
                       provider, provider_event_id, event_type, payload_hash,
                       payload, signature_valid, received_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    provider,
                    event_id,
                    event_type,
                    payload_hash,
                    _json(payload),
                    int(signature_valid),
                    now,
                ),
            )
            if cursor.rowcount == 0:
                return False
            conn.execute(
                """INSERT OR IGNORE INTO work_items(
                       kind, dedupe_key, payload, run_after, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    f"{provider}.event",
                    f"{provider}:event:{event_id}",
                    _json({"event_id": event_id, **payload, "event_type": event_type}),
                    now,
                    now,
                    now,
                ),
            )
        return True

    def claim_work(self, owner: str, limit: int, lease_seconds: int) -> list[WorkItem]:
        now = datetime.now(UTC)
        now_text = now.isoformat()
        lease_until = (now + timedelta(seconds=max(30, lease_seconds))).isoformat()
        with self.connection(immediate=True) as conn:
            rows = conn.execute(
                """SELECT id FROM work_items
                   WHERE (status='pending' AND run_after <= ?)
                      OR (status='running' AND lease_until < ?)
                   ORDER BY run_after, id
                   LIMIT ?""",
                (now_text, now_text, max(0, min(limit, 100))),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"""UPDATE work_items
                    SET status='running', attempt_count=attempt_count + 1,
                        lease_owner=?, lease_until=?, updated_at=?
                    WHERE id IN ({placeholders})""",
                (owner, lease_until, now_text, *ids),
            )
            claimed = conn.execute(
                f"""SELECT id, kind, dedupe_key, payload, attempt_count
                    FROM work_items WHERE id IN ({placeholders})
                    ORDER BY run_after, id""",
                ids,
            ).fetchall()
        return [
            WorkItem(
                id=row["id"],
                kind=row["kind"],
                dedupe_key=row["dedupe_key"],
                payload=json.loads(row["payload"]),
                attempt_count=row["attempt_count"],
            )
            for row in claimed
        ]

    def complete_work(self, item_id: int, owner: str) -> None:
        now = _now()
        with self.connection() as conn:
            conn.execute(
                """UPDATE work_items
                   SET status='completed', completed_at=?, lease_owner=NULL,
                       lease_until=NULL, last_error='', updated_at=?
                   WHERE id=? AND lease_owner=? AND status='running'""",
                (now, now, item_id, owner),
            )

    def retry_work(
        self,
        item: WorkItem,
        owner: str,
        error: Exception,
        *,
        max_attempts: int,
    ) -> bool:
        terminal = item.attempt_count >= max_attempts
        delay = min(3600, 2 ** min(item.attempt_count, 10)) + random.randint(0, 15)
        now = datetime.now(UTC)
        with self.connection() as conn:
            conn.execute(
                """UPDATE work_items
                   SET status=?, run_after=?, lease_owner=NULL, lease_until=NULL,
                       last_error=?, updated_at=?
                   WHERE id=? AND lease_owner=?""",
                (
                    "dead_letter" if terminal else "pending",
                    (now + timedelta(seconds=delay)).isoformat(),
                    str(error)[:1000],
                    now.isoformat(),
                    item.id,
                    owner,
                ),
            )
        return terminal

    def defer_work(
        self,
        item: WorkItem,
        owner: str,
        error: Exception,
        *,
        delay_seconds: int = 300,
    ) -> None:
        """Release an activation-blocked item without spending its retry budget."""
        now = datetime.now(UTC)
        with self.connection() as conn:
            conn.execute(
                """UPDATE work_items
                   SET status='pending', attempt_count=MAX(0, attempt_count - 1),
                       run_after=?, lease_owner=NULL, lease_until=NULL,
                       last_error=?, updated_at=?
                   WHERE id=? AND lease_owner=?""",
                (
                    (now + timedelta(seconds=max(30, delay_seconds))).isoformat(),
                    str(error)[:1000],
                    now.isoformat(),
                    item.id,
                    owner,
                ),
            )

    def replay_dead_letters(self) -> int:
        now = _now()
        with self.connection(immediate=True) as conn:
            cursor = conn.execute(
                """UPDATE work_items
                   SET status='pending', attempt_count=0, run_after=?,
                       lease_owner=NULL, lease_until=NULL, last_error='',
                       completed_at=NULL, updated_at=?
                   WHERE status='dead_letter'""",
                (now, now),
            )
        return cursor.rowcount

    def upsert_mapping(self, mapping: MappingRecord) -> None:
        data = mapping.model_dump(mode="json")
        now = _now()
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO lead_mappings(
                       outreach_id, source_contact_candidate_id,
                       source_verification_status, source_verification_reason,
                       source_provider, email, lead_event_id,
                       organization_id, person_id, why_line,
                       pipedrive_organization_id,
                       pipedrive_person_id,
                       pipedrive_lead_id, pipedrive_deal_id, warmy_prospect_id,
                       warmy_campaign_id, warmy_mailbox_id, gmail_thread_id,
                       verification_status, reply_disposition, reply_received_at,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(outreach_id) DO UPDATE SET
                       source_contact_candidate_id=excluded.source_contact_candidate_id,
                       source_verification_status=excluded.source_verification_status,
                       source_verification_reason=excluded.source_verification_reason,
                       source_provider=excluded.source_provider,
                       email=excluded.email,
                       lead_event_id=excluded.lead_event_id,
                       organization_id=excluded.organization_id,
                       person_id=excluded.person_id,
                       why_line=excluded.why_line,
                       pipedrive_organization_id=COALESCE(
                           excluded.pipedrive_organization_id,
                           lead_mappings.pipedrive_organization_id),
                       pipedrive_person_id=COALESCE(
                           excluded.pipedrive_person_id,
                           lead_mappings.pipedrive_person_id),
                       pipedrive_lead_id=COALESCE(
                           excluded.pipedrive_lead_id,
                           lead_mappings.pipedrive_lead_id),
                       pipedrive_deal_id=COALESCE(
                           excluded.pipedrive_deal_id,
                           lead_mappings.pipedrive_deal_id),
                       warmy_prospect_id=COALESCE(
                           excluded.warmy_prospect_id,
                           lead_mappings.warmy_prospect_id),
                       warmy_campaign_id=COALESCE(
                           excluded.warmy_campaign_id,
                           lead_mappings.warmy_campaign_id),
                       warmy_mailbox_id=COALESCE(
                           excluded.warmy_mailbox_id,
                           lead_mappings.warmy_mailbox_id),
                       gmail_thread_id=COALESCE(
                           excluded.gmail_thread_id,
                           lead_mappings.gmail_thread_id),
                       verification_status=excluded.verification_status,
                       reply_disposition=COALESCE(
                           excluded.reply_disposition,
                           lead_mappings.reply_disposition),
                       reply_received_at=COALESCE(
                           excluded.reply_received_at,
                           lead_mappings.reply_received_at),
                       updated_at=excluded.updated_at""",
                (
                    data["outreach_id"],
                    data["source_contact_candidate_id"],
                    data["source_verification_status"],
                    data["source_verification_reason"],
                    data["source_provider"],
                    data["email"],
                    data["lead_event_id"],
                    data["organization_id"],
                    data["person_id"],
                    data["why_line"],
                    data["pipedrive_organization_id"],
                    data["pipedrive_person_id"],
                    data["pipedrive_lead_id"],
                    data["pipedrive_deal_id"],
                    data["warmy_prospect_id"],
                    data["warmy_campaign_id"],
                    data["warmy_mailbox_id"],
                    data["gmail_thread_id"],
                    data["verification_status"],
                    data["reply_disposition"],
                    data["reply_received_at"],
                    data["created_at"],
                    now,
                ),
            )

    def get_mapping(self, **lookup: Any) -> MappingRecord | None:
        allowed = {
            "outreach_id",
            "source_contact_candidate_id",
            "email",
            "warmy_prospect_id",
            "pipedrive_lead_id",
            "pipedrive_deal_id",
            "reply_disposition",
            "reply_received_at",
        }
        if len(lookup) != 1 or next(iter(lookup)) not in allowed:
            raise ValueError("exactly one supported mapping lookup is required")
        column, value = next(iter(lookup.items()))
        with self.connection() as conn:
            row = conn.execute(
                f"SELECT * FROM lead_mappings WHERE {column}=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (value,),
            ).fetchone()
        return MappingRecord.model_validate(dict(row)) if row else None

    def get_mappings_by_email(self, email: str) -> list[MappingRecord]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM lead_mappings WHERE email=? ORDER BY updated_at DESC",
                (email.strip().casefold(),),
            ).fetchall()
        return [MappingRecord.model_validate(dict(row)) for row in rows]

    def update_mapping(self, outreach_id: str, **fields: Any) -> None:
        allowed = {
            "source_contact_candidate_id",
            "source_verification_status",
            "source_verification_reason",
            "source_provider",
            "email",
            "why_line",
            "pipedrive_organization_id",
            "pipedrive_person_id",
            "pipedrive_lead_id",
            "pipedrive_deal_id",
            "warmy_prospect_id",
            "warmy_campaign_id",
            "warmy_mailbox_id",
            "gmail_thread_id",
            "verification_status",
            "reply_disposition",
            "reply_received_at",
        }
        bad = set(fields) - allowed
        if bad or not fields:
            raise ValueError(f"unsupported mapping fields: {sorted(bad)}")
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = [
            value.value if hasattr(value, "value") else value
            for value in fields.values()
        ]
        with self.connection() as conn:
            conn.execute(
                f"UPDATE lead_mappings SET {assignments}, updated_at=? "
                "WHERE outreach_id=?",
                (*values, _now(), outreach_id),
            )

    def suppress(
        self,
        email: str,
        reason: SuppressionReason | str,
        source: str,
        *,
        external_event_id: str = "",
    ) -> bool:
        normalized = email.strip().casefold()
        now = _now()
        reason_value = (
            reason.value if isinstance(reason, SuppressionReason) else str(reason)
        )
        with self.connection(immediate=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM suppressions WHERE email=?", (normalized,)
            ).fetchone()
            conn.execute(
                """INSERT INTO suppressions(
                       email, reason, source, external_event_id, suppressed_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(email) DO UPDATE SET
                       reason=excluded.reason,
                       source=excluded.source,
                       external_event_id=CASE
                         WHEN excluded.external_event_id <> ''
                         THEN excluded.external_event_id
                         ELSE suppressions.external_event_id END,
                       updated_at=excluded.updated_at""",
                (normalized, reason_value, source, external_event_id, now, now),
            )
        return exists is None

    def is_suppressed(self, email: str) -> bool:
        with self.connection() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM suppressions WHERE email=?",
                    (email.strip().casefold(),),
                ).fetchone()
                is not None
            )

    def store_unsubscribe_token(self, token_id: str, email: str) -> None:
        token_hash = hashlib.sha256(token_id.encode()).hexdigest()
        now = _now()
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO unsubscribe_tokens(token_hash, email, created_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(email) DO UPDATE SET token_hash=excluded.token_hash""",
                (token_hash, email.strip().casefold(), now),
            )

    def resolve_unsubscribe_token(self, token_id: str) -> str | None:
        token_hash = hashlib.sha256(token_id.encode()).hexdigest()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT email FROM unsubscribe_tokens WHERE token_hash=?",
                (token_hash,),
            ).fetchone()
        return row["email"] if row else None

    def claim_operation(
        self,
        provider: str,
        operation_key: str,
        payload: dict,
        *,
        owner: str = "",
        lease_seconds: int = 300,
    ) -> bool:
        now_value = datetime.now(UTC)
        now = now_value.isoformat()
        lease_until = (
            now_value + timedelta(seconds=max(30, lease_seconds))
        ).isoformat()
        with self.connection(immediate=True) as conn:
            row = conn.execute(
                """SELECT status, lease_until FROM provider_operations
                   WHERE provider=? AND operation_key=?""",
                (provider, operation_key),
            ).fetchone()
            active = (
                row
                and row["status"] == "started"
                and row["lease_until"]
                and row["lease_until"] >= now
            )
            if row and (row["status"] == "completed" or active):
                return False
            if row:
                conn.execute(
                    """UPDATE provider_operations
                       SET status='started', request_payload=?, last_error='',
                           lease_owner=?, lease_until=?, updated_at=?, completed_at=NULL
                       WHERE provider=? AND operation_key=?""",
                    (
                        _json(payload),
                        owner,
                        lease_until,
                        now,
                        provider,
                        operation_key,
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO provider_operations(
                           provider, operation_key, request_payload,
                           lease_owner, lease_until, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        provider,
                        operation_key,
                        _json(payload),
                        owner,
                        lease_until,
                        now,
                        now,
                    ),
                )
        return True

    def get_operation(self, provider: str, operation_key: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                """SELECT status, external_id, response_payload, last_error
                   FROM provider_operations WHERE provider=? AND operation_key=?""",
                (provider, operation_key),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["response_payload"] = json.loads(result["response_payload"])
        return result

    def complete_operation(
        self,
        provider: str,
        operation_key: str,
        response: dict,
        *,
        external_id: str = "",
    ) -> None:
        now = _now()
        with self.connection() as conn:
            conn.execute(
                """UPDATE provider_operations
                   SET status='completed', response_payload=?, external_id=?,
                       lease_owner=NULL, lease_until=NULL, completed_at=?, updated_at=?
                   WHERE provider=? AND operation_key=?""",
                (_json(response), external_id, now, now, provider, operation_key),
            )

    def fail_operation(
        self, provider: str, operation_key: str, error: Exception
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """UPDATE provider_operations
                   SET status='failed', last_error=?, updated_at=?
                       , lease_owner=NULL, lease_until=NULL
                   WHERE provider=? AND operation_key=?""",
                (str(error)[:1000], _now(), provider, operation_key),
            )

    def mark_operation_uncertain(
        self, provider: str, operation_key: str, error: Exception
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """UPDATE provider_operations
                   SET status='uncertain', last_error=?, updated_at=?,
                       lease_owner=NULL, lease_until=NULL
                   WHERE provider=? AND operation_key=?""",
                (str(error)[:1000], _now(), provider, operation_key),
            )

    def get_state(self, key: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT value FROM app_state WHERE key=?", (key,)
            ).fetchone()
        return json.loads(row["value"]) if row else None

    def set_state(self, key: str, value: dict[str, Any]) -> None:
        now = _now()
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO app_state(key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value=excluded.value, updated_at=excluded.updated_at""",
                (key, _json(value), now),
            )

    def record_run(
        self, run_id: str, source_file: str, discovered_count: int, enqueued_count: int
    ) -> None:
        now = _now()
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO integration_runs(
                       run_id, source_file, discovered_count, enqueued_count,
                       started_at, completed_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                       source_file=excluded.source_file,
                       discovered_count=excluded.discovered_count,
                       enqueued_count=excluded.enqueued_count,
                       completed_at=excluded.completed_at""",
                (run_id, source_file, discovered_count, enqueued_count, now, now),
            )

    def upsert_company(self, company: CompanySync, *, source: str = "scout") -> None:
        now = _now()
        payload = company.model_dump(mode="json")
        aliases = {
            ("name", company.canonical_name.strip().casefold()),
            *(("name", value.strip().casefold()) for value in company.aliases if value.strip()),
            *(("legacy_id", value.strip()) for value in company.legacy_ids if value.strip()),
        }
        if company.domain.strip():
            aliases.add(("domain", company.domain.strip().casefold()))
        with self.connection(immediate=True) as conn:
            existing = conn.execute(
                "SELECT company_id FROM sales_companies WHERE company_id=?",
                (company.company_id,),
            ).fetchone()
            if not existing:
                for alias_type, alias_value in list(aliases):
                    row = conn.execute(
                        "SELECT company_id FROM sales_company_aliases WHERE alias_type=? AND alias_value=?",
                        (alias_type, alias_value),
                    ).fetchone()
                    if row and row["company_id"] != company.company_id:
                        other = conn.execute("SELECT canonical_name,domain FROM sales_companies WHERE company_id=?", (row["company_id"],)).fetchone()
                        if (alias_type == "domain" and other
                                and other["canonical_name"].strip().casefold() != company.canonical_name.strip().casefold()):
                            # Parent organizations and projects may share a
                            # domain; a unique alias is not proof of identity.
                            aliases.discard((alias_type, alias_value))
                            continue
                        if (alias_type == "name" and company.domain.strip() and other
                                and other["domain"] and other["domain"].casefold() != company.domain.strip().casefold()):
                            # Distinct corporate/franchise domains may share a
                            # display name. Preserve both identities and do not
                            # steal the existing name alias.
                            aliases.discard((alias_type, alias_value))
                            continue
                        raise ValueError(
                            f"company alias {alias_type}:{alias_value} already belongs to {row['company_id']}"
                        )
            conn.execute(
                """INSERT INTO sales_companies(
                       company_id, canonical_name, domain, payload, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(company_id) DO UPDATE SET
                       canonical_name=excluded.canonical_name,
                       domain=CASE WHEN excluded.domain <> '' THEN excluded.domain ELSE sales_companies.domain END,
                       payload=excluded.payload,
                       updated_at=excluded.updated_at""",
                (
                    company.company_id,
                    company.canonical_name,
                    company.domain.strip().casefold(),
                    _json(payload),
                    now,
                    now,
                ),
            )
            for alias_type, alias_value in sorted(aliases):
                conn.execute(
                    """INSERT INTO sales_company_aliases(
                           alias_type, alias_value, company_id, source, created_at
                       ) VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(alias_type, alias_value) DO UPDATE SET
                           source=excluded.source""",
                    (alias_type, alias_value, company.company_id, source, now),
                )

    def resolve_company_alias(self, alias_type: str, alias_value: str) -> str | None:
        normalized = (
            alias_value.strip().casefold()
            if alias_type in {"name", "domain"}
            else alias_value.strip()
        )
        with self.connection() as conn:
            row = conn.execute(
                "SELECT company_id FROM sales_company_aliases WHERE alias_type=? AND alias_value=?",
                (alias_type, normalized),
            ).fetchone()
        return str(row["company_id"]) if row else None

    def get_company(self, company_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM sales_companies WHERE company_id=?",
                (company_id,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def update_company(self, company_id: str, **fields: Any) -> None:
        allowed = {"pipedrive_organization_id"}
        bad = set(fields) - allowed
        if bad or not fields:
            raise ValueError(f"unsupported company fields: {sorted(bad)}")
        assignments = ", ".join(f"{key}=?" for key in fields)
        with self.connection() as conn:
            conn.execute(
                f"UPDATE sales_companies SET {assignments}, updated_at=? WHERE company_id=?",
                (*fields.values(), _now(), company_id),
            )

    def upsert_lead_event(self, event: LeadEventSync) -> None:
        now = _now()
        crm_state = "ready" if event.crm_eligible else "local_review"
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO sales_lead_events(
                       lead_event_id, company_id, run_id, payload, crm_state,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(lead_event_id) DO UPDATE SET
                       company_id=excluded.company_id,
                       run_id=excluded.run_id,
                       payload=excluded.payload,
                       crm_state=CASE
                         WHEN sales_lead_events.pipedrive_deal_id IS NOT NULL THEN 'converted'
                         ELSE excluded.crm_state END,
                       updated_at=excluded.updated_at""",
                (
                    event.lead_event_id,
                    event.company_id,
                    event.run_id,
                    _json(event.model_dump(mode="json")),
                    crm_state,
                    now,
                    now,
                ),
            )

    def update_lead_event(self, lead_event_id: str, **fields: Any) -> None:
        allowed = {"crm_state", "pipedrive_lead_id", "pipedrive_deal_id"}
        bad = set(fields) - allowed
        if bad or not fields:
            raise ValueError(f"unsupported lead event fields: {sorted(bad)}")
        assignments = ", ".join(f"{key}=?" for key in fields)
        with self.connection() as conn:
            conn.execute(
                f"UPDATE sales_lead_events SET {assignments}, updated_at=? WHERE lead_event_id=?",
                (*fields.values(), _now(), lead_event_id),
            )

    def get_lead_event(self, lead_event_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM sales_lead_events WHERE lead_event_id=?",
                (lead_event_id,),
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        return value

    def get_lead_event_by_pipedrive_id(self, pipedrive_lead_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM sales_lead_events WHERE pipedrive_lead_id=?",
                (pipedrive_lead_id,),
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        return value

    def upsert_recipient(self, recipient: RecipientSync) -> None:
        now = _now()
        with self.connection() as conn:
            prior = conn.execute("SELECT * FROM sales_recipients WHERE recipient_id=?", (recipient.recipient_id,)).fetchone()
            if prior and prior["normalized_email"] != recipient.email:
                conn.execute("INSERT INTO recipient_address_history VALUES (?,?,?,?)",
                    (recipient.recipient_id, prior["normalized_email"], _json(dict(prior)), now))
                conn.execute("""UPDATE sales_recipients SET verification_status='pending',
                    verification_policy_version='', verification_reason='',
                    warmy_prospect_id=NULL, pipedrive_person_id=NULL WHERE recipient_id=?""", (recipient.recipient_id,))
            conn.execute(
                """INSERT INTO sales_recipients(
                       recipient_id, company_id, person_id, normalized_email,
                       payload, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(recipient_id) DO UPDATE SET
                       normalized_email=excluded.normalized_email,
                       payload=excluded.payload,
                       updated_at=excluded.updated_at""",
                (
                    recipient.recipient_id,
                    recipient.company_id,
                    recipient.person_id,
                    recipient.email,
                    _json(recipient.model_dump(mode="json")),
                    now,
                    now,
                ),
            )

    def get_recipient(self, *, recipient_id: str = "", warmy_prospect_id: str = "") -> dict[str, Any] | None:
        if bool(recipient_id) == bool(warmy_prospect_id):
            raise ValueError("exactly one recipient lookup is required")
        column, value = (
            ("recipient_id", recipient_id)
            if recipient_id
            else ("warmy_prospect_id", warmy_prospect_id)
        )
        with self.connection() as conn:
            row = conn.execute(
                f"SELECT * FROM sales_recipients WHERE {column}=?",
                (value,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def recipient_email_count(self, company_id: str) -> int:
        with self.connection() as conn:
            row = conn.execute(
                """SELECT COUNT(DISTINCT LOWER(TRIM(normalized_email))) AS count
                     FROM sales_recipients
                    WHERE company_id=? AND TRIM(normalized_email) <> ''
                      AND verification_status!='invalid'
                      AND NOT EXISTS (SELECT 1 FROM suppressions s WHERE s.email=sales_recipients.normalized_email)""",
                (company_id,),
            ).fetchone()
        return int(row["count"])

    def update_recipient(self, recipient_id: str, **fields: Any) -> None:
        allowed = {
            "verification_status",
            "verification_policy_version",
            "verification_reason",
            "pipedrive_person_id",
            "warmy_prospect_id",
        }
        bad = set(fields) - allowed
        if bad or not fields:
            raise ValueError(f"unsupported recipient fields: {sorted(bad)}")
        assignments = ", ".join(f"{key}=?" for key in fields)
        with self.connection() as conn:
            conn.execute(
                f"UPDATE sales_recipients SET {assignments}, updated_at=? WHERE recipient_id=?",
                (*fields.values(), _now(), recipient_id),
            )

    def save_sequence(self, sequence: OutreachSequenceSync) -> None:
        now = _now()
        with self.connection(immediate=True) as conn:
            self._save_sequence(conn, sequence, now)

    def save_sequence_and_enqueue(
        self,
        sequence: OutreachSequenceSync,
        *,
        kind: str,
        dedupe_key: str,
        payload: dict[str, Any],
    ) -> bool:
        """Atomically refresh sequence input and reserve its durable work item.

        If the work key already exists, the sequence is left untouched.  This is
        essential after a worker has replaced Scout's unsubscribe placeholder with
        its provider-ready merge snapshot.  The immediate transaction also prevents
        concurrent ingesters or a worker from observing a half-ingested sequence.
        """
        now = _now()
        with self.connection(immediate=True) as conn:
            existing = conn.execute(
                "SELECT 1 FROM work_items WHERE dedupe_key=?",
                (dedupe_key,),
            ).fetchone()
            if existing:
                return False
            self._save_sequence(conn, sequence, now)
            cursor = conn.execute(
                """INSERT INTO work_items(
                       kind, dedupe_key, payload, run_after, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (kind, dedupe_key, _json(payload), now, now, now),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("failed to reserve sequence work item")
        return True

    def _save_sequence(
        self,
        conn: sqlite3.Connection,
        sequence: OutreachSequenceSync,
        now: str,
    ) -> None:
        prior = conn.execute(
            "SELECT approval_state, anchor_lead_event_id, primary_recipient_id, merge_hash FROM outreach_sequences WHERE sequence_id=?",
            (sequence.sequence_id,),
        ).fetchone()
        if prior and prior["approval_state"] != SequenceApprovalState.DRAFT.value:
            immutable = (
                prior["anchor_lead_event_id"],
                prior["primary_recipient_id"],
                prior["merge_hash"],
            )
            incoming = (
                sequence.anchor_lead_event_id,
                sequence.primary_recipient_id,
                sequence.merge_hash,
            )
            if immutable != incoming:
                raise ValueError("approved outreach sequence is immutable")
            # Approval freezes the complete send snapshot and its eligibility
            # evidence, not only the three fields checked above.
            return
        payload = sequence.model_dump(mode="json")
        conn.execute(
            """INSERT INTO outreach_sequences(
                   sequence_id, company_id, campaign_protocol,
                   anchor_lead_event_id, primary_recipient_id, merge_hash,
                   payload, eligibility_status, eligibility_reasons,
                   created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(sequence_id) DO UPDATE SET
                   anchor_lead_event_id=excluded.anchor_lead_event_id,
                   primary_recipient_id=excluded.primary_recipient_id,
                   merge_hash=excluded.merge_hash,
                   payload=excluded.payload,
                   eligibility_status=excluded.eligibility_status,
                   eligibility_reasons=excluded.eligibility_reasons,
                   updated_at=excluded.updated_at""",
            (
                sequence.sequence_id,
                sequence.company_id,
                sequence.campaign_protocol,
                sequence.anchor_lead_event_id,
                sequence.primary_recipient_id,
                sequence.merge_hash,
                _json(payload),
                sequence.eligibility_status.value,
                _json(sequence.eligibility_reasons),
                now,
                now,
            ),
        )
        conn.execute(
            "DELETE FROM outreach_sequence_events WHERE sequence_id=?",
            (sequence.sequence_id,),
        )
        conn.execute(
            "INSERT INTO outreach_sequence_events(sequence_id, lead_event_id, event_role) VALUES (?, ?, 'anchor')",
            (sequence.sequence_id, sequence.anchor_lead_event_id),
        )
        for lead_event_id in sorted(set(sequence.supporting_event_ids)):
            if lead_event_id == sequence.anchor_lead_event_id:
                continue
            conn.execute(
                "INSERT INTO outreach_sequence_events(sequence_id, lead_event_id, event_role) VALUES (?, ?, 'supporting')",
                (sequence.sequence_id, lead_event_id),
            )

    def get_sequence(self, sequence_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM outreach_sequences WHERE sequence_id=?",
                (sequence_id,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["eligibility_reasons"] = json.loads(result["eligibility_reasons"])
        return result

    def update_sequence(self, sequence_id: str, **fields: Any) -> None:
        allowed = {
            "approval_state",
            "eligibility_status",
            "eligibility_reasons",
            "approval_batch_id",
            "warmy_campaign_id",
            "warmy_mailbox_id",
            "pipedrive_deal_id",
            "reply_disposition",
            "reply_received_at",
            "merge_hash",
            "payload",
        }
        bad = set(fields) - allowed
        if bad or not fields:
            raise ValueError(f"unsupported sequence fields: {sorted(bad)}")
        normalized = {
            key: _json(value) if key in {"eligibility_reasons", "payload"} else value
            for key, value in fields.items()
        }
        assignments = ", ".join(f"{key}=?" for key in normalized)
        with self.connection() as conn:
            conn.execute(
                f"UPDATE outreach_sequences SET {assignments}, updated_at=? WHERE sequence_id=?",
                (*normalized.values(), _now(), sequence_id),
            )

    def get_sequence_for_prospect(
        self, prospect_id: str, campaign_id: str = ""
    ) -> dict[str, Any] | None:
        sql = """SELECT s.* FROM outreach_sequences s
                 JOIN sales_recipients r ON r.recipient_id=s.primary_recipient_id
                 WHERE r.warmy_prospect_id=?"""
        params: list[Any] = [prospect_id]
        if campaign_id:
            sql += " AND s.warmy_campaign_id=?"
            params.append(campaign_id)
        sql += " ORDER BY s.updated_at DESC LIMIT 1"
        with self.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["eligibility_reasons"] = json.loads(result["eligibility_reasons"])
        return result

    def get_sequence_for_event(self, lead_event_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """SELECT s.* FROM outreach_sequences s
                   JOIN outreach_sequence_events e ON e.sequence_id=s.sequence_id
                   WHERE e.lead_event_id=?
                   ORDER BY s.updated_at DESC LIMIT 1""",
                (lead_event_id,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["eligibility_reasons"] = json.loads(result["eligibility_reasons"])
        return result

    def sequence_events(self, sequence_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT e.event_role, l.*
                   FROM outreach_sequence_events e
                   JOIN sales_lead_events l ON l.lead_event_id=e.lead_event_id
                   WHERE e.sequence_id=?
                   ORDER BY CASE e.event_role WHEN 'anchor' THEN 0 ELSE 1 END,
                            l.lead_event_id""",
                (sequence_id,),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value["payload"])
            result.append(value)
        return result

    def save_approval_batch(self, batch: ApprovalBatch) -> None:
        if len(batch.sequence_ids) > batch.maximum_recipient_count:
            raise ValueError("approval batch exceeds its recipient ceiling")
        now = _now()
        with self.connection(immediate=True) as conn:
            existing = conn.execute(
                "SELECT payload FROM approval_batches WHERE batch_id=?",
                (batch.batch_id,),
            ).fetchone()
            serialized = _json(batch.model_dump(mode="json"))
            if existing and existing["payload"] != serialized:
                raise ValueError("approval batch is immutable")
            for sequence_id in batch.sequence_ids:
                row = conn.execute(
                    "SELECT merge_hash, eligibility_status FROM outreach_sequences WHERE sequence_id=?",
                    (sequence_id,),
                ).fetchone()
                if not row:
                    raise ValueError(f"approval references missing sequence {sequence_id}")
                if row["eligibility_status"] != "ready":
                    raise ValueError(f"sequence {sequence_id} is not ready")
                if batch.merge_hashes.get(sequence_id) != row["merge_hash"]:
                    raise ValueError(f"sequence {sequence_id} merge hash changed")
            conn.execute(
                """INSERT INTO approval_batches(
                       batch_id, campaign_id, campaign_manifest_hash, payload,
                       approved_by, approved_at, expires_at, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(batch_id) DO NOTHING""",
                (
                    batch.batch_id,
                    batch.campaign_id,
                    batch.campaign_manifest_hash,
                    serialized,
                    batch.approved_by,
                    batch.approved_at.astimezone(UTC).isoformat(),
                    batch.expires_at.astimezone(UTC).isoformat(),
                    now,
                ),
            )
            for sequence_id in batch.sequence_ids:
                conn.execute(
                    """UPDATE outreach_sequences
                       SET approval_state='approved', approval_batch_id=?, updated_at=?
                       WHERE sequence_id=? AND approval_state='draft'""",
                    (batch.batch_id, now, sequence_id),
                )

    def valid_approval_for_sequence(
        self,
        sequence_id: str,
        *,
        campaign_id: str,
        campaign_manifest_hash: str,
    ) -> bool:
        now = _now()
        with self.connection() as conn:
            row = conn.execute(
                """SELECT s.merge_hash, s.approval_state, b.payload
                   FROM outreach_sequences s
                   JOIN approval_batches b ON b.batch_id=s.approval_batch_id
                   WHERE s.sequence_id=? AND b.campaign_id=?
                     AND b.campaign_manifest_hash=? AND b.revoked_at IS NULL
                     AND b.expires_at>?""",
                (sequence_id, campaign_id, campaign_manifest_hash, now),
            ).fetchone()
        if not row or row["approval_state"] != SequenceApprovalState.APPROVED.value:
            return False
        batch = ApprovalBatch.model_validate(json.loads(row["payload"]))
        return (
            sequence_id in batch.sequence_ids
            and batch.merge_hashes.get(sequence_id) == row["merge_hash"]
            and len(batch.sequence_ids) <= batch.maximum_recipient_count
        )

    def cache_email_verification(
        self,
        email: str,
        policy_version: str,
        status: str,
        reason: str,
        provider_payload: dict[str, Any],
    ) -> None:
        normalized = email.strip().casefold()
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO email_verifications(
                       normalized_email, policy_version, status, reason,
                       provider_payload, verified_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(normalized_email, policy_version) DO UPDATE SET
                       status=excluded.status, reason=excluded.reason,
                       provider_payload=excluded.provider_payload,
                       verified_at=excluded.verified_at""",
                (normalized, policy_version, status, reason, _json(provider_payload), _now()),
            )

    def get_email_verification(self, email: str, policy_version: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """SELECT status, reason, provider_payload, verified_at
                   FROM email_verifications
                   WHERE normalized_email=? AND policy_version=?""",
                (email.strip().casefold(), policy_version),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["provider_payload"] = json.loads(result["provider_payload"])
        return result

    def record_eligibility_decision(
        self,
        decision_id: str,
        subject_type: str,
        subject_id: str,
        status: str,
        reasons: list[str],
        evidence: dict[str, Any] | None = None,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO eligibility_decisions(
                       decision_id, subject_type, subject_id, status, reasons,
                       evidence, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision_id,
                    subject_type,
                    subject_id,
                    status,
                    _json(reasons),
                    _json(evidence or {}),
                    _now(),
                ),
            )

    def supersede_work(self, work_id: int, reason: str) -> bool:
        now = _now()
        with self.connection(immediate=True) as conn:
            cursor = conn.execute(
                """UPDATE work_items
                   SET status='superseded', completed_at=?, lease_owner=NULL,
                       lease_until=NULL, last_error=?, updated_at=?
                   WHERE id=? AND status IN ('pending', 'running')""",
                (now, reason[:1000], now, work_id),
            )
        return cursor.rowcount > 0
