# Campaign configuration and verification

`config/aether_campaign.yaml` holds base copy, reply threading and settings.
`config/aether_subject_variants.json` holds every approved subject variant and
weight, by step and campaign. Together they are the approved configuration.
`complete_campaign_hash` binds the base manifest hash to all variant subjects,
weights, labels and step assignments. The legacy base hash stays compatible with
existing immutable enrollment approvals; it is explicitly not a full A/B hash.

The ordinary Warmy campaign response does not expose A/B state. Do not infer it
from the first subject or fill missing observations from configuration. Read the
actual variant state via MCP, then documented API, then Computer Use when both
lack capability. Normalize that read into this internal observation schema (not
a Warmy request body):

```json
{
  "observed_at": "2026-09-09T03:00:00+00:00",
  "source": "computer_use",
  "source_reference": "Path or durable reference to the actual read evidence",
  "base_manifest_hash": "Hash of the same observed base campaign snapshot",
  "plan": {
    "campaign_id": "Actual campaign ID",
    "steps": [{"step_index": 0, "variants": [
      {"label": "A", "subject": "Actual subject A", "weight": 50},
      {"label": "B", "subject": "Actual subject B", "weight": 50}
    ]}]
  }
}
```

Verify with `uv run python -m integration.cli verify-campaign-variants observation.json`.
Add `--apply` to store the verified observation locally; this never edits Warmy.
The verifier validates the supplied observation, not its provenance by network:
the operator must capture real read evidence, never copy the approved plan and
claim it was observed. Records expire after 24 hours and are invalidated by base
or variant-plan changes. Neither an old UI note nor an omitted API field passes.

Draft ingestion retains useful contacts and reports A/B state as unverified when
necessary. Starting or enrolling into a non-draft configured campaign requires a
fresh matching observation. Recheck immediately before an approved launch and
after any external campaign edit; observations cannot detect unseen later edits.
The single-manifest updater remains blocked for variant-managed campaigns until
a verified variant-capable write adapter exists. Do not create a replacement to
get around a capability gap.

For signatures, `AETHER_SIGNATURE_LOGO_URL` decouples the image from the sales API.
An approved text-only template can omit the logo tag entirely; verify its full
signature with `verify-campaign-signature --text-only`. Approval, postal-address,
unsubscribe and sender-signature checks remain in place.
