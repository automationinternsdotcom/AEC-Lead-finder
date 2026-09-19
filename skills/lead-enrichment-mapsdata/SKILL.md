---
name: lead-enrichment-mapsdata
description: Import completed MapsData and Sales Navigator exports, enrich them through the AEC pipeline, and route them to a separate Warmy campaign.
---

# MapsData and Sales Navigator lead enrichment

Use this workflow for commercial-cleaning prospect exports from MapsData and
Sales Navigator.

## Source contract

- Consume completed MapsData exports or already-created job results only. Do not
  start a scrape or spend MapsData credits from the pipeline.
- Consume exported Sales Navigator CSVs only; preserve the source and observed
  signal fields.
- Normalize and deduplicate with `pipeline.raw_leads` before model enrichment.
- Keep source data and manifests outside Git; lead exports are sensitive.

## Model contract

The lead-enrichment reference workflow uses Codex/Computer Use with authenticated
browser sessions and does not require a model API key. This project must not call
`api.x.ai`, `api.grok.com`, or any direct model endpoint, and must not require
CLIProxy. If browser research is unavailable, stop at the durable raw queue and
report the preflight failure; never fabricate enrichment or silently switch
providers.

## Warmy contract

- MapsData and Sales Navigator rows belong in their own campaign, separate from
  article leads.
- The only approved sender is `jordan@aethercommercialcleaning.us`; reject or
  remove every other Jordan mailbox before saving a campaign.
- Keep provider writes, enrollment, and campaign start disabled until an
  operator reviews the manifest and explicitly approves the action.
- A test send is an external email action and requires confirmation immediately
  before sending.

## Reference-repository alignment

The `automationinternsdotcom/lead-enrichment` repository does not contain a
standalone MapsData `SKILL.md`; this file imports its relevant operating
contract: completed-export-only discovery, Codex/Computer-Use research, durable
normalization, and logged-in Chrome for Warmy list operations.
