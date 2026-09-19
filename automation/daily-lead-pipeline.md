# Daily article-lead workflow (no model API)

This is the Aether equivalent of the lead-enrichment workflow. It is executed
by the Codex daily automation, using the authenticated Computer-Use/Chrome
session for public-site research and Gmail for the internal report. It does not
use Grok API credentials, CLIProxy, direct provider endpoints, or a separate
headless model client.

## Run contract

1. Read `news_websites.csv` and require exactly 125 data rows before changing
   any output.
2. For every pending Phoenix calendar date, inspect all 125 public sources.
   Use direct pages, RSS/Atom, sitemaps, and public records; do not bypass
   paywalls, robots rules, CAPTCHAs, or access controls.
3. Keep only date-verifiable Arizona commercial-property/facility events and
   record the source URL, original publication date, company/property, signal,
   score, priority, filter reason, and service angle. Never guess contact data.
4. Save a validated article-lead CSV, an audit log, a `sales_handoff.json`, and
   an HTML report under `results/YYYY-MM-DD/`. Use atomic writes and preserve
   retryable state under `pipeline_state/`.
5. Upsert each eligible article lead into Pipedrive through the Leads API, with
   an `Article lead` title and the source category `article`. Keep the CRM
   result (created/updated IDs and failures) in the run artifacts.
6. Send exactly one internal report from `akhil@automationinterns.com` to
   `jon@automationinterns.com` after validation and the Pipedrive handoff. Keep
   CC and BCC empty. The report must state that no prospect outreach was sent.
   Structure it as an HTML table with: article/source link, publication date,
   company/property, event/signal, location, score/priority, service angle,
   contact name/title/email/phone/LinkedIn when verified, and the Pipedrive
   Lead ID/status. Never use `jordan@aethercommercialcleaning.us` for this
   internal report; Jordan is reserved for Warmy prospect campaigns.
7. Only after Gmail confirms delivery may the run record its sent marker. A
   failed or ambiguous send remains retryable and must not be claimed as sent.

MapsData and Sales Navigator exports use the separate Maps/Sales Navigator
Warmy campaign and are not mixed into this article report. Warmy enrollment and
campaign start remain controlled by the existing provider/send gates.

## Preflight

```bash
python3 scripts/validate_source_list.py
```

The preflight is deterministic and has no network or model dependency. The
Codex automation owns the browser research and Gmail steps; the repository owns
the schemas, state, validation, and artifacts.
