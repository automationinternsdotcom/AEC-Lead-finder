# Aether AEC Lead Finder

Aether AEC Lead Finder is a GPS-style lead pipeline for Aether Facility Services.
It finds Arizona commercial-real-estate activity, enriches each qualified lead with
decision makers and contact data, scores the list, and builds a daily HTML lead email.

The canonical runner is:

```bash
uv run scout/pipeline.py
```

## Architecture

This repo follows the same `scout/` architecture as
[`gps-grok-leadfinder`](https://github.com/automationinternsdotcom/gps-grok-leadfinder).
The one intentional difference is discovery:

- GPS discovers articles through Google News and provider expansion.
- Aether AEC discovers articles from the curated root file `news_websites.csv`.

The active V2 pipeline writes compatibility CSV/HTML outputs plus typed JSONL, raw
responses, stage state, and an auditable run manifest. Provider writes are off by
default. When `AETHER_INTEGRATION_ENABLED=true` on the persistent Mac, a successful V2
export enqueues a typed, hashed company/event/recipient/sequence handoff for the
separate sales worker.
Comparison delivery remains a separate exactly-once Gmail command.

## Folder Layout

| Path | Purpose |
|---|---|
| `scout/` | Active GPS-style pipeline code. |
| `news_websites.csv` | Curated source list used by AEC discovery. |
| `results/YYYY-MM-DD/` | Generated lead CSVs and `leads_email.html`. Ignored by git. |
| `scout/logs/` | Stage logs. Ignored by git. |
| `.github/workflows/test.yml` | CI tests. Does not spend Apollo credits. |
| `.github/workflows/nightly-scout.yml` | Manual/scheduled production run. Can spend Apollo credits. |
| `check.sh` | Fast local self-checks for the `scout/` modules. |
| `run-nightly.sh` | Local LaunchAgent wrapper around `uv run scout/pipeline.py`. |
| `scout/v2/` | Typed services, SQLite state, artifacts, migration, comparison, and promotion gates. |
| `integration/` | Mac-local Warmy, Gmail, Pipedrive, webhook, and SQLite worker service. |
| `infra/macos/` | LaunchAgent templates and Mac-local operating instructions. |
| `config/` | Inactive Warmy campaign and Pipedrive automation specifications. |
| `pipeline/` | Deprecated historical AEC/Pipedrive path. Canonical Scout code does not import it. |

## Requirements

- Python 3.12 or newer
- `uv`
- A Responses-compatible API endpoint with access to Grok and web search
- `news_websites.csv`
- Apollo.io API key if you want Apollo fallback enrichment

## Configuration

Local runs read `.env` from the repository root. Start from `.env.example`:

```env
CLIPROXY_BASE_URL=http://localhost:8317/v1
CLIPROXY_API_KEY=your-key-here
GROK_MODEL=grok-4.3
EXTRACTOR_MODEL=grok-4.3
DB_PATH=scout.db
RESULTS_DIR=results
NEWS_WEBSITES_CSV=news_websites.csv

APOLLO_API_KEY=your-apollo-key-here
APOLLO_WEBHOOK_URL=

NEWSAPI_AI_API_KEY=
NEWSAPI_AI_MAX_PAGES=0
NEWSAPI_AI_TIMEOUT_SECONDS=60
APIFY_TOKEN=
APIFY_FACEBOOK_ACTOR_ID=
APIFY_TIMEOUT_SECONDS=300
```

Do not commit `.env` or any real API key.

## GitHub Secrets

The production GitHub workflow reads these secrets:

| Secret | Required | Used for |
|---|---:|---|
| `CLIPROXY_BASE_URL` | Yes | Responses API endpoint. |
| `CLIPROXY_API_KEY` | Yes | Responses API auth. |
| `APOLLO_API_KEY` | Yes | Apollo fallback when `--apollo-go` is enabled. |
| `APOLLO_WEBHOOK_URL` | No | Apollo phone reveal webhook if phone reveal is added. |

Optional repository variables:

| Variable | Default |
|---|---|
| `GROK_MODEL` | `grok-4.3` |
| `EXTRACTOR_MODEL` | `grok-4.3` |

Add the Apollo key in GitHub at:

`Settings -> Secrets and variables -> Actions -> New repository secret -> APOLLO_API_KEY`

## Running Locally

Install dependencies:

```bash
uv sync
```

Run the full pipeline without Apollo spending:

```bash
uv run scout/pipeline.py
```

Run with Apollo fallback enabled:

```bash
uv run scout/pipeline.py --apollo-go
```

Useful spend controls:

```bash
uv run scout/pipeline.py --max-articles 10
uv run scout/pipeline.py --workers 10
```

Resume an interrupted run without repeating completed stages:

```bash
uv run scout/pipeline.py --run-id <run-id> --resume
uv run scout/pipeline.py --run-id <run-id> --resume --retry-review
```

NewsAPI and Apify are manual-only. Selecting either adapter without its credential
fails preflight:

```bash
uv run scout/pipeline.py --newsapi
uv run scout/pipeline.py --apify
```

Apollo credits are only spent when `--apollo-go` is present.

## Sales automation integration

The integration stays outside Scout's authoritative database:

```text
V2 hashed handoff -> canonical company + event-level Pipedrive Leads
eligible primary -> standalone Warmy verification -> unenrolled Warmy prospect
immutable approval batch -> exact named sequence enrollment
Warmy reply -> Jordan review task -> positive Deal conversion or suppression
```

If Google Workspace delegation is intentionally deferred, set
`GMAIL_REPLY_FORWARDING_ENABLED=false`. Warmy reply webhooks still create Jordan's
Pipedrive review activity and point to the WarmySender Inbox, but the original
message is not forwarded until delegation is enabled.

V2 creates one immutable company identity, one Pipedrive Lead per qualified event,
and one outreach sequence per company/campaign protocol. Leads are organization-only
until the deterministic primary recipient passes all eligibility gates. Backup
recipients remain research records and are never enrolled. `contact_candidate_id`
is provenance rather than CRM identity, so a later source correction does not create
another event Lead. Warmy prospects are reused by normalized email and are created
only after a standalone valid verification result.

Run the default-off configuration check:

```bash
uv run python -m integration.cli doctor
```

Enrollment remains deferred until every activation check passes, the configured live
Warmy campaign exactly matches its approved manifest hash, and an unexpired immutable
approval batch names the exact sequence and merge hash. Campaign approval cannot
release an older backlog accidentally.

WarmySender campaign templates are not updated by git alone. After changing
`config/aether_campaign.yaml`, patch the configured draft or paused campaign and
promote the returned manifest hash:

```bash
uv run python -m integration.cli update-campaign config/aether_campaign.yaml --apply
```

If Warmy rejects the update because the campaign is running, pause it first or create
a replacement draft with `create-campaign-draft`. Verify the live Warmy payload after
the update:

```bash
uv run python -m integration.cli verify-campaign-signature
```

The interrupted Southwest Value Partners canary has a local-only, idempotent
reconciliation command. Preview is read-only; apply performs no provider calls:

```bash
uv run python -m integration.cli reconcile-legacy-swvp
uv run python -m integration.cli reconcile-legacy-swvp --apply-local
```

The GitHub workflow intentionally leaves the handoff disabled. The persistent Mac's
`run-nightly.sh` owns both Scout execution and enqueueing. See
[`infra/macos/README.md`](infra/macos/README.md) for launchd, public HTTPS tunnel,
health-check, and backup instructions.

## Pipeline Stages

| Stage | Service outcome |
|---|---|
| discover | Curated URLs, learned/validated RSS, and optional manual providers |
| qualify | Typed Arizona AEC judgments; invalid/incomplete records go to review |
| dedup | Canonical URL, event fingerprint, and coverage-checked fuzzy grouping |
| decision-makers | Organization-grouped research with stable person identities |
| contacts | Sourced contact research, normalization, and verification |
| apollo | Persistent cached fallback; dry unless `--apollo-go` is present |
| score | Exactly one 0–100 score per submitted `lead_event_id` |
| company-outreach | Company consolidation, sourced Y-line selection, and deterministic primary/backups |
| export | Compatibility CSV/HTML plus typed, hashed `sales_handoff.json` |

## Outputs

Each run writes to `results/YYYY-MM-DD/`:

| File | Contents |
|---|---|
| `raw_leads.csv` | Qualified sales-ready AEC leads. |
| `uncertain_leads.csv` | Plausible but low-confidence leads. |
| `contacts.csv` | Decision makers, contact data, and the personalized outreach `why_line`. |
| `leads_email.html` | HTML lead digest ready to review/send. |
| `runs/<run-id>/final/sales_handoff.json` | The only provider-worker input; typed, versioned, and content-hashed. |

The database `scout.db` is authoritative run state. Every run also writes
`results/YYYY-MM-DD/runs/<run-id>/raw/`, `final/`, and `manifest.json`.

## Historical Migration

Run migration on the local machine that has the complete git-ignored history. Preview
first, then apply. Apply creates a timestamped SQLite backup, imports dated CSVs into
synthetic legacy runs, writes a migration report, and never modifies historical CSVs.

```bash
uv run scout/migrate_v2.py
uv run scout/migrate_v2.py --apply
```

The import is idempotent. Keep the emitted backup until the comparison and promotion
window is complete.

## V1/V2 Comparison and Promotion

The frozen V1 tag is `aether-aec-v1-baseline`. The external harness requires isolated
checkouts, separate V1/V2 databases, a shared source snapshot, and a shared Apollo
cache. Neither runtime receives `--apollo-go`; only the harness accepts that explicit
authorization and projects each paid or null result to both versions.

```bash
uv run scout/compare_v1_v2.py \
  --v1-checkout /path/to/v1 \
  --v2-checkout /path/to/v2 \
  --v1-sha "$(git -C /path/to/v1 rev-parse HEAD)" \
  --work-dir comparison \
  --source-snapshot news_websites.csv
```

`.github/workflows/comparison-scout.yml` is scheduled for 10:00 UTC with a 90-minute
timeout but stays inactive until repository variable `AETHER_V2_COMPARISON_ENABLED`
is `true`. Shared Apollo use additionally requires explicit workflow input or
`AETHER_COMPARISON_APOLLO_ENABLED=true`.

Comparison email sending is separate and requires the authenticated local `gog`
profile. `scout/deliver_comparison.py` validates the profile, both terminal manifests,
exact subjects, deduplicated recipients, disclosure footer, and exactly one Sent
message. Its `--monitor` mode is read-only and never retries a send.

After delivery, `scout/prepare_promotion.py` derives deterministic inputs and
`scout/score_promotion.py` writes the versioned scorecard plus exact judge prompt and
raw response. Automatic promotion requires two green days out of three and no manual
review, veto, or hard blocker. Apply the final gate with
`scout/aggregate_promotion.py day1.json day2.json day3.json`; signed human overrides
are supplied to the scoring command as a JSON file.

## GitHub Production Run

`.github/workflows/nightly-scout.yml` can run the V2 pipeline from GitHub Actions:

- Manually through `Actions -> nightly scout -> Run workflow`
- Automatically every day at `13:00 UTC`

The workflow validates required secrets, restores only the schema-specific V2
production cache, runs `uv run scout/pipeline.py --apollo-go`, uploads the generated results as an
artifact, and saves the updated database cache.

## Local Nightly Run

`run-nightly.sh` is the local wrapper for launchd:

```bash
./run-nightly.sh --max-articles 5
```

It loads `.env`, writes a timestamped log under `logs/`, and exits with the same status
as the scout pipeline.

## Checks

Fast module self-checks:

```bash
./check.sh
```

Full test suite:

```bash
uv run pytest -q
```
