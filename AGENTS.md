# Aether AEC article pipeline

Production runs only in `.github/workflows/nightly-article-pipeline.yml`.
Fetch the latest GitHub code before changes. The runtime scans all 125 rows in
`news_websites.csv`, qualifies fetched evidence deterministically, enriches through
TREG data endpoints, syncs research-only Pipedrive Leads, and sends Gmail reports.
No Codex Automations, Mac schedules, browser research, Grok, x.ai, CLIProxy, or
model APIs may run the production article pipeline. Legacy `scout.llm.call` fails
closed. Do not re-enable it or add fallback runners.

Only GitHub Actions secrets may store `TREG_TOKEN` and
`GMAIL_SERVICE_ACCOUNT_JSON`; never commit or print credentials. Gmail delegation
uses only gmail.readonly and gmail.send and impersonates akhil@automationinterns.com.
Reports go to jw@aetherclean.com, with a separate copy to jon@automationinterns.com
and akhil@automationinterns.com. Keep CC/BCC empty. Never send article reports or
tests to prospect addresses or jordan@aethercommercialcleaning.us.

Validate sources with `python3 scripts/validate_source_list.py`, run `uv run pytest -q`
and `./check.sh`. Dispatch a controlled test only after Gmail and TREG configuration.
Legacy sales modules remain separate from the article workflow.

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
