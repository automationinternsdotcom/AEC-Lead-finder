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

### Treg fallback after Grok

Enable `TREG_FALLBACK_ENABLED=true` on the persistent Mac, or pass `--treg-go`.
After Grok's public contact research, companies still without a usable email go
through Treg: cached people first, exact company/domain discovery, free coverage
counts, Icypeas plus independent free LeadsForge search, up to three people per search, then an email-provider waterfall capped at
$0.025 per lookup. Catch-all/unknown and former-employer addresses remain fallbacks;
role is a preference and a sole distinct usable address is sufficient.

`TREG_BUDGET_USD` / `--treg-budget-usd` defaults to $5 per monthly daily-pipeline
cache, including reservations and resumed requests. The client also retains a
$5.05 balance floor. Every paid request has a persisted idempotency key; provider
errors and budget deferrals are separate from no-match results. Authentication
uses `TREG_TOKEN` or the existing local Treg CLI login. Secrets never enter exports.
Completed lookups refresh after 30 days; uncertain retries reuse their original
idempotency key. A recovered alternate address has a separate verification identity.
Domain discovery tries two free providers. Newly searched people require an exact
company-domain match; a previously sourced person plus LinkedIn can be used without
a domain. Same-name properties/companies alone are recorded as `needs_identity`.
When article context is available, a $0.005 Exa ownership lookup can resolve the
remaining identity: an exact quote from a retrieved official-domain citation and
an explicit link to the opportunity are required. These calls share the same cap.

Recover the existing sales backlog without recrawling articles or repeating Grok:

```bash
uv run python skills/aether-bulk-enrichment/scripts/bulk_enrich.py \
  --since 2026-01-01 --until 2026-09-05 \
  --output results/backfills/treg-recovery-20260906 \
  --recover-sales-with-treg aether_sales.sqlite --treg-go \
  --treg-budget-usd 5 --workers 3 --apply-recovered-leads
```

The saved inventory defines the company cohort; dates label the historical request
and do not filter this existing-sales mode. Repeating the command resumes it.
Use `--refresh-sales-inventory` to include newly missing or invalid-only companies;
the prior inventory is archived and the same paid-request ledger is retained.
`inventory.json`, per-company results, `leads.csv`, `summary.json`, and a validated
`sales_handoff.json` retain coverage and evidence. `--apply-recovered-leads` enqueues
the existing provider synchronization flow; it does not approve or enroll a campaign.
`recovered-leads.csv` contains only the currently usable recovered contacts;
`leads.csv` also includes unresolved opportunities. Warmy verification is paced
across workers, and HTTP 429 deferrals do not exhaust the job retry budget.
For manually reviewed named emails already present in cached official citations,
add `--import-reviewed-treg-contacts reviewed.json` to the recovery command. This
makes no paid calls, validates the person/email/quote against the cached source,
retains the review and prior result, and imports only the reviewed batch. The JSON
rows require company ID, name, title, email, corporate domain, source URL, exact
source quote and an operator-reviewed opportunity relationship note.
To refresh the exports after live verification or suppression changes, use
`--export-treg-recovery` with the same output and sales DB. This local-only mode
requires no `--treg-go`, makes no provider calls, and does not enqueue work.
Use `--treg-cache-only` to retry the recovery cohort entirely from cached Treg
responses. Cache misses remain explicit; even free provider calls are disabled.
Preview and later apply are resumable: `--apply-recovered-leads` imports only newly
recovered contacts not already present, avoiding re-enqueuing the whole cohort.

Resolver v6 treats provider/historical domains as unverified hints whenever an
opportunity is supplied. Official evidence must confirm the original named target;
landlord/owner alternatives remain separate `related_route` research records.
`reviewed-identities.json` in the recovery folder can supply reviewed corrections
with a company ID, official domain, source URL, exact cached source quote and
relationship note. Optional `people_domains` are reviewer-attested employer-domain
aliases, not permission to match an arbitrary same-name business. Optional `inboxes`
require their own exact official source, quote and scope. Shared mailboxes are
labeled explicitly and use `Hi team`, never a fabricated person's name. Official
identity responses can also supply published general/contact/procurement inboxes
as a last fallback. Suppression and invalid-email checks still apply.

Named people searches rank role and local geography, retrieve up to 12 people per
page and follow at most three Icypeas pages per filter. The live catalog is checked
against the per-call ceiling for every page; the original total budget and balance
floor remain unchanged. Corrected identity inputs invalidate cached no-match
decisions. Prior result snapshots, `identity-review-queue.json`, and
`recovered-identity-audit.json` preserve unresolved and legacy identity checks;
the latter is a review list, not proof that all listed contacts are wrong.
Confirmed wrong-company routes are recorded in `identity-exclusions.json` with
exact company/email/domain and evidence. Applying recovery blocks those specific
sequences and persists company-scoped exclusions checked during both sync and
enrollment. It does not mark a working mailbox globally invalid, unsubscribe its
owner, delete the prospect, or reject other contacts at the correct company.
Fresh bulk recipient enrichment also accepts `--treg-go`; completed older bulk
revisions should use the existing-sales recovery mode to preserve their manifests.

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
after a standalone verification attempt. Valid addresses are preferred, while
catch-all and unknown results remain sendable fallbacks when they are the best
available address. An organization-domain mismatch is retained as a lower-ranked
fallback instead of being discarded. A below-threshold role is also allowed when it
is the only address found for the company. Explicit invalid results, missing-MX domains,
disposable domains, suppressions, opt-outs, bounces, and duplicate protections remain
hard stops.

Recipient admission is shared by the daily Scout, bulk enrichment, Treg recovery,
historical reconciliation, and provider integration. It deliberately separates:

- hard failures: invalid/disposable addresses, explicit suppressions, and an
  evidence-backed company/email identity exclusion;
- selection preferences: current-employer domain, role fit, local scope, source,
  verification confidence, and evidence strength;
- soft fallback signals: catch-all/unknown mailbox status, employer-domain mismatch,
  low role score, shared mailbox, and unresolved geography.

Soft signals stay on the recipient rationale and trigger better-match research; they
do not delete the only usable address. Treg returns the best current-domain result
when it finds one and otherwise preserves the prior non-invalid address as a labeled
fallback. Company profiles also persist the article company's exact relationship
(operator/owner, property manager, developer, contractor, broker, tenant, project,
or unknown), confidence, sources, and outreach route through the sales handoff.
Project/organization ambiguity is retained for review rather than being silently
converted to an unrelated landlord, developer, contractor, or namesake.

Accuracy findings are scoped to an exact company/email pair. An identity exclusion
is a hard stop for that pairing; an accuracy review hold pauses that pairing without
globally suppressing the mailbox. Enrollment rechecks both guards at action time.

Existing sequences blocked only by the former catch-all/unknown rule can be previewed
and idempotently re-queued under the current fallback policy:

```bash
uv run python -m integration.cli reconcile-fallback-addresses
uv run python -m integration.cli reconcile-fallback-addresses --apply
uv run python -m integration.cli reconcile-role-fallbacks
uv run python -m integration.cli reconcile-role-fallbacks --apply
```

Run the default-off configuration check:

```bash
uv run python -m integration.cli doctor
```

Enrollment remains deferred until every activation check passes, the configured live
Warmy campaign exactly matches its approved manifest hash, and an unexpired immutable
approval batch names the exact sequence and merge hash. Campaign approval cannot
release an older backlog accidentally.

WarmySender campaign templates are not updated by git alone. The existing Aether
draft has separately managed 50/50 A/B subjects recorded in
`config/daily_campaign_fingerprint.json`. The YAML contains the initial A subject
and threaded follow-ups, not a full export of live variant state. The update CLI
refuses to overwrite this pinned campaign because its API cannot preserve or
verify the variants. Use a variant-capable edit surface and re-verify the daily
fingerprint after any approved live change; merging code does not deploy templates.

For other draft/paused campaigns without separately managed variants, after changing
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
