# Aether AEC Nightly Automation

## Daily Grok → Pipedrive → Warmy ingestion

The current daily automation runs `bash run-daily-ingest.sh` at 02:00 America/Phoenix.
It uses Grok 4.3 for qualification/research and Treg only after Grok, under the shared
$5 monthly cap. The persistent sales DB stores the last ingested date and the active
run, so an interrupted run resumes and missed dates are covered. Each new window
overlaps the checkpoint by two days to catch late articles.

`integration.daily` validates the typed handoff, syncs Pipedrive events and people,
creates/updates Warmy contacts with the sourced why-lines, then (when explicitly
requested) appends them to the configured canonical Warmy prospect list. This
list-only operation does not approve, directly enroll, or start a campaign. A
provider campaign linked to this list may still natively autosync new members;
that provider behavior is a separate campaign-enrollment risk and production
remains paused. It
uses Grok to confirm same-company/same-project matches against prior CRM events
and reuses their original Lead IDs. Uncertain matches stay eligible as new events.
The match inputs and decisions are cached in the sales DB for audit and retries. It
preserves existing enrolled/replied sequences and their recipient data. Draft
ingestion does not require Pipedrive reply automations to be live; live campaign
activation retains every existing activation requirement.

### Canonical Warmy list collection

Set `WARMY_PROSPECT_LIST_ID` to the stable list ID obtained from an operator-reviewed
Warmy configuration and set `WARMY_LIST_SYNC_ENABLED=true` in every worker
environment. With `--enroll-draft`, each successfully upserted prospect is
sent through the documented `POST /api/v1/prospects` list route with `listId` and
`enroll:false`; the provider's explicit `list.attached:true` acknowledgement and
matching `listId` are required. The operation is idempotent by email and sends no
email or direct enrollment request, and does not start or resume a campaign. A
provider's native list-linked autosync, if configured, must be considered separately.
The full
custom-field snapshot is included on both new and existing-prospect upserts so an
existing contact's source and personalization fields are not silently unset.

List collection does not use campaign approval or campaign status as its gate. A
separate activation preflight must verify that the intended campaign is paused or
draft and is list-only. At activation, the fresh provider prospect read must show
the configured canonical list in `listMemberships`; exact campaign membership still
requires the existing audited Warmy UI attestation because the campaign readback
does not expose a supported exact-audience contract. If either read is unavailable,
activation remains held. Do not infer campaign membership from a local list ID or a
successful prospect upsert.

Operationally, provide the reviewed ID only for the list-collection run:

```bash
WARMY_PROSPECT_LIST_ID=<reviewed-list-id> WARMY_LIST_SYNC_ENABLED=true \
  uv run python -m integration.daily --enroll-draft \
  --handoff /absolute/path/to/sales_handoff.json
```

Keep `WARMY_ENROLLMENT_ENABLED=false` and `CAMPAIGN_START_ENABLED=false` for this
run. A later activation command must perform its own provider/UI audience
attestation and send-gate checks; this ingestion command cannot authorize that
activation.

Treg budget/provider deferrals create durable `treg_enrichment_deferred` reviews;
they do not abort export or ingestion of contacts already found. Completed Grok
no-email searches are cached within each run. After the provider/balance recovers,
retry the original dates and run ID with `--retry-review` to recover the backlog.

MapsData and Costar tenant data are available as explicit discovery add-ons. They
are not enabled by default in the daily automation. To include them, place completed
exports on the runtime machine, set `MAPSDATA_CSV` or `MAPSDATA_JOB_ID`/`MAPSDATA_KEY`
and `COSTAR_TENANT_CSV` in the runtime `.env`, then append `--mapsdata` and/or
`--costar` to the automation command. The pipeline reads completed MapsData jobs or
CSV exports only; it does not start new MapsData scrapes during the nightly run.
Costar rows must be operator-reviewed tenant exports, not browser-scraped session
data. Imported rows are preserved under the run's raw artifacts before qualification.

For a larger batch that should wait for a later Grok pass, use the separate raw
intake queue. WarmySender lead-database exports are supported in the same command
as MapsData, Sales Navigator, and Costar exports. This keeps provider collection
separate from qualification and never enrolls or sends records:

```bash
uv run python -m scout.raw_leads \
  --source mapsdata=/secure/exports/mapsdata-arizona.csv \
  --source warmysender=/secure/exports/warmysender-aec.csv \
  --output results/raw-leads/$(date +%F)/raw_leads.csv
```

Keep exports in a runtime-only directory outside git. The OpenClaw machine should
run this command after an operator downloads the completed exports, then leave the
queue at `raw_pending_grok` until the approved enrichment run is scheduled.

The reviewed existing draft fingerprint is pinned in
`config/daily_campaign_fingerprint.json`; template/settings drift stops enrollment
until the change is reviewed. This daily pin does not alter the production sending
activation flags or older approval batches.

For a completed backfill, use:

```bash
bash run-daily-ingest.sh --handoff /absolute/path/to/sales_handoff.json --until 2026-09-07
```

Production scheduling points to a deployed code snapshot at
`/Users/openclaw/Code/AEC-Lead-finder-runtime`, outside main and outside disposable
Codex worktrees. Credentials and SQLite/results remain in
`/Users/openclaw/Code/AEC-Lead-finder`. The existing Codex automation is updated in
place; do not also install the legacy nightly LaunchAgent below.

Keep the OpenClaw MacBook automation instructions in
`handoff/oc-macbook-automation-instructions.md` current whenever the runtime command,
environment, branch, or provider gates change.

## Legacy nightly wrapper

This repository now follows the same headless automation pattern as
`gps-grok-leadfinder`.

The nightly command is:

```bash
uv run scout/pipeline.py
```

`run-nightly.sh` is a thin wrapper around that command. It loads `.env` when present,
writes a timestamped log under `logs/`, and exits with the same status as the scout
pipeline.

## Files

- `run-nightly.sh` — local nightly wrapper.
- `com.aether.nightly.plist` — launchd LaunchAgent template.
- `logs/` — generated run logs, ignored by git.
- `results/YYYY-MM-DD/` — generated CSV and HTML reports, ignored by git.
- `scout.db` — SQLite state, ignored by git.

## Enable Locally

1. Confirm `.env` contains the Responses API settings from `README.md`.
2. Run a manual smoke test:

   ```bash
   ./run-nightly.sh --max-articles 5
   ```

3. Install the LaunchAgent when the smoke test is good:

   ```bash
   cp com.aether.nightly.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aether.nightly.plist
   launchctl enable gui/$(id -u)/com.aether.nightly
   ```

## Disable

```bash
launchctl bootout gui/$(id -u)/com.aether.nightly
rm ~/Library/LaunchAgents/com.aether.nightly.plist
```
