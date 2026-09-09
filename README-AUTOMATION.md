# Aether AEC Nightly Automation

## Daily Grok → Pipedrive → Warmy ingestion

The current daily automation runs `bash run-daily-ingest.sh` at 02:00 America/Phoenix.
It uses Grok 4.3 for qualification/research and Treg only after Grok, under the shared
$5 monthly cap. The persistent sales DB stores the last ingested date and the active
run, so an interrupted run resumes and missed dates are covered. Each new window
overlaps the checkpoint by two days to catch late articles.

`integration.daily` validates the typed handoff, syncs Pipedrive events and people,
creates/updates Warmy contacts with the sourced why-lines, then approves the exact
ready merge hashes and enrolls them into the configured **draft** campaign. It
uses Grok to confirm same-company/same-project matches against prior CRM events
and reuses their original Lead IDs. Uncertain matches stay eligible as new events.
The match inputs and decisions are cached in the sales DB for audit and retries. It
preserves existing enrolled/replied sequences and their recipient data. Draft
ingestion does not require Pipedrive reply automations to be live; live campaign
activation retains every existing activation requirement.

Treg budget/provider deferrals create durable `treg_enrichment_deferred` reviews;
they do not abort export or ingestion of contacts already found. Completed Grok
no-email searches are cached within each run. After the provider/balance recovers,
retry the original dates and run ID with `--retry-review` to recover the backlog.

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
