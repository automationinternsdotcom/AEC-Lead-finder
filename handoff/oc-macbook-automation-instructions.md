# OC MacBook Automation Handoff: Costar and MapsData Lead Sources

Branch: `feature/costar-mapsdata-lead-sources`

## Goal

Add optional Costar tenant-data and MapsData lead ingestion to the Aether AEC Scout
pipeline alongside the existing curated web/RSS discovery, LinkedIn/Sales Nav-guided
research, general web research, Apollo fallback, and sales handoff.

## What Changed

- `scout/v2/providers.py`
  - Adds `MapsDataAdapter` for completed MapsData jobs or local MapsData CSV exports.
  - Adds `CostarTenantAdapter` for operator-reviewed Costar tenant CSV exports.
  - Both adapters normalize flexible CSV headers, preserve the raw row payload, and
    emit ordinary provider discovery records.
- `scout/v2/orchestrator.py`
  - Adds `mapsdata` and `costar` provider gates to the resumable run configuration.
- `scout/pipeline.py`
  - Adds `--mapsdata` and `--costar`.
  - Enables those flags automatically when their env-backed input paths/job IDs are
    present.
- `scout/v2/qualification.py`
  - Qualifies saved source evidence rather than assuming every candidate is a news
    article.
- `scout/v2/research.py`
  - Passes lead-event/source context into decision-maker research and explicitly
    allows LinkedIn Sales Navigator findings when available to the operator.
- `README.md`, `README-AUTOMATION.md`, `.env.example`
  - Document provider setup and runtime behavior.

## Required Runtime Inputs

MapsData, local CSV mode:

```bash
MAPSDATA_CSV=/Users/openclaw/Code/AEC-Lead-finder/provider-inputs/mapsdata-phoenix.csv
```

MapsData, completed job mode:

```bash
MAPSDATA_KEY=mdk_live_...
MAPSDATA_JOB_ID=<completed-job-id>
```

Costar tenant export:

```bash
COSTAR_TENANT_CSV=/Users/openclaw/Code/AEC-Lead-finder/provider-inputs/costar-tenants.csv
```

Keep all real exports and keys out of git. Recommended runtime-only directory:
`/Users/openclaw/Code/AEC-Lead-finder/provider-inputs/`.

## MapsData Operating Notes

The pipeline only reads completed MapsData jobs or CSV exports. It does not start
new scrapes during nightly automation because MapsData jobs are asynchronous and
can spend account allowance. To use job mode, start and complete the scrape in the
MapsData app/API first, then put the completed job ID in `.env`.

CSV exports should include as many of these columns as available:

- `Business` or `Business Name`
- `Category`
- `Address`
- `City`
- `State`
- `Website`
- `Email`
- `Phone`

## Costar Operating Notes

Use an operator-reviewed tenant export. Do not scrape a logged-in Costar browser
session from automation. Exports should include as many of these columns as
available:

- `Tenant Name`
- `Property Name`
- `Address`
- `City`
- `State`
- `Lease Date`, `Move In Date`, `Occupancy Date`, or similar
- `Property URL` or `Listing URL`

The qualification stage treats Costar tenant rows as source context that must still
produce an Arizona commercial-property event. Downstream research must verify the
operator/person/domain with public or official sources, LinkedIn/Sales Nav context,
or another credible source before outreach.

## Smoke Test Before Updating Automation

From the runtime checkout:

```bash
git fetch origin
git checkout feature/costar-mapsdata-lead-sources
uv sync
uv run pytest -q tests/test_scout_v2_discovery.py tests/test_scout_v2_orchestrator.py tests/test_scout_entrypoints.py
uv run scout/pipeline.py --max-articles 5 --mapsdata --costar
```

If only one provider is ready, omit the other flag. If no provider input is ready,
run the baseline smoke:

```bash
uv run scout/pipeline.py --max-articles 5
```

## Automation Command Update

After the smoke test produces a nonzero lead run or a reviewed expected result,
update the existing Codex automation instruction file on the OC MacBook to append
the ready provider flags:

```bash
bash run-daily-ingest.sh --mapsdata --costar
```

Use only flags whose inputs exist in the runtime `.env`. Keep this handoff file
updated whenever the automation command, provider paths, or provider policy changes.

## Rollback

Remove `--mapsdata` and/or `--costar` from the automation command and unset the
corresponding env vars. Existing Scout state remains valid because provider flags
are part of each run's configuration hash.
