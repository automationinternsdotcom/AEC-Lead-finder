"""In-memory database implementing the service contract for tests."""

from __future__ import annotations

import hashlib
from typing import Any

from .config import ActivationBlocked
from .models import MappingRecord, WorkItem


class MemoryDatabase:
    def __init__(self):
        self.webhooks: set[tuple[str, str]] = set()
        self.work: dict[str, WorkItem] = {}
        self.completed: set[int] = set()
        self.mappings: dict[str, MappingRecord] = {}
        self.suppressions: dict[str, dict[str, str]] = {}
        self.tokens: dict[str, str] = {}
        self.operations: dict[tuple[str, str], dict[str, Any]] = {}
        self.state: dict[str, dict[str, Any]] = {}
        self.sent_messages: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self._next_id = 1

    def healthcheck(self) -> bool:
        return True

    def enqueue_work(self, kind, dedupe_key, payload, *, run_after=None):
        if dedupe_key in self.work:
            return False
        self.work[dedupe_key] = WorkItem(
            id=self._next_id,
            kind=kind,
            dedupe_key=dedupe_key,
            payload=payload,
        )
        self._next_id += 1
        return True

    def accept_webhook(
        self, provider, event_id, event_type, payload, payload_hash, *, signature_valid
    ):
        key = (provider, event_id)
        if key in self.webhooks:
            return False
        self.webhooks.add(key)
        self.enqueue_work(
            f"{provider}.event",
            f"{provider}:event:{event_id}",
            {"event_id": event_id, **payload, "event_type": event_type},
        )
        return True

    def claim_work(self, owner, limit, lease_seconds):
        return [item for item in self.work.values() if item.id not in self.completed][
            :limit
        ]

    def complete_work(self, item_id, owner):
        self.completed.add(item_id)

    def retry_work(self, item, owner, error, *, max_attempts):
        item.attempt_count += 1
        return item.attempt_count >= max_attempts

    def defer_work(self, item, owner, error, *, delay_seconds=300):
        item.attempt_count = max(0, item.attempt_count - 1)

    def replay_dead_letters(self):
        return 0

    def upsert_mapping(self, mapping: MappingRecord):
        current = self.mappings.get(mapping.outreach_id)
        if current:
            data = current.model_dump()
            for key, value in mapping.model_dump().items():
                if value is not None and value != "":
                    data[key] = value
            mapping = MappingRecord.model_validate(data)
        self.mappings[mapping.outreach_id] = mapping

    def get_mapping(self, **lookup):
        if len(lookup) != 1:
            raise ValueError("one lookup required")
        key, value = next(iter(lookup.items()))
        for mapping in self.mappings.values():
            if getattr(mapping, key) == value:
                return mapping
        return None

    def get_mappings_by_email(self, email):
        normalized = email.strip().casefold()
        return [
            mapping for mapping in self.mappings.values() if mapping.email == normalized
        ]

    def update_mapping(self, outreach_id, **fields):
        mapping = self.mappings[outreach_id]
        self.mappings[outreach_id] = mapping.model_copy(update=fields)

    def suppress(self, email, reason, source, *, external_event_id=""):
        normalized = email.strip().casefold()
        inserted = normalized not in self.suppressions
        self.suppressions[normalized] = {
            "reason": str(reason),
            "source": source,
            "external_event_id": external_event_id,
        }
        return inserted

    def is_suppressed(self, email):
        return email.strip().casefold() in self.suppressions

    def store_unsubscribe_token(self, token_id, email):
        self.tokens[hashlib.sha256(token_id.encode()).hexdigest()] = (
            email.strip().casefold()
        )

    def resolve_unsubscribe_token(self, token_id):
        return self.tokens.get(hashlib.sha256(token_id.encode()).hexdigest())

    def claim_operation(
        self, provider, operation_key, payload, *, owner="", lease_seconds=300
    ):
        key = (provider, operation_key)
        if key in self.operations and self.operations[key]["status"] != "failed":
            return False
        self.operations[key] = {
            "status": "started",
            "request": payload,
            "owner": owner,
        }
        return True

    def get_operation(self, provider, operation_key):
        return self.operations.get((provider, operation_key))

    def complete_operation(self, provider, operation_key, response, *, external_id=""):
        self.operations[(provider, operation_key)].update(
            status="completed", response=response, external_id=external_id
        )

    def fail_operation(self, provider, operation_key, error):
        self.operations[(provider, operation_key)].update(
            status="failed", error=str(error)
        )

    def get_state(self, key):
        return self.state.get(key)

    def set_state(self, key, value):
        self.state[key] = value

    def valid_frozen_send_approval(self, campaign_id, campaign_manifest_hash, **kwargs):
        """Mirror the real DB contract; legacy test routes fail closed."""
        raise ActivationBlocked("frozen recipient-specific send approval is required")

    def record_sent_message(self, message_id, content_hash, recipient_id, sent_at, *, provider=""):
        self.sent_messages[message_id] = {
            "content_hash": content_hash,
            "recipient_id": recipient_id,
            "sent_at": sent_at,
            "provider": provider,
        }

    def has_sent_message(self, message_id, recipient_id, sent_at):
        item = self.sent_messages.get(message_id)
        return bool(item and item["recipient_id"] == recipient_id and item["sent_at"] == sent_at)

    def record_run(self, run_id, source_file, discovered_count, enqueued_count):
        self.runs[run_id] = {
            "source_file": source_file,
            "discovered_count": discovered_count,
            "enqueued_count": enqueued_count,
        }
