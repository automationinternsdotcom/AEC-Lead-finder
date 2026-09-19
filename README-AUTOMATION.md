# Aether daily automation

## Article discovery and Jon's internal report

The canonical daily workflow is
`automation/daily-lead-pipeline.md`. It uses Codex/Computer Use with the
authenticated Chrome profile for public research and Gmail for one internal
report from `akhil@automationinterns.com` to `jon@automationinterns.com`. It
upserts eligible article leads into Pipedrive Leads and does not use a Grok/x.ai
API key, CLIProxy, a direct model endpoint, or prospect addresses as report
recipients.

Before each run, validate the complete source list:

```bash
python3 scripts/validate_source_list.py
```

The check must report exactly 125 websites. The Codex run then processes every
pending Phoenix calendar date and attempts all 125 sources. It saves validated
article-lead artifacts, a sales handoff, and a Gmail sent marker under the
run/state contracts in the repository. Ambiguous or failed work stays
retryable.

The report to Jon is separate from prospect outreach. It must say that no
external outreach was sent. Warmy enrollment and campaign activation remain
off unless the existing provider and frozen-send gates are explicitly approved.

## MapsData/Sales Navigator ingestion

MapsData and Sales Navigator are completed-export-only inputs. Import them with
`scout.raw_leads`, preserve their source fields, enrich them through the
authenticated browser workflow, and route them only to the separate MapsData +
LinkedIn Outreach Warmy campaign. Do not mix them into the article report or
article campaign.

For a validated sales handoff, the local ingestion wrapper is:

```bash
bash run-daily-ingest.sh --handoff /absolute/path/to/sales_handoff.json
```

The wrapper refuses to run without `--handoff`; it is not a model/API runner.
Keep `WARMY_ENROLLMENT_ENABLED=false` and `CAMPAIGN_START_ENABLED=false` until
the operator reviews the manifest and approves activation.

## Legacy files

`run-nightly.sh` and the LaunchAgent plist are retained as legacy compatibility
templates. They are not the daily no-key scheduler. Do not install the plist as
a second scheduler beside the Codex automation.
