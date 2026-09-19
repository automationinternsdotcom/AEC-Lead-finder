# Daily article-lead workflow (GitHub, no model API)

This is the Aether equivalent of the lead-enrichment workflow. It is executed
by `.github/workflows/nightly-article-pipeline.yml` on GitHub Actions. The
runner reads and fetches every row in `news_websites.csv` over public HTTP,
uses deterministic evidence gates for qualification and deduplication, uses
TREG for bounded contact enrichment, upserts Article leads into Pipedrive, and
sends the internal report through Gmail domain-wide delegation. It does not
use Grok API credentials, CLIProxy, Codex/Computer Use, or a separate model
endpoint.

## Run contract

1. Run every calendar day, including Saturdays and Sundays. Read
   `news_websites.csv` and require exactly 125 data rows before changing
   any output.
2. For every pending Phoenix calendar date, inspect all 125 public sources.
   Use direct pages, RSS/Atom, sitemaps, and public records; do not bypass
   paywalls, robots rules, CAPTCHAs, or access controls.
3. Target at least 30 unique article leads per calendar day. Define uniqueness
   by the canonical source URL plus the normalized company/property, event, and
   publication date. Continue through all 125 sources and any retryable source
   work needed to reach the target. Keep only date-verifiable Arizona
   commercial-property/facility events supported by fetched article text and
   record the source URL, original publication date, company/property, signal,
   score, location, and service angle. Never guess contact data.
4. Save a validated article-lead CSV, an audit log, a `sales_handoff.json`, and
   an HTML report under `results/YYYY-MM-DD/`. Use atomic writes and preserve
   retryable state under `pipeline_state/`.
5. Enrich each selected lead through the authenticated TREG route, retaining
   only returned and verification-reviewed contact data. Upsert each eligible
   article lead into Pipedrive through the Leads API, with an `Article lead`
   title and the source category `article`. Keep the CRM result (created or
   updated IDs and failures) in the run artifacts.
6. Send exactly one internal report from `akhil@automationinterns.com` to
   `jw@aetherclean.com` after validation, TREG enrichment, and the Pipedrive
   handoff. Keep CC and BCC empty. Then send one separately identified internal
   review copy to `jon@automationinterns.com` and
   `akhil@automationinterns.com`. The report must state that no prospect
   outreach was sent.
   Structure it as an HTML table with: article/source link, publication date,
   company/property, event/signal, location, score/priority, service angle,
   contact name/title/email/phone/LinkedIn when verified, and the Pipedrive
   Lead ID/status, plus the achieved count and any shortfall versus 30. Never
   use `jordan@aethercommercialcleaning.us` for this
   internal report; Jordan is reserved for Warmy prospect campaigns.
   Never fabricate, duplicate, or downgrade qualification standards just to
   reach 30; if fewer than 30 valid unique leads exist, report the shortfall
   clearly to Jon and leave the run retryable where more source work is possible.
7. Only after Gmail confirms delivery may the run record its sent marker. A
   failed or ambiguous send remains retryable and must not be claimed as sent.

MapsData and Sales Navigator exports use the separate Maps/Sales Navigator
Warmy campaign and are not mixed into this article report. Warmy enrollment and
campaign start remain controlled by the existing provider/send gates.

## Preflight

```bash
python3 scripts/validate_source_list.py
```

The preflight is deterministic and has no model dependency. The GitHub workflow
owns discovery, enrichment, CRM sync, and Gmail delivery; the repository owns
the schemas, state, validation, and artifacts.
