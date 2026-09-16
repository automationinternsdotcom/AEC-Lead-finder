# Send-gate promotion runbook (review-only)

This is a concrete promotion procedure, not an instruction to deploy now. The
production Warmy campaign remains paused and the scheduled automation remains
unchanged. The runtime copy is not a Git checkout; the source checkout also
contains unrelated user edits, so never use `git reset`, `git checkout`, or a
whole-tree sync.

## Stage and verify without touching either target

Run from the reviewed clean worktree (`/tmp/aec-lead-gate.PB56vL`). The gate
closure includes `integration/config.py`, `integration/campaign.py`, the
recipient/company guard, subject policy, variant reader, recipient policy, and
variant configuration; do not stage only the visibly edited gate module:

```bash
set -euo pipefail
SRC=/tmp/aec-lead-gate.PB56vL
STAGE=$(mktemp -d /tmp/aether-send-gate.XXXXXX)
mkdir -p "$STAGE/integration" "$STAGE/docs"
for f in models.py database.py providers.py workflows.py cli.py send_gate.py next_five.py memory.py config.py campaign.py recipient_guard.py subjects.py variants.py; do
  cp -p "$SRC/integration/$f" "$STAGE/integration/$f"
done
mkdir -p "$STAGE/config"
cp -p "$SRC/recipient_policy.py" "$STAGE/recipient_policy.py"
cp -p "$SRC/config/aether_subject_variants.json" "$STAGE/config/"
cp -p "$SRC/docs/send-approval-gate.md" "$SRC/docs/send-approval-gate-deployment.md" "$STAGE/docs/"
(cd "$STAGE" && PYTHONPATH="$STAGE:$SRC" uv run --project "$SRC" pytest -q "$SRC/tests/test_send_gate.py" "$SRC/tests/test_next_five.py")
```

The exact-five preview contains contact data and is retained in the protected
`/Users/openclaw/Code/lead-enrichment/data/warmysender/exports/` area. It is
not copied into a source or runtime tree by this runbook; generate a fresh
HOLD preview there from the reviewed JSON when the operator is ready.

Before promotion, compare only the staged files against both targets. A
missing target file is a required review item, not permission to copy the
entire repository:

```bash
TARGETS=(/Users/openclaw/Code/AEC-Lead-finder-runtime /Users/openclaw/Code/AEC-Lead-finder)
for target in "${TARGETS[@]}"; do
  for f in integration/models.py integration/database.py integration/providers.py integration/workflows.py integration/cli.py integration/send_gate.py integration/next_five.py integration/memory.py integration/config.py integration/campaign.py integration/recipient_guard.py integration/subjects.py integration/variants.py recipient_policy.py config/aether_subject_variants.json docs/send-approval-gate.md docs/send-approval-gate-deployment.md; do
    test -e "$target/$f" && shasum -a 256 "$target/$f" || echo "MISSING $target/$f"
    shasum -a 256 "$STAGE/$f"
  done
done
```

## Promotion after explicit review

After the checks above and an explicit operator approval, stop/pause both
worker paths, back up the listed target files, then copy only the staged files
with `rsync --checksum`. Preserve each target's `.env`, deployed environment
settings, SQLite database plus `-wal`/`-shm`, credentials, launchd wrapper,
and runtime-only files. Resume neither sender until fresh UI read evidence and
Gmail MIME evidence have been recorded.

```bash
export AETHER_GATE_PROMOTION_APPROVED=1
test "$AETHER_GATE_PROMOTION_APPROVED" = 1
RUNTIME=/Users/openclaw/Code/AEC-Lead-finder-runtime
CHECKOUT=/Users/openclaw/Code/AEC-Lead-finder
BACKUP=$(mktemp -d /tmp/aether-send-gate-backup.XXXXXX)
for target in "$RUNTIME" "$CHECKOUT"; do
  mkdir -p "$target/integration" "$target/docs" "$target/config"
  mkdir -p "$BACKUP$(dirname "$target")"
  for f in integration/models.py integration/database.py integration/providers.py integration/workflows.py integration/cli.py integration/send_gate.py integration/next_five.py integration/memory.py integration/config.py integration/campaign.py integration/recipient_guard.py integration/subjects.py integration/variants.py recipient_policy.py config/aether_subject_variants.json docs/send-approval-gate.md docs/send-approval-gate-deployment.md; do
    mkdir -p "$BACKUP$(dirname "$target/$f")"
    test ! -e "$target/$f" || cp -p "$target/$f" "$BACKUP$target/$f"
  done
  rsync -a --checksum --no-owner --no-group \
    "$STAGE/integration/" "$target/integration/"
  rsync -a --checksum --no-owner --no-group \
    "$STAGE/docs/" "$target/docs/"
  rsync -a --checksum --no-owner --no-group \
    "$STAGE/config/" "$target/config/"
  rsync -a --checksum --no-owner --no-group \
    "$STAGE/recipient_policy.py" "$target/recipient_policy.py"
done
```

The source checkout's dirty diff must be reviewed before the second target is
promoted. If it cannot be reconciled safely, promote neither target and use a
dedicated clean checkout for the launchd path. Do not alter the automation TOML
or enable `CAMPAIGN_START_ENABLED` as part of this code promotion.

## Post-promotion gate sequence

The trusted operator/agent must actually acquire the Warmy UI read and Gmail
message before invoking these commands; the CLI only validates persisted
evidence and does not fetch either service:

```bash
uv run python -m integration.cli record-live-read <fresh-ui-read.json> --apply
uv run python -m integration.cli record-render-evidence <gmail-mime-evidence.json> --apply
uv run python -m integration.cli start-campaign --apply
```

`start-campaign` independently rereads the provider API, checks the UI
attestation and received MIME binding, reserves the global daily five-send
ledger atomically, and only then issues the provider start request.

The 13:00 UTC release heartbeat must use a pre-start UI snapshot with the
approved `schedule_date` and `08:00`–`09:00` window fields; it must not invent a
draft `next_send_at` that Warmy has not computed. A separate 14:00 UTC
verification heartbeat must reread the post-start queue/activity and contain
the unsent batch (pause the dedicated campaign) if any queued or computed send
time falls outside the approved date/window. This is containment for the
single-recipient workaround, not proof that the remote Warmy scheduler is
intercepted or that the route scales to the legacy 297-lead campaign.
