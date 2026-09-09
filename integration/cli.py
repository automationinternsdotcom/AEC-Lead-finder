"""Operator CLI. Mutating provider commands always require --apply and activation flags."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .campaign import campaign_manifest_hash, load_campaign
from .config import ActivationBlocked, Settings
from .database import Database
from .handoff import enqueue_handoff, load_handoff
from .legacy_reconcile import apply_legacy_swvp_local, legacy_swvp_plan
from .models import ApprovalBatch
from .providers import WarmyClient
from .provisioning import apply as apply_provisioning
from .provisioning import plan as provisioning_plan
from .scout_bridge import contacts_from_csv


def _json(value) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _campaign_data(response: Any) -> dict[str, Any]:
    """Normalize Warmy campaign responses that may be wrapped in ``data``."""
    data = response if isinstance(response, dict) else {}
    if isinstance(data.get("data"), dict):
        data = data["data"]
    if isinstance(data.get("campaign"), dict):
        data = data["campaign"]
    return data


def _verify_campaign_draft(
    warmy: WarmyClient,
    created_response: dict[str, Any],
    expected_manifest_hash: str,
    expected_manifest: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    created = _campaign_data(created_response)
    campaign_id = str(created.get("id") or "").strip()
    if not campaign_id:
        raise ActivationBlocked("Warmy campaign draft response did not include an ID")

    readback_response = warmy.get_campaign(campaign_id)
    readback = _campaign_data(readback_response)
    errors: list[str] = []
    if str(readback.get("id") or "").strip() != campaign_id:
        errors.append("campaign ID mismatch")
    status = str(readback.get("status") or "").casefold()
    if status not in {"draft", "paused"}:
        errors.append(f"unsafe campaign status {status or '(missing)'}")

    mailbox_values = readback.get("mailboxIds")
    if mailbox_values is None and "mailboxes" in readback:
        mailbox_values = readback.get("mailboxes")
    mailbox_ids = {
        str(item.get("id") if isinstance(item, dict) else item)
        for item in mailbox_values or []
    }
    if "mailboxIds" in readback or "mailboxes" in readback:
        expected_mailbox_ids = {
            str(item.get("id") if isinstance(item, dict) else item)
            for item in (expected_manifest or {}).get("mailboxIds") or []
        }
        mailbox_mismatch = mailbox_ids != expected_mailbox_ids
        if mailbox_mismatch:
            errors.append("campaign mailbox set mismatch")
        mailbox_verification = {
            "status": "mismatch" if mailbox_mismatch else "verified",
            "requires_ui_check": False,
        }
    else:
        mailbox_verification = {
            "status": "not_returned",
            "requires_ui_check": True,
            "reason": "Warmy readback omitted mailboxIds/mailboxes; verify in the UI",
        }

    # Warmy omits mailbox IDs from this read endpoint. Fill only that omitted
    # field from the submitted manifest so the approval hash remains the exact
    # hash of the config, while reporting mailbox verification separately.
    hash_payload = dict(readback)
    if "mailboxIds" not in hash_payload and "mailboxes" not in hash_payload:
        if expected_manifest is None:
            errors.append("campaign mailbox IDs unavailable for manifest hash verification")
        else:
            hash_payload["mailboxIds"] = expected_manifest.get("mailboxIds", [])
    elif "mailboxIds" not in hash_payload:
        hash_payload["mailboxIds"] = mailbox_values
    if campaign_manifest_hash(hash_payload) != expected_manifest_hash:
        errors.append("campaign manifest hash mismatch")
    if errors:
        raise ActivationBlocked("Warmy campaign draft verification failed: " + ", ".join(errors))
    return campaign_id, readback_response, mailbox_verification


def _verify_campaign_signature_payload(
    campaign: dict[str, Any], expected_logo_url: str
) -> dict[str, Any]:
    steps = campaign.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ActivationBlocked("Warmy campaign has no steps")
    signature_parts = (
        "Jordan Whitehurst, Partner",
        "Aether Facility Services, LLC",
        "O: (602) 612-6393",
        "M: (813) 992-0858",
        "2120 W Encanto Blvd, Phoenix, AZ 85009",
    )
    missing_logo_steps: list[int] = []
    missing_signature_steps: list[int] = []
    for index, step in enumerate(steps):
        body_html = str(step.get("bodyHtml") or "")
        step_index = int(step.get("stepIndex", index))
        if expected_logo_url not in body_html:
            missing_logo_steps.append(step_index)
        if not all(part in body_html for part in signature_parts):
            missing_signature_steps.append(step_index)
    if missing_logo_steps or missing_signature_steps:
        errors: list[str] = []
        if missing_logo_steps:
            errors.append(f"missing logo in steps {missing_logo_steps}")
        if missing_signature_steps:
            errors.append(f"missing signature in steps {missing_signature_steps}")
        raise ActivationBlocked("Warmy signature verification failed: " + ", ".join(errors))
    return {
        "status": "verified",
        "steps": len(steps),
        "signature_logo_url": expected_logo_url,
        "track_opens": bool(campaign.get("trackOpens")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    enqueue = commands.add_parser("enqueue-contacts")
    enqueue.add_argument("csv")
    enqueue.add_argument("--run-id", default="")

    enqueue_handoff_command = commands.add_parser("enqueue-handoff")
    enqueue_handoff_command.add_argument("handoff")

    validate_handoff = commands.add_parser("validate-handoff")
    validate_handoff.add_argument("handoff")

    approve_batch = commands.add_parser("approve-batch")
    approve_batch.add_argument("batch")
    approve_batch.add_argument("--apply", action="store_true")

    reconcile = commands.add_parser("reconcile-csv")
    reconcile.add_argument("csv")
    reconcile.add_argument("--run-id", default="reconcile-preview")

    reconcile_swvp = commands.add_parser("reconcile-legacy-swvp")
    reconcile_swvp.add_argument("--apply-local", action="store_true")

    provision = commands.add_parser("provision")
    provision.add_argument("--apply", action="store_true")

    campaign = commands.add_parser("create-campaign-draft")
    campaign.add_argument("manifest")
    campaign.add_argument("--apply", action="store_true")

    update_campaign = commands.add_parser("update-campaign")
    update_campaign.add_argument("manifest")
    update_campaign.add_argument("--campaign-id", default="")
    update_campaign.add_argument("--apply", action="store_true")

    verify_campaign = commands.add_parser("verify-campaign-signature")
    verify_campaign.add_argument("--campaign-id", default="")

    start = commands.add_parser("start-campaign")
    start.add_argument("--apply", action="store_true")

    commands.add_parser("doctor")
    commands.add_parser("replay-dead-letters")
    args = parser.parse_args()
    settings = Settings.from_env()

    if args.command == "validate-handoff":
        handoff = load_handoff(args.handoff)
        _json(
            {
                "valid": True,
                "run_id": handoff.run_id,
                "content_hash": handoff.content_hash,
                "companies": len(handoff.companies),
                "lead_events": len(handoff.lead_events),
                "recipients": len(handoff.recipients),
                "sequences": len(handoff.sequences),
            }
        )
        return 0
    if args.command == "enqueue-handoff":
        _json(enqueue_handoff(Database(settings.database_path), args.handoff))
        return 0
    if args.command == "approve-batch":
        batch = ApprovalBatch.model_validate_json(
            Path(args.batch).read_text(encoding="utf-8")
        )
        if not args.apply:
            _json({"valid": True, "would_approve": batch.model_dump(mode="json")})
            return 0
        db = Database(settings.database_path)
        db.save_approval_batch(batch)
        enqueued = 0
        for sequence_id in batch.sequence_ids:
            if db.enqueue_work(
                "warmy.sequence.enroll",
                f"warmy:sequence:enroll:{batch.batch_id}:{sequence_id}",
                {"sequence_id": sequence_id, "approval_batch_id": batch.batch_id},
            ):
                enqueued += 1
        _json(
            {
                "approved": batch.batch_id,
                "sequences": batch.sequence_ids,
                "enrollment_jobs": enqueued,
            }
        )
        return 0

    if args.command == "doctor":
        _json(
            {
                "database_path": settings.database_path,
                "database_ready": Database(settings.database_path).healthcheck(),
                "warmy_key_configured": bool(settings.warmy_api_key),
                "pipedrive_configured": bool(
                    settings.pipedrive_api_token and settings.pipedrive_domain
                ),
                "gmail_delegation_configured": bool(
                    settings.gmail_service_account_json
                ),
                "gmail_reply_forwarding_enabled": (
                    settings.gmail_reply_forwarding_enabled
                ),
                "provider_writes_enabled": settings.provider_writes_enabled,
                "campaign_activation_ready": settings.campaign_activation_ready,
                "campaign_activation_missing": settings.campaign_activation_missing(),
                "campaign_enrollment_ready": settings.campaign_enrollment_ready,
                "campaign_enrollment_missing": settings.campaign_enrollment_missing(),
            }
        )
        return 0
    if args.command == "replay-dead-letters":
        replayed = Database(settings.database_path).replay_dead_letters()
        _json({"replayed": replayed})
        return 0
    if args.command == "reconcile-csv":
        contacts = contacts_from_csv(args.csv, args.run_id)
        ids = [item.outreach_id for item in contacts]
        emails = [item.email for item in contacts]
        _json(
            {
                "rows_ready": len(contacts),
                "unique_outreach_ids": len(set(ids)),
                "duplicate_outreach_ids": len(ids) - len(set(ids)),
                "unique_emails": len(set(emails)),
                "duplicate_emails": len(emails) - len(set(emails)),
            }
        )
        return 0
    if args.command == "reconcile-legacy-swvp":
        _json(
            apply_legacy_swvp_local(settings.database_path)
            if args.apply_local
            else legacy_swvp_plan(settings.database_path)
        )
        return 0
    if args.command == "provision":
        _json(
            apply_provisioning(settings) if args.apply else provisioning_plan(settings)
        )
        return 0
    if args.command == "create-campaign-draft":
        manifest = load_campaign(args.manifest, settings)
        manifest_hash = campaign_manifest_hash(manifest)
        if not args.apply:
            _json(
                {
                    "manifest": manifest.model_dump(mode="json"),
                    "manifest_hash": manifest_hash,
                }
            )
            return 0
        settings.require_provider_writes()
        warmy = WarmyClient(settings)
        try:
            operation_key = f"aether-campaign-evergreen-v1:{manifest_hash}"
            created = warmy.create_campaign(
                manifest.model_dump(mode="json"), operation_key
            )
            campaign_id, verified, mailbox_verification = _verify_campaign_draft(
                warmy,
                created,
                manifest_hash,
                manifest.model_dump(mode="json"),
            )
            _json(
                {
                    "campaign_id": campaign_id,
                    "manifest_hash": manifest_hash,
                    "campaign": verified,
                    "mailbox_verification": mailbox_verification,
                }
            )
        finally:
            warmy.close()
        return 0
    if args.command == "update-campaign":
        manifest = load_campaign(args.manifest, settings)
        manifest_hash = campaign_manifest_hash(manifest)
        campaign_id = (args.campaign_id or settings.warmy_campaign_id).strip()
        if not campaign_id:
            raise ActivationBlocked("WARMY_CAMPAIGN_ID is required")
        if not args.apply:
            _json(
                {
                    "would_update": campaign_id,
                    "manifest": manifest.model_dump(mode="json"),
                    "manifest_hash": manifest_hash,
                }
            )
            return 0
        settings.require_provider_writes()
        warmy = WarmyClient(settings)
        try:
            operation_key = f"aether-campaign-update-v1:{campaign_id}:{manifest_hash}"
            warmy.update_campaign(
                campaign_id,
                manifest.model_dump(mode="json"),
                operation_key,
            )
            campaign_id, verified, mailbox_verification = _verify_campaign_draft(
                warmy,
                {"data": {"id": campaign_id}},
                manifest_hash,
                manifest.model_dump(mode="json"),
            )
            _json(
                {
                    "campaign_id": campaign_id,
                    "manifest_hash": manifest_hash,
                    "campaign": verified,
                    "mailbox_verification": mailbox_verification,
                }
            )
        finally:
            warmy.close()
        return 0
    if args.command == "verify-campaign-signature":
        campaign_id = (args.campaign_id or settings.warmy_campaign_id).strip()
        if not campaign_id:
            raise ActivationBlocked("WARMY_CAMPAIGN_ID is required")
        warmy = WarmyClient(settings)
        try:
            response = warmy.get_campaign(campaign_id)
            campaign = _campaign_data(response)
            _json(
                {
                    "campaign_id": campaign_id,
                    **_verify_campaign_signature_payload(
                        campaign, settings.signature_logo_url
                    ),
                }
            )
        finally:
            warmy.close()
        return 0
    if args.command == "start-campaign":
        if not args.apply:
            _json(
                {
                    "would_start": settings.warmy_campaign_id,
                    "ready": settings.campaign_activation_ready,
                }
            )
            return 0
        settings.require_campaign_activation()
        warmy = WarmyClient(settings)
        try:
            _json(
                warmy.start_campaign(
                    settings.warmy_campaign_id, "aether-campaign-start-v1"
                )
            )
        finally:
            warmy.close()
        return 0

    if args.command == "enqueue-contacts":
        raise ValueError(
            "contacts.csv is analytical only; use enqueue-handoff with the hashed sales_handoff.json artifact"
        )
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001 - CLI boundary renders a concise error
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
