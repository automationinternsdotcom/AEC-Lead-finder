# Aether AEC Scout Pipeline

This repository follows the no-key Codex/Computer-Use lead-enrichment pattern.

The canonical daily contract is:

```bash
python3 scripts/validate_source_list.py
```

The Codex daily automation performs browser discovery/enrichment and writes the
validated handoff. Import a validated handoff with:

```bash
bash run-daily-ingest.sh --handoff /absolute/path/to/sales_handoff.json
```

The retained V2 code can validate and project nine resumable in-process stages
when supplied with a Codex-produced handoff:

1. Curated-site and validated-feed discovery.
2. Typed qualification with review quarantine.
3. Exact and coverage-checked fuzzy event deduplication.
4. Sourced, approved-template why-line generation for LinkedIn and first-email outreach.
5. Organization-grouped decision-maker research.
6. Person-grouped contact research and verification.
7. Optional, authorization-gated Apollo fallback.
8. Complete-ID scoring.
9. Compatibility CSV/HTML and auditable JSONL export.

Each run persists stage state, raw/final artifacts, and a manifest under
`results/<day>/runs/<run_id>/`. Use `--run-id ID --resume` to continue a run.
Legacy stage programs remain compatibility entrypoints only; canonical `scout/`
code must not import the deprecated top-level `pipeline/` package.

The intentional architecture difference from the old GPS runtime is discovery
and model routing: Aether uses the curated `news_websites.csv` file in the repo
root, while research is performed in-session by Codex/Computer Use rather than
through a model API or CLIProxy.

Start commands from the repo root. Keep secrets in `.env`; do not commit them.
Use `--apollo-go` only when the operator explicitly wants Apollo credits spent.
NewsAPI, Apify, MapsData, and Costar are manual-only via `--newsapi`, `--apify`,
`--mapsdata`, and `--costar`.

## Production send contract

- Production Aether email starts and enrollments must use the validated
  recipient-specific frozen-send gate. Direct Warmy MCP/UI launches or other
  integrations must not be treated as an alternate approval path. This local
  contract does not claim to technically intercept Warmy's remote scheduler.
- Keep the legacy production campaign paused until its queue, live readback,
  received MIME evidence, and global send ledger are reconciled and reviewed.
- An internal diagnostic send is permitted only when Jon explicitly authorizes
  it, and it must be a contained single-recipient, single-step literal route
  with the same sender, signature, body, evidence, and stop controls. It is not
  production authorization and cannot be used to bypass the gate for other
  recipients.
