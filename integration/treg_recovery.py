"""Recover missing recipients from existing opportunity state, never send email."""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from v2.contracts import ContactCandidate, Evidence, Organization, Person, VerificationStatus
from v2.ids import normalize_text, person_id, stable_uuid
from v2.state import StateStore
from v2.treg import RESOLVER_PROTOCOL, TregClient, TregResolver, domain_host, official_inboxes
from v2.verification import ContactVerifier
from .recipient_guard import IDENTITY_EXCLUSION, set_recipient_guard


def read_db(path):
    # immutable is safe only for a persisted database with no WAL. Live WAL
    # databases use SQLite's normal read transaction so committed WAL is included.
    suffix = "&immutable=1" if not Path(str(path) + "-wal").exists() else ""
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro" + suffix, uri=True)
    db.row_factory = sqlite3.Row
    return db


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str))
    os.chmod(temp, 0o600)
    temp.replace(path)


def load_reviewed_identities(output, plan):
    """Validate explicit identity corrections against the preserved source bytes.

    The reviewer attests to official-site ownership and employer-domain aliases;
    exact source/name/quote checks prevent unsupported or accidentally stale rows.
    These are target-company corrections, not permission to relabel a landlord.
    """
    path = Path(output) / 'reviewed-identities.json'
    if not path.exists():
        return {}
    targets = {t['company']['company_id']: t for t in plan['targets']}
    sources = defaultdict(list)
    db = read_db(Path(output) / 'treg-cache.sqlite')
    try:
        for row in db.execute("SELECT response FROM calls WHERE endpoint='exa.web.answer' AND status='done'"):
            for c in json.loads(row[0]).get('citations', []):
                sources[c.get('url', '').rstrip('/')].append(c.get('text') or '')
    finally:
        db.close()
    def norm(x): return ' '.join(x.casefold().split())
    result = {}
    for row in json.loads(path.read_text()):
        cid = row['company_id']
        target = targets[cid]['company']
        if cid in result:
            raise ValueError('duplicate reviewed company identity')
        domain = domain_host(row['domain'])
        text = '\n'.join(sources[row['source_url'].rstrip('/')])
        names = [target['canonical_name'], *target.get('aliases', []), *row.get('aliases', [])]
        if not domain or domain_host(row['source_url']) != domain or not row.get('relationship_note'):
            raise ValueError('reviewed identity requires official domain and relationship note')
        if len(row['source_quote']) < 25 or norm(row['source_quote']) not in norm(text) or not any(norm(n) in norm(text) for n in names):
            raise ValueError('reviewed identity name or quote absent from cached source')
        for inbox in row.get('inboxes', []):
            cited = '\n'.join(sources[inbox['source_url'].rstrip('/')])
            if domain_host(inbox['source_url']) != domain or not inbox.get('scope') or len(inbox['source_quote']) < 25:
                raise ValueError('company inbox requires official source, quote and scope')
            if norm(inbox['source_quote']) not in norm(cited) or inbox['email'].lower() not in cited.lower():
                raise ValueError('company inbox absent from official source')
        if any(not domain_host(d) or domain_host(d) != d for d in row.get('people_domains', [])):
            raise ValueError('invalid reviewed employer domain alias')
        result[cid] = {**row, 'accepted':True, 'domain':domain, 'official_source_url':row['source_url'],
            'inboxes': row.get('inboxes') or official_inboxes(text, row['source_url']),
            'evidence_quote':row['source_quote'], 'relationship':row['relationship_note'],
            'company_name':target['canonical_name'], 'validation':'reviewed_cached_official_source'}
    return result


def import_reviewed_contacts(sales_db, output, review_path, *, apply=False):
    """Import operator-reviewed named emails from already cached official sources.

    No model or paid provider calls. Preserve the cited source, previous result,
    reviewed input and exact batch handoff so the decision remains auditable.
    """
    import hashlib
    output = Path(output)
    rows = json.loads(Path(review_path).read_text())
    plan = json.loads((output / "inventory.json").read_text())
    if plan["sales_db"] != str(Path(sales_db).resolve()):
        raise ValueError("reviewed import sales DB does not match inventory")
    targets = {t["company"]["company_id"]: t for t in plan["targets"]}
    if len({r['company_id'] for r in rows}) != len(rows):
        raise ValueError("one primary reviewed contact per company is required")
    sources = defaultdict(list)
    cache = read_db(output / "treg-cache.sqlite")
    try:
        for row in cache.execute("SELECT response FROM calls WHERE endpoint='exa.web.answer' AND status='done'"):
            for citation in json.loads(row[0]).get("citations", []):
                sources[citation.get("url", "").rstrip("/")].append(citation.get("text", ""))
    finally:
        cache.close()
    live = read_db(sales_db)
    try:
        excluded = {r[0] for r in live.execute("SELECT email FROM suppressions")}
        excluded.update(r[0] for r in live.execute("SELECT normalized_email FROM sales_recipients WHERE verification_status='invalid'"))
        stopped_companies = {r[0] for r in live.execute("SELECT r.company_id FROM sales_recipients r JOIN suppressions s ON s.email=r.normalized_email WHERE s.reason!='invalid'")}
    finally:
        live.close()
    state = StateStore(str(output / "verification.sqlite")); state.migrate()
    verifier = ContactVerifier(state)
    results = []
    def norm(value): return " ".join(value.casefold().split())
    for row in rows:
        target = targets[row['company_id']]
        if row['company_id'] in stopped_companies:
            raise ValueError("company has a suppression; reviewed import cannot bypass it")
        email = row['email'].strip().lower()
        text = "\n".join(sources[row['source_url'].rstrip('/')])
        if len(row.get('source_quote','').strip()) < 25:
            raise ValueError("a substantive exact source quote is required")
        if domain_host(row['source_url']) != domain_host(row['domain']):
            raise ValueError("review source must be on the stated official corporate domain")
        if not all(norm(value) in norm(text) for value in [row['name'],email,row['source_quote']]):
            raise ValueError("reviewed person, email or exact quote is absent from cached source")
        if not row.get('relationship_note'):
            raise ValueError("operator must document the company/opportunity relationship")
        check = verifier.verify(email=email, organization_domain=row['domain'])
        if email in excluded or check.status == VerificationStatus.REJECTED:
            raise ValueError("reviewed email is suppressed or invalid")
        person = Person(person_id=person_id(row['name'],row['company_id']), organization_id=row['company_id'],
            name=row['name'],title=row['title'],scope=row.get('scope',''), evidence=[Evidence(url=row['source_url'],
            supports=row['relationship_note']+'; '+row['source_quote'],provider='treg:cached-official-source')])
        results.append({'status':'found','company_id':row['company_id'],'company':target['company']['canonical_name'],
            'person':person.model_dump(mode='json'),'email':email,'domain':row['domain'],'linkedin':'',
            'provider':'treg:cached-official-source-reviewed','reason':check.reason,
            'checked_at':time.time(),'resolver_protocol':RESOLVER_PROTOCOL,'opportunity_count':len(target['events'])})
    batch = output / ('reviewed-' + hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()[:12])
    write_json(batch / 'reviewed-input.json', rows)
    for result in results:
        old_path=output/'companies'/(result['company_id']+'.json')
        previous = batch/'previous-results'/(result['company_id']+'.json')
        if old_path.exists() and not previous.exists(): write_json(previous,json.loads(old_path.read_text()))
        write_json(old_path,result)
    handoff = build_handoff(plan,results,batch)
    sync = None
    if apply:
        from .database import Database
        from .handoff import enqueue_handoff
        sync=enqueue_handoff(Database(sales_db),handoff)
    summary={'processed':len(results),'paid_provider_calls':0,'handoff':str(handoff),'sync':sync}
    write_json(batch/'summary.json',summary)
    build_handoff(plan,[json.loads(f.read_text()) for f in (output/'companies').glob('*.json')],output)
    return summary


def inventory(repo, sales_db, output):
    """Join source research using exact company/legacy IDs, not fuzzy person names."""
    output = Path(output)
    source = read_db(sales_db)
    try:
        companies = [dict(r) for r in source.execute("""SELECT c.* FROM sales_companies c
          WHERE NOT EXISTS (SELECT 1 FROM sales_recipients r WHERE r.company_id=c.company_id
            AND r.verification_status!='invalid' AND TRIM(r.normalized_email)!='')
          AND NOT EXISTS (SELECT 1 FROM sales_recipients r JOIN suppressions s ON s.email=r.normalized_email
            WHERE r.company_id=c.company_id AND s.reason!='invalid')
          ORDER BY c.company_id""")]
        events = defaultdict(list)
        for row in source.execute("SELECT payload FROM sales_lead_events"):
            event = json.loads(row[0])
            events[event["company_id"]].append(event)
        excluded = {r[0] for r in source.execute("SELECT email FROM suppressions")}
        excluded.update(r[0] for r in source.execute("SELECT normalized_email FROM sales_recipients WHERE verification_status='invalid'"))
    finally:
        source.close()
    mapping = {}
    targets = {}
    for company in companies:
        payload = json.loads(company["payload"])
        cid = company["company_id"]
        for alias in [cid, *payload.get("legacy_ids", [])]:
            if alias in mapping and mapping[alias] != cid:
                raise ValueError("ambiguous historical organization ID")
            mapping[alias] = cid
        targets[cid] = {"company": payload, "events": events[cid], "people": {}, "contacts": [], "profiles": [], "reviews": []}
        # Preserve the database's most recent canonical attributes over old payloads.
        targets[cid]["company"].update(canonical_name=company["canonical_name"], domain=company["domain"])
    paths = [Path(repo) / "scout.db", *sorted((Path(repo) / "results/backfills").glob("*/state.sqlite"))]
    for path in paths:
        if not path.exists():
            continue
        db = read_db(path)
        try:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "v2_organizations" not in tables:
                continue
            orgs = {r["organization_id"]: json.loads(r["payload_json"]) for r in db.execute("SELECT organization_id,payload_json FROM v2_organizations")}
            local_map = dict(mapping)
            if "v2_company_profiles" in tables:
                for row in db.execute("SELECT payload_json FROM v2_company_profiles ORDER BY updated_at"):
                    p = json.loads(row[0])
                    cid = mapping.get(p["company_id"])
                    if not cid:
                        continue
                    targets[cid]["profiles"].append(p)
                    for oid in p.get("organization_ids", []):
                        if oid not in local_map or local_map[oid] == cid:
                            local_map[oid] = cid
            person_map = {}
            for row in db.execute("SELECT payload_json FROM v2_people"):
                p = json.loads(row[0])
                cid = local_map.get(p["organization_id"])
                if not cid:
                    continue
                original = p["person_id"]
                p.update(organization_id=cid, person_id=person_id(p["name"], cid))
                person_map[original] = (cid, p["person_id"])
                targets[cid]["people"][p["person_id"]] = p
                org = orgs.get(json.loads(row[0])["organization_id"], {})
                if not targets[cid]["company"].get("domain") and org.get("domain"):
                    targets[cid]["company"]["domain"] = org["domain"]
            for row in db.execute("SELECT payload_json FROM v2_contact_candidates"):
                c = json.loads(row[0])
                match = person_map.get(c["person_id"])
                if not match:
                    continue
                cid, pid = match
                c.update(person_id=pid, organization_id=cid)
                targets[cid]["contacts"].append(c)
            if "v2_review_items" in tables:
                for row in db.execute("SELECT payload_json FROM v2_review_items WHERE state='open'"):
                    review = json.loads(row[0])
                    for cid, target in targets.items():
                        if review["record_id"] in {cid, *(e["lead_event_id"] for e in target["events"])}:
                            target["reviews"].append(review)
        finally:
            db.close()
    # Bulk profiles are versioned files, not daily v2_company_profiles rows.
    for path in sorted((Path(repo) / "results").rglob("company_profiles.jsonl")):
        if "recipient-outreach-v4" not in path.parts:
            continue
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            p = json.loads(line)
            cid = mapping.get(p.get("company_id"))
            if cid and p.get("variants", {}).get("primary", {}).get("status") == "valid":
                targets[cid]["profiles"].append(p)
    rows = list(targets.values())
    for row in rows:
        row["people"] = list(row["people"].values())
        row["contacts"] = list({c["contact_candidate_id"]: c for c in row["contacts"]}.values())
        row["profiles"] = [p for p in row["profiles"] if p.get("why_line_status") == "valid" or p.get("variants", {}).get("primary", {}).get("status") == "valid"]
    rows.sort(key=lambda t: (not bool(t["company"].get("domain")), -max((e["score"] for e in t["events"]), default=0), t["company"]["company_id"]))
    plan = {"protocol": "treg-backlog-v1", "sales_db": str(Path(sales_db).resolve()), "targets": rows, "excluded_emails": sorted(excluded)}
    write_json(output / "inventory.json", plan)
    return plan


def recover(repo, sales_db, output, *, workers=3, budget_usd=5, limit=0, apply=False, refresh_inventory=False, cache_only=False,
            company_ids=()):
    output = Path(output)
    path = output / "inventory.json"
    plan = json.loads(path.read_text()) if path.exists() else inventory(repo, sales_db, output)
    if plan["sales_db"] != str(Path(sales_db).resolve()):
        raise ValueError("recovery sales database differs from saved inventory")
    if refresh_inventory:
        write_json(output / "inventory-history" / f"{time.time_ns()}.json", plan)
        fresh = inventory(repo, sales_db, output)
        merged = {t["company"]["company_id"]: t for t in plan["targets"]}
        merged.update({t["company"]["company_id"]: t for t in fresh["targets"]})
        plan.update(targets=list(merged.values()), excluded_emails=sorted(set(plan["excluded_emails"]) | set(fresh["excluded_emails"])))
        write_json(path, plan)
    exclusion_path = output/'identity-exclusions.json'
    identity_exclusions = json.loads(exclusion_path.read_text()) if exclusion_path.exists() else []
    excluded_by_company = defaultdict(set)
    for row in identity_exclusions:
        if row['company_id'] not in {t['company']['company_id'] for t in plan['targets']} or not row.get('reason') or not row.get('evidence'):
            raise ValueError('identity exclusion needs exact company, reason and evidence')
        excluded_by_company[row['company_id']].add(row['email'].lower())
    if apply and identity_exclusions:
        from .database import Database
        db = Database(str(sales_db))
        for row in identity_exclusions:
            cid, email = row['company_id'], row['email'].lower()
            set_recipient_guard(db, IDENTITY_EXCLUSION, cid, email, row)
            with db.connection() as conn:
                sequences = list(conn.execute('''SELECT s.sequence_id FROM outreach_sequences s
                    JOIN sales_recipients r ON r.recipient_id=s.primary_recipient_id
                    WHERE s.company_id=? AND r.normalized_email=?''',(cid,email)))
            for seq in sequences:
                db.update_sequence(seq[0],eligibility_status='blocked',eligibility_reasons=['company_identity_mismatch'])
                db.record_eligibility_decision(stable_uuid('identity-exclusion',cid,email),'sequence',seq[0],
                    'blocked',['company_identity_mismatch'],row)
    live = read_db(sales_db)
    try:
        excluded = set(plan["excluded_emails"])
        excluded.update(r[0] for r in live.execute("SELECT email FROM suppressions"))
        excluded.update(r[0] for r in live.execute("SELECT normalized_email FROM sales_recipients WHERE verification_status='invalid'"))
        live_recipients = {(r[0],r[1]) for r in live.execute('SELECT company_id,normalized_email FROM sales_recipients')}
        stopped_companies = {r[0] for r in live.execute("""SELECT DISTINCT r.company_id FROM sales_recipients r
            JOIN suppressions s ON s.email=r.normalized_email WHERE s.reason!='invalid'""")}
    finally:
        live.close()
    state = StateStore(str(output / "verification.sqlite"))
    state.migrate()
    company_ids = list(company_ids)
    if len(company_ids) != len(set(company_ids)):
        raise ValueError("duplicate --treg-company-id")
    if company_ids and limit:
        raise ValueError("--treg-company-id and --treg-limit are mutually exclusive")
    indexed_targets = {t["company"]["company_id"]: t for t in plan["targets"]}
    unknown_ids = [cid for cid in company_ids if cid not in indexed_targets]
    if unknown_ids:
        raise ValueError("unknown --treg-company-id: " + ", ".join(unknown_ids))
    targets = [indexed_targets[cid] for cid in company_ids] if company_ids else plan["targets"][:limit or None]
    client = TregClient(output / "treg-cache.sqlite", budget_usd=budget_usd, cache_only=cache_only)
    identities = load_reviewed_identities(output, plan)
    balance_before = None if cache_only else client.balance()
    changed_companies = set()

    def one(target):
        cid = target["company"]["company_id"]
        result_path = output / "companies" / (cid + ".json")
        import hashlib
        input_hash = hashlib.sha256(json.dumps({'target':target, 'identity':identities.get(cid),
            'identity_exclusions':[r for r in identity_exclusions if r['company_id']==cid]}, sort_keys=True).encode()).hexdigest()
        if cid in stopped_companies:
            return {"company_id": cid, "company": target["company"]["canonical_name"], "status": "suppressed"}
        if result_path.exists():
            old = json.loads(result_path.read_text())
            person_id = old.get("person", {}).get("person_id")
            grounded = bool(old.get("domain")) or any(p["person_id"] == person_id and not p.get("inferred_identity") for p in target["people"])
            if old["status"] in {"found", "existing_email"} and old.get("email") not in excluded | excluded_by_company[cid] and grounded:
                if (cid, old['email'].lower()) not in live_recipients:
                    changed_companies.add(cid)
                return old
            if old["status"] == "no_match" and old.get("input_hash") == input_hash and old.get("resolver_protocol") == RESOLVER_PROTOCOL and time.time() - old.get("checked_at", 0) < 30 * 86400:
                return old
            previous = output/'result-history'/f"{cid}-{old.get('checked_at', 0)}.json"
            if not previous.exists():
                write_json(previous, old)
        c = target["company"]
        org = Organization(organization_id=cid, canonical_name=c["canonical_name"], domain=c.get("domain", ""),
                           aliases=c.get("aliases", []), location="Arizona")
        resolver = TregResolver(client, ContactVerifier(state), excluded_emails=excluded | excluded_by_company[cid],
            excluded_domains=[r.get('domain','') for r in identity_exclusions if r['company_id']==cid])
        try:
            result = resolver.resolve(org, [Person.model_validate(p) for p in target["people"]],
                                     [ContactCandidate.model_validate(c) for c in target["contacts"]], opportunities=target["events"],
                                     reviewed_identity=identities.get(cid))
        except Exception as exc:
            result = {"status": "deferred", "errors": [type(exc).__name__ + ": " + str(exc)[:250]]}
        result.update(company_id=cid, company=c["canonical_name"], opportunity_count=len(target["events"]), checked_at=time.time(), resolver_protocol=RESOLVER_PROTOCOL, input_hash=input_hash)
        write_json(result_path, result)
        if result['status'] in {'found', 'existing_email'}:
            changed_companies.add(cid)
        return result

    results = []
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(one, t) for t in targets]
            for future in as_completed(futures):
                results.append(future.result())
                if len(results) % 25 == 0 or len(results) == len(targets):
                    print(json.dumps({"processed": len(results), "total": len(targets), "outcomes": dict(Counter(r["status"] for r in results))}), flush=True)
        # An exact priority batch updates only those companies while preserving the
        # full saved cohort in exports and review queues. A limit retains its legacy
        # pilot semantics; it is not a reviewed selection mechanism.
        report_results = results
        if company_ids:
            report_results = [json.loads(path.read_text()) for path in (output / "companies").glob("*.json")]
        handoff = build_handoff(plan, report_results, output)
        sync = None
        if apply:
            import hashlib
            from .database import Database
            from .handoff import enqueue_handoff
            changed = [r for r in results if r['company_id'] in changed_companies]
            if changed:
                batch = output / ('recovery-batch-' + hashlib.sha256(json.dumps(changed,sort_keys=True).encode()).hexdigest()[:12])
                batch_handoff = build_handoff(plan, changed, batch)
                sync = enqueue_handoff(Database(str(sales_db)), batch_handoff)
            else:
                sync = {'lead_jobs':0, 'sequence_jobs':0}
        write_json(output/'identity-review-queue.json', [
            {'company_id':r['company_id'], 'company':r['company'], 'domain':r.get('domain',''),
             'identity_context':r.get('identity_context'), 'errors':r.get('errors',[])}
            for r in report_results if r['status'] not in {'found','existing_email'}])
        write_json(output/'recovered-identity-audit.json', [
            {'company_id':r['company_id'], 'company':r['company'], 'email':r.get('email'),
             'domain':r.get('domain',''), 'reason':'legacy_domain_requires_corroboration_before_outreach'}
            for r in report_results if r['status'] in {'found','existing_email'} and r['company_id'] not in identities
            and not (r.get('identity_context') or {}).get('accepted')
            and not r.get('provider','').startswith('treg:cached-official-source-reviewed')])
        summary = {"targets": len(plan["targets"]), "processed": len(results),
                   "outcomes": dict(Counter(r["status"] for r in results)), "accounting": client.stats(),
                   "balance_before_micro": balance_before, "balance_after_micro": None if cache_only else client.balance(),
                   "cache_only":cache_only,
                   "handoff": str(handoff), "sync": sync}
        if company_ids:
            summary.update(selected_company_ids=company_ids,
                           cohort_outcomes=dict(Counter(r["status"] for r in report_results)))
        write_json(output / "summary.json", summary)
        return summary
    finally:
        client.close()


def build_handoff(plan, results, output):
    from .handoff import HANDOFF_SCHEMA_VERSION, HANDOFF_PROTOCOL_VERSION, handoff_content_hash, load_handoff
    from .models import CompanySync, LeadEventSync, RecipientSync, OutreachSequenceSync, SalesHandoff
    from v2.outreach import score_recipient_role
    from bulk_lib import _first_name as first_name, _profile_why_line, CompanyProfile, _personalize_why_line, WhyVariant
    import hashlib

    companies, events, recipients, sequences = [], [], [], []
    indexed = {t["company"]["company_id"]: t for t in plan["targets"]}
    run_id = Path(output).name
    source = read_db(plan["sales_db"])
    try:
        existing_sequences = {r['company_id']: dict(r) for r in source.execute("SELECT company_id,sequence_id,json_extract(payload,'$.run_id') AS run_id,approval_state,eligibility_status,eligibility_reasons FROM outreach_sequences")}
        existing_recipients = {(r['company_id'], r['person_id'], r['normalized_email']): r['recipient_id'] for r in source.execute("SELECT company_id,person_id,normalized_email,recipient_id FROM sales_recipients")}
        existing_person_ids = {(cid, pid): rid for (cid, pid, email), rid in existing_recipients.items()}
        used_emails = defaultdict(set)
        for row in source.execute("SELECT normalized_email,company_id FROM sales_recipients"):
            used_emails[row[0]].add(row[1])
        email_winners = {}
        for row in source.execute("""SELECT r.normalized_email,s.company_id FROM outreach_sequences s
            JOIN sales_recipients r ON r.recipient_id=s.primary_recipient_id
            WHERE s.eligibility_status='ready' OR s.approval_state!='draft'
            ORDER BY CASE WHEN s.approval_state!='draft' THEN 0 ELSE 1 END,s.created_at,s.company_id"""):
            email_winners.setdefault(row[0],row[1])
        excluded = {r[0] for r in source.execute("SELECT email FROM suppressions")}
        excluded.update(r[0] for r in source.execute("SELECT normalized_email FROM sales_recipients WHERE verification_status='invalid'"))
        identity_excluded_pairs = {(json.loads(r[0])['company_id'],json.loads(r[0])['email'].lower())
            for r in source.execute("SELECT value FROM app_state WHERE key LIKE 'identity-exclusion:%'")}
    finally:
        source.close()
    for email, owners in used_emails.items():
        outside_cohort = owners - set(indexed)
        if outside_cohort:
            email_winners.setdefault(email, min(outside_cohort))
    for result in sorted(results,key=lambda r:r['company_id']):
        if result['status'] in {'found','existing_email'}:
            email_winners.setdefault(result['email'].lower(),result['company_id'])
    for result in sorted(results, key=lambda r: r["company_id"]):
        if result["status"] not in {"found", "existing_email"}:
            continue
        if result["email"].lower() in excluded:
            continue
        cid = result["company_id"]
        if (cid, result['email'].lower()) in identity_excluded_pairs:
            continue
        target = indexed[cid]
        if not (result.get("domain") or target["company"].get("domain")) and not any(p["person_id"] == result.get("person", {}).get("person_id") and not p.get("inferred_identity") for p in target.get("people", [])):
            continue
        company = CompanySync.model_validate(target["company"])
        if result.get("domain") and not company.domain:
            company = company.model_copy(update={"domain": result["domain"]})
        companies.append(company)
        local_events = [LeadEventSync.model_validate(e) for e in target["events"]]
        events.extend(local_events)
        person = Person.model_validate(result["person"])
        first = 'team' if person.scope.startswith('company_mailbox:') else first_name(person.name)
        role, rationale = score_recipient_role(person.title, person.scope)
        recipient_id = existing_recipients.get((cid, person.person_id, result["email"]))
        if not recipient_id:
            existing = existing_sequences.get(cid)
            if (cid, person.person_id) in existing_person_ids and existing and existing['approval_state'] != 'draft':
                continue
            # One recipient per company/person. Database upsert archives the old
            # mailbox and resets verification/mappings when its address changes.
            recipient_id = existing_person_ids.get((cid, person.person_id)) or stable_uuid("recipient", cid, person.person_id)
        recipient = RecipientSync(recipient_id=recipient_id, company_id=cid,
            person_id=person.person_id, contact_candidate_id=stable_uuid("treg-contact", cid, person.person_id, result["email"]),
            full_name=person.name, first_name=first or "unknown", title=person.title, scope=person.scope,
            email=result["email"], source_provider=result["provider"], source_verification_status="unknown",
            source_verification_reason=result["reason"], role_score=role, rank=1, primary=True,
            selection_rationale=list(dict.fromkeys([
                *rationale,
                *(result.get("quality_signals") or []),
                "sole_email_role_fallback",
                "treg_after_grok",
            ])))
        recipients.append(recipient)
        # Preserve every existing sequence/approval; recovery never changes enrollment.
        existing = existing_sequences.get(cid)
        replace_invalid = existing and existing['approval_state'] == 'draft' and existing['eligibility_status'] == 'blocked' and json.loads(existing['eligibility_reasons']) == ['warmy_verification_invalid']
        resume_own_draft = existing and existing['approval_state'] == 'draft' and existing['run_id'] == run_id
        if existing and not (replace_invalid or resume_own_draft):
            continue
        valid_ids = {e.lead_event_id for e in local_events}
        profiles = [p for p in target["profiles"] if p.get("anchor_lead_event_id") in valid_ids]
        if not profiles:
            continue
        raw_profile = profiles[-1]
        if "variants" in raw_profile:
            profile = CompanyProfile.model_validate(raw_profile)
            why = _profile_why_line(profile)
        else:
            from v2.contracts import CompanyProfile as DailyProfile
            profile = DailyProfile.model_validate(raw_profile)
            why = WhyVariant(text=profile.why_line, template_key=profile.why_template_key, slots=profile.why_slots,
                             confidence=profile.why_confidence, source_urls=profile.why_sources, status=profile.why_line_status)
        if why.status != "valid":
            continue
        anchor = next(e for e in local_events if e.lead_event_id == profile.anchor_lead_event_id)
        reasons = list(anchor.crm_exclusion_reasons)
        if not anchor.crm_eligible or anchor.record_status != "valid" or anchor.confidence != "high" or anchor.score <= 0:
            reasons.append("anchor_not_eligible")
        if target["reviews"]:
            reasons.append("blocking_open_review")
        if not first:
            reasons.append("recipient_first_name_missing")
        if email_winners[result["email"].lower()] != cid:
            reasons.append("duplicate_primary_email")
        text = _personalize_why_line(why.text, first or "unknown")
        merge = {"firstName": first or "unknown", "company": company.canonical_name, "whyLine": text, "unsubscribeUrl": "__integration_generated__"}
        sequences.append(OutreachSequenceSync(sequence_id=existing['sequence_id'] if existing else stable_uuid("outreach-sequence", cid, HANDOFF_PROTOCOL_VERSION),
            run_id=run_id, company_id=cid, campaign_protocol=HANDOFF_PROTOCOL_VERSION, anchor_lead_event_id=anchor.lead_event_id,
            supporting_event_ids=[e.lead_event_id for e in local_events if e != anchor], primary_recipient_id=recipient.recipient_id,
            why_template_key=why.template_key, why_slots=why.slots, why_sources=why.source_urls, why_confidence=why.confidence,
            company_why_line=why.text, personalized_why_line=text, merge_snapshot=merge,
            merge_hash=hashlib.sha256(json.dumps(merge, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            eligibility_status="blocked" if reasons else "ready", eligibility_reasons=sorted(set(reasons))))
    value = SalesHandoff(schema_version=HANDOFF_SCHEMA_VERSION, protocol_version=HANDOFF_PROTOCOL_VERSION,
                        run_id=run_id, companies=companies, lead_events=events, recipients=recipients, sequences=sequences, content_hash="pending")
    value = value.model_copy(update={"content_hash": handoff_content_hash(value)})
    path = Path(output) / "sales_handoff.json"
    write_json(path, value.model_dump(mode="json"))
    load_handoff(path)
    csv_path = Path(output) / "leads.csv"
    with csv_path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["company", "status", "full_name", "title", "email", "linkedin", "provider", "domain", "opportunity_count"])
        writer.writeheader()
        for r in results:
            writer.writerow({k: (r.get("person", {}).get("name") if k == "full_name" else r.get("person", {}).get("title") if k == "title" else r.get(k, "")) for k in writer.fieldnames})
    os.chmod(csv_path, 0o600)
    clean_path = Path(output) / "recovered-leads.csv"
    company_names = {c.company_id: c.canonical_name for c in companies}
    sequence_by_company = {s.company_id: s for s in sequences}
    result_by_company = {r['company_id']:r for r in results}
    with clean_path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["company", "full_name", "title", "scope", "email", "provider", "identity_review", "source_urls", "eligibility", "eligibility_reasons"])
        writer.writeheader()
        for recipient in recipients:
            seq = sequence_by_company.get(recipient.company_id)
            result = result_by_company[recipient.company_id]
            writer.writerow({"company":company_names[recipient.company_id], "full_name":recipient.full_name,
                "title":recipient.title, "scope":recipient.scope, "email":recipient.email, "provider":recipient.source_provider,
                "identity_review":"corroborated" if (result.get('identity_context') or {}).get('accepted') or result.get('provider','').startswith('treg:cached-official-source-reviewed') else "needs_domain_review",
                "source_urls":"; ".join(sorted({e['url'] for e in result.get('person',{}).get('evidence',[]) if e.get('url')})),
                "eligibility":seq.eligibility_status.value if seq else "existing_sequence_preserved",
                "eligibility_reasons":"; ".join(seq.eligibility_reasons) if seq else ""})
    os.chmod(clean_path, 0o600)
    return path
