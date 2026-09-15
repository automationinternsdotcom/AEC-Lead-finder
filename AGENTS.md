# Aether AEC Scout Pipeline

This repository follows the `gps-grok-leadfinder` operating pattern.

The canonical daily command is:

```bash
uv run scout/pipeline.py
```

The V2 pipeline runs nine resumable in-process stages:

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

The only intentional architecture difference from `gps-grok-leadfinder` is discovery:
GPS uses Google News/provider expansion, while Aether AEC uses the curated
`news_websites.csv` file in the repo root.

Start commands from the repo root. Keep secrets in `.env`; do not commit them.
Use `--apollo-go` only when the operator explicitly wants Apollo credits spent.
NewsAPI, Apify, MapsData, and Costar are manual-only via `--newsapi`, `--apify`,
`--mapsdata`, and `--costar`.
