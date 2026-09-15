# Targeted outreach grammar review

`scout.copy_grammar.review_copy` runs on daily and bulk rendered why lines,
Warmy prospect handoff, and campaign subject/plain-text/HTML validation.
Generation prompts ask for a grammar-only pass on the assembled sentence and
grammatical insertion slots. Proper-name capitalization is preserved rather
than forced to lowercase. Grammar does not add an eligibility gate.

Repairs are deliberately bounded: missing articles in known constructions,
incomplete greetings, supplied reference capitalization, common Arizona
locality capitalization, and numeric square-foot modifiers. The function is
idempotent and protects URLs, merge tokens, and HTML tags. It does not rewrite
facts, change tone or calls to action, or claim to catch all English errors.
Ambiguous constructions need editorial review; extend rules only with positive
and unchanged-text regression cases.

For existing campaigns, resolve the exact enrolled IDs, snapshot all fields,
review a before/after plan, then patch only changed copy while preserving custom
fields and campaign settings. Check fresh values for concurrent edits before
applying and reread persisted fields afterward. Pace writes to the provider's
current limits; stop on explicit rate limits and record pending IDs. Never
resume a campaign as part of proofreading.
