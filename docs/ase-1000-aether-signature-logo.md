# ASE 1000 Technical Spec: Aether Warmy Signature Logo

## Status

Implemented in branch `add-aether-signature-logo` and reflected in the live
WarmySender draft on September 4, 2026.

## Context

Jordan Whitehurst's Aether campaign signature already included his name, title,
company, office number, mobile number, and Phoenix address. The missing piece was
the Aether logo image in the HTML signature. WarmySender campaigns do not change
just because the repository manifest changes; the approved manifest has to be
posted to Warmy again and the resulting campaign ID/hash have to be promoted into
the runtime environment.

## Decision

Serve the provided Aether logo from the existing sales integration API and embed
that public HTTPS URL in every Warmy campaign step. Use WarmySender's own open
tracking for campaign analytics by enabling `trackOpens`.

## Implementation

- Added `integration/assets/aether-signature-logo.png` from the provided Aether
  signature image.
- Added `GET /assets/aether-signature-logo.png` to the FastAPI sales integration
  so the logo is reachable at:
  `PUBLIC_BASE_URL/assets/aether-signature-logo.png`.
- Added `Settings.signature_logo_url` and replaced
  `{{AETHER_SIGNATURE_LOGO_URL}}` during manifest loading.
- Updated `config/aether_campaign.yaml` so all four Warmy HTML steps include the
  logo above Jordan's full signature:
  Jordan Whitehurst, Partner; Aether Facility Services, LLC; office and mobile
  phone numbers; 2120 W Encanto Blvd, Phoenix, AZ 85009.
- Kept the plain-text bodies image-free for email clients that do not render HTML.
- Added `update-campaign` so an existing draft or paused Warmy campaign can be
  patched from the same approved manifest instead of always creating a new draft.
- Added `verify-campaign-signature` so the live Warmy campaign can be checked
  for the hosted logo and full Jordan signature after sync.
- Added validation so an approved campaign cannot load without the logo placeholder
  or with a non-HTTPS public base URL.

## WarmySender Sync

Live sync completed for WarmySender campaign
`92fc58e2-d68f-4c3d-878f-158f89dde5df` (`Aether AEC Evergreen Outreach`). The
verified draft now has `trackOpens` enabled, six selected mailboxes preserved,
and all four email steps include the logo plus the full Jordan Whitehurst
signature. The immediate live sync used this commit-pinned logo URL:
`https://raw.githubusercontent.com/automationinternsdotcom/AEC-Lead-finder/14a9c18ff211f15f965ae5aa181073592d3e3cae/integration/assets/aether-signature-logo.png`.

After this branch is merged on the persistent Mac with production `.env` values,
patch the existing Warmy campaign if it is in draft or paused status:

```bash
uv run python -m integration.cli update-campaign config/aether_campaign.yaml --apply
```

Warmy rejects template edits on running campaigns. If the existing campaign cannot
be patched, create a fresh draft from the approved manifest:

```bash
uv run python -m integration.cli create-campaign-draft config/aether_campaign.yaml --apply
```

Then set the returned `campaign_id` and `manifest_hash` as the runtime
`WARMY_CAMPAIGN_ID` and `WARMY_CAMPAIGN_MANIFEST_HASH`. That is the step that makes
the repository changes visible inside WarmySender and allows enrollment hash
validation to pass.

Verify the reflected Warmy payload:

```bash
uv run python -m integration.cli verify-campaign-signature
```

## Verification

Run:

```bash
uv run pytest tests/test_campaign_hardening.py tests/test_sales_integration.py -q
```

The tests cover logo inclusion in the Warmy payload, HTTPS-only hosted logo URLs,
Warmy open tracking, the public logo asset endpoint, and the read-only Warmy
signature verifier.
