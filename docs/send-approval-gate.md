# Frozen send approval gate

The pipeline's send boundary is a `FrozenSendManifest` embedded in an
`ApprovalBatch`. Every entry contains the exact recipient, sender, step,
property-filled subject, literal text/HTML body, source facts and URLs, and
the scheduled timestamp. Entry hashes include those fields; the manifest hash
includes the complete ordered artifact. The database stores the full artifact
under `send-approval:<campaign_id>` so activation can revalidate it rather
than trusting a template-field check.

The policy is intentionally narrow:

- sender name is exactly `Jordan Whitehurst`;
- the approved Aether signature and `2120 W Encanto Blvd, Phoenix, AZ 85009`
  are present in both text and HTML, with no second/conflicting address;
- subjects contain the actual `property_name` (not a company-field substitute);
- bodies contain the recipient's first name; initial notes additionally carry
  the reviewed personalized why line and every supplied source fact, with no
  blank values, merge tags, or placeholders; source facts also require an
  explicit reviewed flag in the frozen artifact (URLs alone are not review).
  Body prose may use a concise or case-variant property description; if the
  full CRM `property_name` is not present, a reviewed `property_context`
  excerpt must be present in both MIME bodies and is included in the message
  hash;
- each approval contains one current step per recipient; step 0 is initial,
  step 1 is the 08:00 slot on the calendar date seven days after its recorded
  actual prior send, and step 2 is the 08:00 slot fourteen days after its
  recorded actual step-1 send. The full 7*24-hour/14*24-hour minimum elapsed
  interval is enforced before rounding forward to the next 08:00 slot.
- all messages are at 08:00 fixed EST (`Etc/GMT+5`) and the aggregate cap is
  five per local calendar day. `first_send_at` may pin the first batch (for
  example, five messages at 08:00 EST on September 16, 2026).

`provider_rendered_canary` is an evidence label only. A canary proves what a
provider rendered for that test recipient at that time; it does not prove
what a dynamic merge route will send later, and it cannot satisfy this send
gate. Only `literal_frozen` artifacts are eligible for a provider request.

Activation also requires fresh received-render evidence. The evidence must
include a provider message ID and both non-null MIME bodies. The frozen
recipient-specific subject and main body must be unchanged; if the provider
appends a footer to HTML, evidence must carry an explicit `footer_policy` with
`status=approved`, `mode=provider_html_only`, an address, unsubscribe domain,
reviewer, and review time. Warmy's plaintext is then required to equal the
frozen payload (CRLF normalization only), while the HTML part must begin with
the exact frozen HTML bytes after that same normalization. The gate validates
the reviewed HTML suffix and rejects unknown
addresses/domains, duplicate/conflicting footer addresses, unresolved
`[[WSY_OPT_OUT]]`, and merge tokens. A missing policy is HOLD. This is not a
generic footer stripper: property addresses in the reviewed why-line remain
part of the main payload and are not treated as footer addresses. The known
Gmail receipt `literal-canary-failed-receipt-20260916.json` remains a negative
diagnostic because it has the wrong workspace address and unresolved plaintext
sentinel. The CLI only validates persisted evidence; it does not fetch Gmail or
Warmy and cannot turn a self-authored JSON claim into independent provider
proof. Before the provider request, the live campaign readback
must be a dedicated one-recipient, one-step campaign whose audience, sender,
mailboxes, subject, and both bodies exactly match the frozen entry. A legacy
  multi-step or dynamic campaign is rejected even when an unrelated local
  manifest is valid. Warmy's API does not reliably return exact membership and
  mailbox selection, so activation additionally requires a fresh hashed UI
  read-attestation (`record-live-read`). The API read independently verifies
  status, fixed-EST window, five-send limit, stop-on-reply/bounce/unsubscribe,
  one literal step, and exact body fields; the UI attestation supplies and
  binds the exact one-recipient audience, mailbox set, sender, variant count,
  approved local schedule date, and 08:00–09:00 fixed-EST window. A draft
  campaign normally has no computed `next_send_at`, so pre-start evidence must
  use `schedule_date`, `schedule_timezone`, `window_start_hour`, and
  `window_end_hour`; an optional post-start `scheduled_at` is accepted only
	  when it remains inside that date/window. Missing or stale UI evidence fails
	  closed.

An internal Jon diagnostic is a separate preflight route: when its exact
allowed transformations pass, it is eligible evidence for the bound literal
route without requiring that a production recipient has already received a
message. It is not evidence that a production recipient has received one.
Its evidence must bind `actual_to` to `jon@automationinterns.com`, retain the
approved production recipient as `target_recipient_email`, include exactly
`Sent by Codex on Jon Schack's behalf.`, and identify the target-specific
unsubscribe URL. The evidence also supplies the approved message's production
unsubscribe URL, the Jon test-prospect/token provider ID, and a hash-bound
`unsubscribe_binding`; the received plaintext/HTML must show the exact
production-to-Jon URL substitution. The provider/token IDs are audited
operator-captured fields; the local CLI does not decode a private token or
independently query the provider, so an unreviewed self-authored record remains
HOLD. A diagnostic never authorizes a production recipient.

This is an enforceable guard for requests made by this repository. Warmy's
independent scheduler, a manual launch in its UI, or another integration can
still send without consulting local code; the pipeline cannot honestly claim
to intercept those paths. Keep the production campaign paused until the
frozen manifest has been independently reviewed and activation is explicitly
authorized. Future follow-up steps require a fresh artifact and approval;
lead research and quarantined copy are retained rather than deleted.

Validate an artifact without provider access:

```bash
uv run python -m integration.cli validate-send-manifest path/to/manifest.json
uv run python -m integration.cli record-render-evidence path/to/evidence.json --apply
uv run python -m integration.cli record-live-read path/to/live-ui-read.json --apply
# Only after reconciling an actual provider acknowledgement:
uv run python -m integration.cli record-provider-send <provider-message-id> <content-hash> <recipient-id> <sent-at-iso> --provider warmy --apply
```

An approving batch must include `render_manifest`; `approve-batch --apply`
rejects legacy template-only batches. Draft-only ingestion may continue to
retain researched contacts, but it cannot authorize a send or campaign start.
