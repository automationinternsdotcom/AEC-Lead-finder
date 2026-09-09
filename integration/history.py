"""Grok-confirmed same-company, same-event reuse across daily runs."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from .ids import stable_uuid
from .models import CompanySync


def _words(value):
    return set(re.findall(r"[a-z0-9]{4,}", value.casefold())) - {
        "company", "companies", "group", "arizona", "phoenix", "development",
        "properties", "property", "commercial", "scottsdale", "construction",
    }


def apply_matches(db, handoff, matches):
    event_ids, company_ids = {}, {}
    for company in handoff.companies:
        if db.get_company(company.company_id):
            continue
        named = db.resolve_company_alias("name", company.canonical_name)
        domain = db.resolve_company_alias("domain", company.domain) if company.domain else None
        if not named:
            alias_targets = {target for alias in company.aliases
                             if (target := db.resolve_company_alias("name", alias))}
            if len(alias_targets) == 1:
                named = next(iter(alias_targets))
            elif domain in alias_targets:
                named = domain
        # A parent/government domain can be shared by unrelated projects. A
        # domain alone must never turn a county into a prior development entity.
        target = named
        if target:
            old = db.get_company(target)
            if named and old.get("domain") and company.domain and old["domain"].casefold() != company.domain.casefold():
                # A brand and its franchisee can share a display name. Keep the
                # independently sourced domain identity instead of conflating them.
                continue
            company_ids[company.company_id] = target
    historical_companies = {}
    for event in handoff.lead_events:
        match = matches.get(event.lead_event_id, {})
        if not (match.get("same_company") is True and match.get("same_event") is True
                and match.get("confidence") == "high" and match.get("existing_id")):
            continue
        old = db.get_lead_event(match["existing_id"])
        if not old or not old.get("pipedrive_lead_id") or old["crm_state"] == "archived":
            continue
        prior = historical_companies.get(event.company_id)
        if prior and prior != old["company_id"]:
            raise ValueError("Conflicting historical company matches")
        event_ids[event.lead_event_id] = old["lead_event_id"]
        company_ids[event.company_id] = old["company_id"]
        historical_companies[event.company_id] = old["company_id"]
    companies = {}
    for company in handoff.companies:
        target = company_ids.get(company.company_id)
        if target:
            company = CompanySync.model_validate(db.get_company(target)["payload"])
        companies[company.company_id] = company
    events = {}
    for event in handoff.lead_events:
        event = event.model_copy(update={
            "lead_event_id": event_ids.get(event.lead_event_id, event.lead_event_id),
            "company_id": company_ids.get(event.company_id, event.company_id),
        })
        events[event.lead_event_id] = event
    recipients = [r.model_copy(update={"company_id": company_ids.get(r.company_id, r.company_id)})
                  for r in handoff.recipients]
    sequences = {}
    for sequence in handoff.sequences:
        company_id = company_ids.get(sequence.company_id, sequence.company_id)
        updates = {"company_id": company_id,
                   "anchor_lead_event_id": event_ids.get(sequence.anchor_lead_event_id, sequence.anchor_lead_event_id),
                   "supporting_event_ids": list(dict.fromkeys(event_ids.get(e, e) for e in sequence.supporting_event_ids))}
        if company_id != sequence.company_id:
            with db.connection() as conn:
                existing = conn.execute("SELECT sequence_id FROM outreach_sequences WHERE company_id=? ORDER BY CASE WHEN approval_state='enrolled' THEN 0 ELSE 1 END LIMIT 1", (company_id,)).fetchone()
            updates["sequence_id"] = existing["sequence_id"] if existing else stable_uuid("outreach-sequence", company_id, sequence.campaign_protocol)
        sequence = sequence.model_copy(update=updates)
        sequences[sequence.sequence_id] = sequence
    return handoff.model_copy(update={"companies": list(companies.values()), "lead_events": list(events.values()),
                                     "recipients": recipients, "sequences": list(sequences.values())}), len(event_ids)


def reconcile_history(db, handoff):
    with db.connection() as conn:
        rows = conn.execute("SELECT lead_event_id,payload FROM sales_lead_events WHERE pipedrive_lead_id IS NOT NULL AND crm_state!='archived'").fetchall()
    history = {r["lead_event_id"]: json.loads(r["payload"]) for r in rows}
    inputs = {}
    for event in handoff.lead_events:
        if event.lead_event_id in history:
            continue
        words = _words(event.organization_name)
        candidates = [old for old in history.values() if words & _words(old["organization_name"])
                      or event.article_url == old.get("article_url")]
        candidates.sort(key=lambda old: -len(_words(event.organization_name + " " + event.event + " " + event.summary)
                                             & _words(old["organization_name"] + " " + old["event"] + " " + old.get("summary", ""))))
        if candidates:
            keys = ("lead_event_id", "organization_name", "event", "summary", "location", "date_posted", "article_url")
            inputs[event.lead_event_id] = {"new": {k: getattr(event, k) for k in keys},
                                          "existing": [{k: old.get(k) for k in keys} for old in candidates[:8]]}
    if not inputs:
        return apply_matches(db, handoff, {})
    material = json.dumps(inputs, sort_keys=True)
    cache_key = "daily:history:" + hashlib.sha256(material.encode()).hexdigest()
    cached = db.get_state(cache_key)
    if cached:
        matches = cached["matches"]
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scout"))
        import llm
        prompt = ("Check each NEW lead against its EXISTING CRM candidates. Merge only the same company in the same role AND the same specific property/project/transaction. "
                  "Later reporting of the same project is the same event. Separate properties, different buyer/seller/operator roles, and mere similar names are not matches. "
                  "Do not exclude a new opportunity on uncertainty: use existing_id null. Return a JSON object covering EVERY new ID exactly once, each with "
                  "existing_id (a supplied candidate ID or null), same_company boolean, same_event boolean, confidence high|medium|low, and reason.\n" + material)
        matches = llm.parse_json(llm.call("grok-4.3", prompt))
        if not isinstance(matches, dict) or set(matches) != set(inputs):
            raise ValueError("Historical dedup response did not cover every input")
        for new_id, match in matches.items():
            if match.get("existing_id") and match["existing_id"] not in {e["lead_event_id"] for e in inputs[new_id]["existing"]}:
                raise ValueError("Historical dedup returned an unknown keeper")
        db.set_state(cache_key, {"inputs": inputs, "matches": matches})
    return apply_matches(db, handoff, matches)
