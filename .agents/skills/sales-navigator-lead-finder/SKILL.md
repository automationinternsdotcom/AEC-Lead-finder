---
name: sales-navigator-lead-finder
description: Find, research, qualify, and prioritize B2B leads for Automation Interns in LinkedIn Sales Navigator. Use for prospect searches, saved-search review, lead-list building, account mapping, and outreach-ready shortlists; use linkedin-manager for sending or managing conversations.
---

# Sales Navigator Lead Finder

Build a small, evidence-backed prospect queue that is useful for human-quality outreach. Optimize for relevance and timing rather than list volume.

## Working mode

Use the user's existing logged-in Sales Navigator session. Inspect the live page before relying on saved searches, personas, lists, result counts, or filters because account state changes over time.

Read [references/automation-interns-playbook.md](references/automation-interns-playbook.md) when the user has not supplied a complete ICP, when scoring or prioritizing prospects, or when operating the established Automation Interns searches.

For Aether Facility Services lead generation, also read
[references/aether-facility-services-icp.md](references/aether-facility-services-icp.md).
It narrows the buyer hypotheses for commercial-property, hospitality, industrial,
and facilities-services prospecting.

## Lead-finding workflow

1. Translate the request into an account hypothesis and one or more buyer hypotheses. Preserve explicit industries, geographies, sizes, titles, and exclusions.
2. Reuse the narrowest relevant saved search or persona when it exists. Otherwise, construct a search with the minimum filters needed to test the hypothesis.
3. Qualify accounts before collecting people. Confirm current company size, location, business model, and a plausible automation need or timing signal.
4. Find two or three relevant people at strong accounts when possible: an economic buyer, the workflow owner, and a technical or operational evaluator. Do not pad the list with weak titles.
5. Open enough profile, company, and recent-activity context to verify each material claim. Treat snippets as discovery cues, not final evidence.
6. Score prospects with the playbook, remove hard exclusions, and return the strongest leads first.
7. Save leads or accounts only when the user asked for Sales Navigator organization or when saving is an ordinary part of the requested lead-building task. Use a clearly named list and avoid duplicates.

## Evidence and output

For each recommended lead, provide:

- name, current title, company, location, and Sales Navigator or LinkedIn URL;
- account fit and likely buyer role;
- observed timing or relevance signal, with its source and date when visible;
- one defensible personalization angle;
- score, confidence, and any unknowns or disqualifiers;
- recommended next action.

Keep facts, inferences, and suggested messaging distinct. Do not invent revenue, headcount, technologies, pain, budget, authority, or personal details.

Prefer a focused queue of 10–25 verified leads over a large unreviewed export. If the user requests a larger batch, work in reviewable chunks and preserve deduplication keys such as profile URL plus company.

## Action boundaries

Lead research and ranking do not authorize messaging, connection requests, InMail, email, or other representational actions. Drafting is allowed when requested; obtain action-time confirmation before sending unless the current request explicitly authorizes the exact outbound action.

Do not bypass LinkedIn controls, verification prompts, rate limits, or access restrictions. Avoid mass actions and unattended scraping. Stop if the account encounters a security challenge or if search state cannot be verified.

Use `linkedin-manager` for conversation history, replies, follow-ups, sending, and optional durable run records.
