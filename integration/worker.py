"""Bounded Mac-local worker for durable integration jobs."""

from __future__ import annotations

import argparse
import logging
import os
import socket
import uuid
from datetime import UTC, datetime

from .config import ActivationBlocked, Settings
from .database import Database
from .workflows import SalesWorkflows
from .providers import ProviderError

LOG = logging.getLogger(__name__)


def run_once(
    settings: Settings, db=None, workflows=None, *, enqueue_gmail_sync=False
) -> int:
    db = db or Database(settings.database_path)
    workflows = workflows or SalesWorkflows(settings, db)
    owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    if (
        enqueue_gmail_sync
        and settings.gmail_reply_forwarding_enabled
        and settings.gmail_service_account_json
    ):
        minute = datetime.now(UTC).strftime("%Y%m%dT%H%M")
        db.enqueue_work(
            "gmail.sync",
            f"gmail:sync:{settings.gmail_forward_to}:{minute}",
            {"mailbox": settings.gmail_forward_to},
        )
    items = db.claim_work(
        owner, settings.worker_batch_size, settings.worker_lease_seconds
    )
    completed = 0
    try:
        for item in items:
            try:
                workflows.handle(item)
            except ActivationBlocked as error:
                db.defer_work(item, owner, error)
                LOG.warning(
                    "work item deferred by activation gate",
                    extra={"work_item_id": item.id, "kind": item.kind},
                )
                continue
            except Exception as error:
                if isinstance(error, ProviderError) and error.status_code == 429:
                    db.defer_work(item, owner, error, delay_seconds=60)
                    LOG.warning("provider throttled; deferred without consuming retry budget", extra={"work_item_id": item.id})
                    continue
                terminal = db.retry_work(
                    item,
                    owner,
                    error,
                    max_attempts=settings.max_attempts,
                )
                LOG.exception(
                    "work item failed",
                    extra={
                        "work_item_id": item.id,
                        "kind": item.kind,
                        "terminal": terminal,
                    },
                )
                if (
                    terminal
                    and settings.provider_writes_enabled
                    and settings.alert_email
                ):
                    try:
                        workflows.gmail.send_text(
                            settings.gmail_forward_to,
                            settings.alert_email,
                            f"Aether sales integration dead letter: {item.kind}",
                            (
                                f"Work item {item.id} reached the retry limit.\n"
                                f"Kind: {item.kind}\n"
                                f"Error: {str(error)[:500]}\n\n"
                                "Sent by Codex on the user's behalf."
                            ),
                        )
                    except Exception:
                        LOG.exception("failed to deliver dead-letter alert")
                continue
            db.complete_work(item.id, owner)
            completed += 1
    finally:
        workflows.close()
    return completed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--enqueue-gmail-sync", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    completed = run_once(settings, enqueue_gmail_sync=args.enqueue_gmail_sync)
    LOG.info("worker completed", extra={"completed": completed})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
