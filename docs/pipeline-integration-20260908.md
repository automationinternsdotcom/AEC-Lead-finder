# Integrated branch: recipient recovery and campaign safety

This integration reconciles `codex/systematize-pipeline-safety` with main,
including Akhil's signature PR #29 and the earlier contact/discovery changes.

- Keep logo hosting, the full Jordan signature, signature verification, and
  campaign-update tooling. Check campaign status before writing, save a pre-write
  snapshot, and prevent this single-manifest updater from overwriting the live
  campaign's separately managed A/B subjects.
- Keep source discovery, property-event qualification, report improvements, and
  known-domain context from main.
- Retain sourced generic mailboxes and domain-mismatched fallback emails, rather
  than restoring older blanket exclusions. Prefer current-employer addresses and
  facilities authority; preserve explicit suppression, invalid-address rejection,
  evidence-backed identity exclusions, and exact-scope review holds.
- Keep budgeted, cached Treg recovery after Grok, run-scoped daily ingestion,
  historical event reconciliation, and protection for existing enrolled recipients.
- Keep sourced subject references and reply-threaded follow-ups. Live A/B state
  is separately pinned; a code merge must not reset the live campaign or its hash.

One-off backfill/repair scripts, research output, and test-send audit records are
local operational artifacts and are intentionally not part of this merge.
Production scheduling still uses the separate runtime snapshot documented in
README-AUTOMATION.md; merging this branch is not a runtime deployment.
