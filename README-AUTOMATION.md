# Aether nightly article pipeline

GitHub Actions is the only production runner:
`.github/workflows/nightly-article-pipeline.yml` runs at 10:00 UTC (03:00 Phoenix).
It scans all 125 curated websites, qualifies fetched evidence without a model API,
enriches through TREG data providers, and upserts research-only Pipedrive Leads.
It sends from akhil@automationinterns.com to jw@aetherclean.com and sends a separate
copy to jon@automationinterns.com and akhil@automationinterns.com. CC/BCC stay empty.
No prospect outreach or Warmy campaign is launched. Jordan's Warmy mailbox must
never be used for article reports.

Required GitHub Actions repository secrets:
- TREG_TOKEN
- GMAIL_SERVICE_ACCOUNT_JSON
- PIPEDRIVE_API_TOKEN
- PIPEDRIVE_DOMAIN
- PIPEDRIVE_JORDAN_USER_ID
- PIPEDRIVE_DEAL_FIELDS

See `docs/gmail-delegation.md`. Missing credentials fail closed. Never print or
commit credentials, or store Gmail JSON/TREG_TOKEN in local configuration.

Manual dispatch defaults to controlled_test=true: scan all sources, enrich and
sync at most one qualified lead, and deliver the two fixed internal report routes
with a unique test subject. Provider authentication runs before paid lookups or
CRM writes. Inspect run_summary.json and Gmail envelope verification before
claiming success. Gmail API acceptance/sent evidence is not proof of inbox receipt.

The historical LaunchAgent is disabled and run-nightly.sh refuses local runs.
Do not install another scheduler, invoke browser/Grok/CLIProxy research, or create
a Codex Automation as a fallback. MapsData/Warmy sales workflows are separate.
