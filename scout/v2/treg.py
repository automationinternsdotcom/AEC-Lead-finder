"""Budgeted, resumable Treg recovery after public contact research is exhausted.

Cache identity is the exact request, independent of run/event. A pending request
retains its reservation and idempotency key after an interrupted response.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from recipient_policy import assess_recipient, current_employer_match

from .contracts import ContactCandidate, Evidence, Person, ReviewItem, VerificationStatus
from .ids import person_id, stable_uuid
from .verification import select_best


PROTOCOL = "treg-recovery-v1"
RESOLVER_PROTOCOL = "treg-resolver-v6"
ROLE_TERMS = ["facilities", "property manager", "asset manager", "real estate", "procurement", "operations"]


class TregDeferred(RuntimeError):
    """Recoverable budget, authorization, or provider failure, never a no-match."""


def domain_host(value: str) -> str:
    from urllib.parse import urlsplit
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = urlsplit(value.strip() if "://" in value else "https://" + value.strip())
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            return ""
        return host if re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) and "." in host and ".." not in host else ""
    except ValueError:
        return ""


def domain_matches(value, domain):
    host = domain_host(value)
    return bool(host and domain and (host == domain or host.endswith("." + domain)))


def official_inboxes(text, source_url):
    """Extract only recognizable shared mailboxes from already trusted source text."""
    inboxes = []
    for email in sorted(set(re.findall(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', text))):
        local = email.split('@')[0].lower()
        if not re.search(r'(?:^|[_.-])(info|contact|hello|office|sales|supplier|procurement|admin)(?:$|[_.-])', local):
            continue
        start = text.find(email)
        inboxes.append({'email':email.lower(), 'source_url':source_url,
            'source_quote':text[max(0,start-100):start+len(email)+100],
            'scope':'Published company contact mailbox; not a named individual'})
    return inboxes


def same_company(a: str, b: str) -> bool:
    def key(value):
        return " ".join(w for w in re.findall(r"[a-z0-9]+", value.lower()) if w not in {"inc", "llc", "ltd", "corporation", "corp", "the"})
    return bool(key(a)) and key(a) == key(b)


class TregClient:
    def __init__(self, cache_path, *, budget_usd=5.0, balance_floor_usd=5.05,
                 email_cap_usd=0.025, token=None, transport=None, cache_only=False):
        self.path = Path(cache_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.budget = round(budget_usd * 1_000_000)
        self.floor = round(balance_floor_usd * 1_000_000)
        self.email_cap = email_cap_usd
        self.cache_only = cache_only
        self.catalog_checked = {}
        if not 0 < email_cap_usd <= 0.025 or budget_usd <= 0 or balance_floor_usd < 0:
            raise ValueError("invalid Treg spend limits")
        cfg = {}
        config_path = Path.home() / ".treg/config.json"
        if token is None and config_path.exists():
            cfg = json.loads(config_path.read_text())
        token = token or os.environ.get("TREG_TOKEN") or cfg.get("token")
        if not token and not cache_only:
            raise TregDeferred("Treg login required")
        token = token or "cache-only-no-network"
        self.http = httpx.Client(base_url="https://treg.to", timeout=180,
                                 headers={"X-Treg-Token": token, "Authorization": "Bearer " + token}, transport=transport)
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS calls (
                request_key TEXT PRIMARY KEY, endpoint TEXT NOT NULL, request TEXT NOT NULL,
                status TEXT NOT NULL, response TEXT, reserved_micro INTEGER NOT NULL,
                charged_micro INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL,
                idempotency_key TEXT NOT NULL, error TEXT NOT NULL DEFAULT '')""")
            # Older conservative rows can be reconciled from the provider's
            # stored authoritative charge without repeating a paid request.
            for row in db.execute("SELECT request_key,response FROM calls WHERE status='done'").fetchall():
                payload = json.loads(row["response"])
                actual = payload.get("_treg", {}).get("charged_micro") if isinstance(payload, dict) else None
                if actual is not None:
                    db.execute("UPDATE calls SET charged_micro=? WHERE request_key=?", (int(actual), row["request_key"]))
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=60)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def close(self):
        self.http.close()

    def balance(self):
        # Read-only MCP balance avoids requiring the account's organization ID.
        response = self.http.post("/mcp/", headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "balance", "arguments": {}}})
        response.raise_for_status()
        payload = response.json().get("result", {})
        if payload.get("isError"):
            raise TregDeferred("Treg balance lookup failed")
        value = payload.get("structuredContent")
        if not value:
            value = next((json.loads(x["text"]) for x in payload.get("content", []) if x.get("type") == "text"), {})
        if "balance_micro" not in value:
            raise TregDeferred("Treg balance response lacks balance_micro")
        return int(value["balance_micro"])

    def call(self, endpoint, params, *, ceiling=0.0, method="POST"):
        material = json.dumps({"protocol": PROTOCOL, "endpoint": endpoint, "method": method,
                               "params": params, "email_cap": self.email_cap}, sort_keys=True)
        key = hashlib.sha256(material.encode()).hexdigest()
        ceiling_micro = round(ceiling * 1_000_000)
        with self.connection() as db:
            row = db.execute("SELECT * FROM calls WHERE request_key=?", (key,)).fetchone()
            # Refresh completed lookups after 30 days, preserving the original
            # ledger and charge. Uncertain requests always retain their old key.
            if row and row["status"] == "done" and time.time() - row["updated"] >= 30 * 86400:
                generation = int(time.time() // (30 * 86400))
                key = hashlib.sha256((material + f":refresh:{generation}").encode()).hexdigest()
                row = db.execute("SELECT * FROM calls WHERE request_key=?", (key,)).fetchone()
            if row and row["status"] == "done":
                return json.loads(row["response"])
        if self.cache_only:
            raise TregDeferred("cache_only_miss:" + endpoint)
        if endpoint not in self.catalog_checked:
            catalog = self.http.get("/catalog/endpoints/" + endpoint)
            catalog.raise_for_status()
            info = catalog.json()
            self.catalog_checked[endpoint] = (info.get("endpoint") or info).get("cost") or {}
        cost = self.catalog_checked[endpoint]
        price = cost.get('usd')
        multiplier = params.get('pagination', {}).get('size', params.get('limit', 1)) if cost.get('unit') == 'result' else 1
        if price is None or float(price) * multiplier > ceiling + 0.0000001:
            raise TregDeferred(f"{endpoint}: catalog price exceeds declared ceiling")
        if ceiling_micro:
            available = self.balance()
            if available - ceiling_micro < self.floor:
                raise TregDeferred("Treg balance floor reached")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM calls WHERE request_key=?", (key,)).fetchone()
            if row and row["status"] == "done":
                return json.loads(row["response"])
            if row and row["status"] == "running" and time.time() - row["updated"] < 240:
                raise TregDeferred("Treg request already in flight")
            spent = db.execute("SELECT COALESCE(SUM(charged_micro + reserved_micro),0) FROM calls").fetchone()[0]
            own_reservation = row["reserved_micro"] if row else 0
            reserved = db.execute("SELECT COALESCE(SUM(reserved_micro),0) FROM calls").fetchone()[0]
            if ceiling_micro and available - (reserved - own_reservation) - ceiling_micro < self.floor:
                raise TregDeferred("Treg balance floor reached with in-flight reservations")
            if spent - own_reservation + ceiling_micro > self.budget:
                raise TregDeferred("Treg recovery budget reached")
            idem = row["idempotency_key"] if row else "aec-" + key
            db.execute("""INSERT INTO calls VALUES (?,?,?,'running',NULL,?,0,?,?,'')
                ON CONFLICT(request_key) DO UPDATE SET status='running', updated=excluded.updated,
                reserved_micro=excluded.reserved_micro""", (key, endpoint, material, ceiling_micro, time.time(), idem))
        try:
            headers = {"Idempotency-Key": idem, "X-Treg-Route-Max-Cost": str(self.email_cap)}
            response = self.http.request(method, "/call/" + endpoint, headers=headers,
                **({"params": params} if method == "GET" else {"json": params}))
            if response.status_code >= 400:
                cost_micro = response.headers.get("x-treg-cost-micro")
                with self.connection() as db:
                    db.execute("UPDATE calls SET reserved_micro=0, charged_micro=? WHERE request_key=?", (int(cost_micro or 0), key))
                raise TregDeferred(f"{endpoint}: HTTP {response.status_code}")
            value = response.json()
            # Preserve unknown accounting conservatively rather than treating it as free.
            cost = response.headers.get("x-treg-cost-micro")
            meta = value.get("_treg", {}) if isinstance(value, dict) else {}
            charged = int(cost) if cost is not None else int(meta.get("charged_micro", ceiling_micro))
            with self.connection() as db:
                db.execute("UPDATE calls SET status='done', response=?, reserved_micro=0, charged_micro=?, updated=? WHERE request_key=?",
                           (json.dumps(value), charged, time.time(), key))
            return value
        except Exception as exc:
            with self.connection() as db:
                db.execute("UPDATE calls SET status='deferred', error=?, updated=? WHERE request_key=?",
                           (type(exc).__name__ + ": " + str(exc)[:200], time.time(), key))
            if isinstance(exc, TregDeferred):
                raise
            raise TregDeferred(f"{endpoint}: {type(exc).__name__}") from exc

    def stats(self):
        with self.connection() as db:
            return dict(db.execute("SELECT COUNT(*) AS calls, COALESCE(SUM(charged_micro),0) AS charged_upper_bound_micro, COALESCE(SUM(reserved_micro),0) AS reserved_micro FROM calls").fetchone())

    def cached_calls(self, endpoint):
        with self.connection() as db:
            return [(json.loads(r['request'])['params'], json.loads(r['response'])) for r in db.execute(
                "SELECT request,response FROM calls WHERE endpoint=? AND status='done' AND updated>?",
                (endpoint, time.time() - 30 * 86400))]


class TregResolver:
    def __init__(self, client, verifier, *, excluded_emails=(), excluded_domains=()):
        self.client = client
        self.verifier = verifier
        self.excluded = {x.strip().lower() for x in excluded_emails}
        self.excluded_domains = {domain_host(x) for x in excluded_domains}

    def resolve(self, organization, people=(), contacts=(), *, opportunities=(), reviewed_identity=None):
        """One usable email per organization; role preference is never a hard gate."""
        from .outreach import score_recipient_role

        domain = domain_host(organization.domain) if organization.domain else ""
        people = list(people)
        contacts = list(contacts)
        identity_context = None
        existing_fallbacks = []
        existing_email_count = len({
            contact.email.strip().casefold()
            for contact in contacts
            if contact.email
            and contact.verification_status != VerificationStatus.REJECTED
            and contact.email.strip().casefold() not in self.excluded
        })
        for contact in contacts:
            if contact.email and contact.verification_status != VerificationStatus.REJECTED and contact.email.lower() not in self.excluded:
                person = next((p for p in people if p.person_id == contact.person_id), None)
                verification = self.verifier.verify(email=contact.email, organization_domain=domain)
                if person and not person.inferred_identity and verification.status != VerificationStatus.REJECTED:
                    assessment = assess_recipient(
                        verification_status=verification.status.value,
                        verification_reason=verification.reason or contact.verification_reason,
                        role_score=score_recipient_role(person.title, person.scope)[0],
                        alternative_email_count=existing_email_count,
                    )
                    result = {"status": "existing_email", "person": person.model_dump(mode="json"),
                              "email": verification.email or contact.email, "linkedin": contact.linkedin,
                              "domain": domain, "provider": contact.provider,
                              "reason": verification.reason or contact.verification_reason,
                              "quality_signals": list(assessment.quality_signals)}
                    existing_fallbacks.append(result)
                    # With no opportunity identity to corroborate, a known-domain
                    # match (or the only sourced person when no domain exists) is
                    # already the best cost-free result.
                    if not opportunities and current_employer_match(result["email"], domain) is not False:
                        return result
        deferred = []
        proposed_domains = {domain} if domain else set()
        # Historical/provider domains are hints until tied to this opportunity.
        if opportunities:
            domain = ""
        def invoke(*args, **kwargs):
            try:
                return self.client.call(*args, **kwargs)
            except TregDeferred as exc:
                deferred.append(str(exc))
                return {}

        # A verified exact name match may supply the domain; ambiguous names stay unresolved.
        if not reviewed_identity and not domain and len(organization.canonical_name) >= 3:
            lookup = invoke("hunter.x.domain-finder", {"company": organization.canonical_name, "limit": 3, "perfect_match": "true"}, method="GET")
            matches = [x for x in lookup.get("data", []) if same_company(x.get("company_name", ""), organization.canonical_name)]
            domains = {domain_host(x["domain"]) for x in matches if x.get("domain")}
            if len(domains) == 1:
                proposed_domains.update(domains)
                if not opportunities:
                    domain = domains.pop()
        if not reviewed_identity and not domain:
            lookup = invoke("companyenrich.companies.autocomplete", {"query": organization.canonical_name}, method="GET")
            rows = lookup if isinstance(lookup, list) else []
            domains = {domain_host(x["domain"]) for x in rows if isinstance(x, dict) and x.get("domain") and same_company(x.get("name", ""), organization.canonical_name)}
            if len(domains) == 1:
                proposed_domains.update(domains)
                if not opportunities:
                    domain = domains.pop()

        if reviewed_identity:
            if reviewed_identity['domain'] in self.excluded_domains:
                raise ValueError('reviewed identity conflicts with rejected company domain')
            domain = reviewed_identity['domain']
            identity_context = reviewed_identity
            organization = organization.model_copy(update={'aliases': [*organization.aliases, *reviewed_identity.get('aliases', [])]})

        if not domain and opportunities:
            try:
                identity_context = research_organization_identity(self.client, organization, opportunities,
                    domain_hints=sorted(proposed_domains - self.excluded_domains))
                if identity_context.get('domain') in self.excluded_domains:
                    identity_context = {**identity_context,'accepted':False,'validation':'rejected_company_domain'}
                if identity_context.get('accepted'):
                    domain = identity_context['domain']
            except TregDeferred as exc:
                deferred.append(str(exc))

        # Re-evaluate existing addresses against the opportunity-corroborated
        # employer domain. Exact current-domain contacts avoid paid lookups;
        # mismatches remain fallbacks while a better address is researched.
        for result in existing_fallbacks:
            result["domain"] = domain
            employer_match = current_employer_match(result["email"], domain)
            signals = list(result.get("quality_signals") or [])
            if employer_match is False:
                signals.append("organization_domain_mismatch")
            elif employer_match is None:
                signals.append("company_domain_unresolved")
            result["quality_signals"] = list(dict.fromkeys(signals))
            if employer_match is True:
                return result

        candidates = []
        for person in people:
            if person.inferred_identity:
                continue
            linkedin = next((c.linkedin for c in contacts if c.person_id == person.person_id and c.linkedin), "")
            if not linkedin:
                linkedin = next((e.url for e in person.evidence if "linkedin.com/in/" in e.url), "")
            candidates.append({"person": person, "linkedin": linkedin})
        # Reuse already-paid people only after employer domain corroboration.
        if domain and hasattr(self.client, 'cached_calls'):
            for _, response in self.client.cached_calls('icypeas.people.search'):
                for row in response.get('leads', []):
                    employer_domains = [domain, *(identity_context or {}).get('people_domains', [])]
                    if not any(domain_matches(row.get('lastCompanyWebsite', ''), d) for d in employer_domains):
                        continue
                    name = ' '.join(str(row.get(k) or '').strip() for k in ('firstname', 'lastname')).strip()
                    linkedin = row.get('profileUrl') or ''
                    if not name or not linkedin:
                        continue
                    candidates.append({'person': Person(person_id=person_id(name, organization.organization_id),
                        organization_id=organization.organization_id, name=name, title=row.get('lastJobTitle') or '',
                        scope=row.get('address') or '', evidence=[Evidence(url=linkedin,
                        supports='Cached Treg person with corroborated employer domain', provider='treg:icypeas')]), 'linkedin': linkedin})
        tried = set()
        provider_fallbacks = []

        def find_email(candidate):
            person, linkedin = candidate["person"], candidate["linkedin"]
            identity = {"full_name": person.name}
            if domain:
                identity["domain"] = domain
            if linkedin:
                identity["linkedin_url"] = linkedin
            if not domain and not linkedin:
                return None
            identity_key = json.dumps(identity, sort_keys=True)
            if identity_key in tried:
                return None
            tried.add(identity_key)
            # An earlier LinkedIn-only reveal is reusable after current employer
            # identity is corroborated; adding a domain must not force repurchase.
            if linkedin and hasattr(self.client, 'cached_calls'):
                for old_params, _ in self.client.cached_calls('treg.people.email.find'):
                    if old_params.get('linkedin_url', '').rstrip('/') == linkedin.rstrip('/') and old_params.get('full_name') == person.name:
                        identity = old_params
                        break
            result = invoke("treg.people.email.find", identity, ceiling=self.client.email_cap)
            route = result.get("_treg", {})
            if route.get("outcome") == "error" or (not result.get("output") and any(x.get("outcome") == "error" for x in route.get("tried", []))):
                deferred.append("email_router_provider_error")
            output = result.get("output") or {}
            if isinstance(output, str):
                output = {"email": output}
            email = str(output.get("email") or "").strip().lower()
            raw = result.get("raw") or {}
            raw = raw.get("data", raw) if isinstance(raw, dict) else {}
            raw_status = str((raw.get("verification") or {}).get("status", "")).lower() if isinstance(raw, dict) else ""
            if raw_status in {"invalid", "undeliverable"}:
                return None
            if not email or email in self.excluded:
                return None
            verification = self.verifier.verify(email=email, linkedin=linkedin, organization_domain=domain)
            if verification.status == VerificationStatus.REJECTED:
                return None
            if identity_context and identity_context.get("accepted"):
                person = person.model_copy(update={"evidence": [*person.evidence, Evidence(
                    url=identity_context["official_source_url"], supports=identity_context["relationship"] + ": " + identity_context["evidence_quote"], provider="treg:exa")]})
            return {"status": "found", "person": person.model_dump(mode="json"),
                    "email": verification.email, "linkedin": linkedin, "domain": domain,
                    "provider": "treg:" + str(result.get("_treg", {}).get("served_by", "router")),
                    "reason": verification.reason, "identity_context": identity_context,
                    "quality_signals": list(assess_recipient(
                        verification_status=verification.status.value,
                        verification_reason=verification.reason,
                    ).quality_signals)}

        def prefer_or_remember(found):
            if not found:
                return None
            if current_employer_match(found["email"], domain) is False:
                found["quality_signals"] = list(dict.fromkeys([
                    *(found.get("quality_signals") or []),
                    "organization_domain_mismatch",
                ]))
                provider_fallbacks.append(found)
                return None
            return found

        candidates.sort(key=lambda x: -score_recipient_role(x["person"].title, x["person"].scope)[0])
        for candidate in candidates[:12]:
            if found := prefer_or_remember(find_email(candidate)):
                return found

        # A sourced Grok person+LinkedIn can be revealed without a company
        # domain. A newly searched person sharing only an employer name cannot:
        # property names and unrelated same-name companies routinely collide.
        if not domain:
            if existing_fallbacks:
                fallback = existing_fallbacks[0]
                fallback["quality_signals"] = list(dict.fromkeys([
                    *(fallback.get("quality_signals") or []),
                    "fallback_after_no_better_match",
                ]))
                fallback["deferred_better_match_searches"] = deferred
                return fallback
            return {"status": "deferred" if deferred else "needs_identity", "domain": "", "errors": deferred or ["company_domain_unresolved"], "identity_context": identity_context}

        def people_rows(query, total):
            size = min(12, int(total))
            token, seen_tokens = None, set()
            for _ in range(3):
                pagination = {'size':size}
                if token:
                    pagination['token'] = token
                result = invoke('icypeas.people.search', {'query':query, 'pagination':pagination}, ceiling=0.00038 * size)
                yield from sorted(result.get('leads', []), key=lambda r: (
                    -score_recipient_role(r.get('lastJobTitle') or '', r.get('address') or '')[0],
                    -int(any(x in (r.get('address') or '').lower() for x in ('arizona', 'phoenix', 'tucson')))))
                token = (result.get('pagination') or {}).get('token')
                if not token or token in seen_tokens:
                    break
                seen_tokens.add(token)

        base = {"currentCompanyWebsite": {"include": [domain]}}
        for role_filter in (True, False):
            query = dict(base)
            if role_filter:
                query["currentJobTitle"] = {"include": ROLE_TERMS}
            count = invoke("icypeas.people.search.count", {"query": query})
            if not count or not count.get("success") or not isinstance(count.get("total"), (int, float)):
                if count:
                    deferred.append("people_count_invalid_response")
                continue
            if count["total"] == 0:
                continue
            for row in people_rows(query, count['total']):
                match_domain = domain_host(row.get("lastCompanyWebsite") or "")
                names = [organization.canonical_name, *organization.aliases]
                name_match = any(same_company(row.get("lastCompanyName", ""), n) for n in names)
                if domain and not domain_matches(match_domain, domain):
                    continue
                if not domain and not name_match:
                    continue
                name = " ".join(str(row.get(k) or "").strip() for k in ("firstname", "lastname")).strip()
                linkedin = row.get("profileUrl") or ""
                if not name or not linkedin:
                    continue
                person = Person(person_id=person_id(name, organization.organization_id), organization_id=organization.organization_id,
                    name=name, title=row.get("lastJobTitle") or row.get("headline") or "", scope=row.get("address") or "",
                    evidence=[Evidence(url=linkedin, supports="Treg Icypeas person profile and employer match", provider="treg:icypeas")])
                if found := prefer_or_remember(find_email({"person": person, "linkedin": linkedin})):
                    return found
        # Independent company-domain coverage when Icypeas has no usable
        # person. Paid email reveal still uses the same budgeted router.
        if domain:
            for role_filter in (True, False):
                query = {"companyDomains": {"include": [domain]}, "limit": 12}
                if role_filter:
                    query["leadJobTitles"] = {"include": ROLE_TERMS}
                result = invoke("leadsforge.people.search", query)
                for row in result.get("leads", []):
                    company = row.get("company") or {}
                    if not domain_matches(company.get("domain") or company.get("website") or "", domain):
                        continue
                    name = " ".join(str(row.get(k) or "").strip() for k in ("firstName", "lastName")).strip()
                    if not row.get("firstName") or not row.get("lastName"):
                        continue
                    person = Person(person_id=person_id(name, organization.organization_id),
                        organization_id=organization.organization_id, name=name, title=row.get("jobTitle") or "",
                        evidence=[Evidence(url="https://treg.to/people-search", supports="LeadsForge named person and exact employer domain; raw response in Treg cache", provider="treg:leadsforge")])
                    if found := prefer_or_remember(find_email({"person": person, "linkedin": ""})):
                        return found
        # Explicitly reviewed public company inboxes are a last resort. Their
        # display label is an organization mailbox, never a fabricated person.
        for inbox in (identity_context or {}).get('inboxes', []):
            email = inbox['email'].strip().lower()
            if email in self.excluded:
                continue
            verification = self.verifier.verify(email=email, organization_domain=domain)
            if verification.status == VerificationStatus.REJECTED:
                continue
            person = Person(person_id=stable_uuid('company-mailbox', organization.organization_id, email),
                organization_id=organization.organization_id, name=organization.canonical_name + ' team (shared mailbox)',
                title='Company contact mailbox', scope='company_mailbox: ' + inbox['scope'],
                evidence=[Evidence(url=inbox['source_url'], supports=inbox['source_quote'], provider='treg:cached-official-source')])
            return {'status':'found', 'person':person.model_dump(mode='json'), 'email':email, 'linkedin':'',
                'domain':domain, 'provider':'treg:official-company-mailbox', 'reason':verification.reason,
                'identity_context':identity_context, 'deferred_named_searches':deferred,
                'quality_signals':['shared_company_mailbox_fallback']}
        fallbacks = [*provider_fallbacks, *existing_fallbacks]
        if fallbacks:
            fallbacks.sort(key=lambda value: -score_recipient_role(
                value.get("person", {}).get("title", ""),
                value.get("person", {}).get("scope", ""),
            )[0])
            fallback = fallbacks[0]
            fallback["quality_signals"] = list(dict.fromkeys([
                *(fallback.get("quality_signals") or []),
                "fallback_after_no_better_match",
            ]))
            fallback["deferred_better_match_searches"] = deferred
            return fallback
        return {"status": "deferred" if deferred else "no_match", "domain": domain, "errors": deferred, "identity_context": identity_context}


def research_organization_identity(client, organization, opportunities, *, domain_hints=()):
    """Sourced owner/operator-domain recovery; never trust an uncited model domain."""
    fields = ["company_name", "domain", "relationship", "confidence", "official_source_url", "article_source_url", "evidence_quote", "explanation"]
    schema = {"type": "object", "properties": {k: {"type": "string"} for k in fields}, "required": fields, "additionalProperties": False}
    context = [x.model_dump(mode="json") if hasattr(x, "model_dump") else x for x in opportunities]
    query = ("Identify the exact organization responsible for this Arizona commercial-property opportunity. "
        "Resolve the NAMED TARGET itself first. Do not replace a tenant, retailer or operating company with its landlord or master developer. "
        "If the target is ONLY a property or project, identify its owner/operator and explain the relationship; it will be retained as a separate route for review. "
        "Return their CORPORATE email domain, NOT a project marketing website, contractor, broker or similarly named unrelated company. "
        "Read the original article and an official corporate page confirming the company/project relationship. "
        "Cite the actual official page and original article. evidence_quote must be an exact verbatim excerpt of at least 25 characters from the cited official page. "
        "confidence must be high, medium or low. Return empty domain and low confidence if unconfirmed. "
        "Unverified domain hints (not evidence): " + json.dumps(list(domain_hints)) + ". "
        "Target: " + organization.canonical_name + "; context: " + json.dumps(context[:3], sort_keys=True))
    prior = []
    if hasattr(client, 'cached_calls'):
        for params, response in client.cached_calls('exa.web.answer'):
            if 'Target: ' + organization.canonical_name + ';' not in params.get('query', ''):
                continue
            checked = validate_identity_response(response, organization)
            if checked['accepted']:
                return checked
            prior.append(checked)
    try:
        response = client.call("exa.web.answer", {"query": query, "text": True, "outputSchema": schema}, ceiling=0.005)
    except TregDeferred:
        if getattr(client, 'cache_only', False) and prior:
            return {**prior[-1], 'next_step':'official_identity_evidence_followup'}
        raise
    return validate_identity_response(response, organization)


def validate_identity_response(response, organization):
    answer = response.get("answer") or {}
    if isinstance(answer, str):
        try:
            answer = json.loads(answer)
        except ValueError:
            answer = {}
    if not isinstance(answer, dict):
        answer = {}
    domain = domain_host(answer.get("domain") or "")
    official = answer.get("official_source_url") or ""
    quote = answer.get("evidence_quote") or ""
    def norm(text):
        return " ".join(str(text).casefold().split())
    citations = response.get("citations") or []
    official_text = " ".join(x.get("text") or "" for x in citations if (x.get("url") or "").rstrip("/") == official.rstrip("/"))
    article_text = " ".join(x.get("text") or "" for x in citations if (x.get("url") or "").rstrip("/") == (answer.get("article_source_url") or "").rstrip("/"))
    name = answer.get("company_name") or ""
    tied = norm(organization.canonical_name) in norm(official_text) or (norm(organization.canonical_name) in norm(article_text) and bool(name) and norm(name) in norm(article_text))
    original_target = any(same_company(name, n) for n in [organization.canonical_name, *organization.aliases])
    accepted = bool(original_target and domain and name and answer.get("confidence") == "high" and domain_matches(official, domain)
        and len(quote.strip()) >= 25 and norm(quote) in norm(official_text) and tied)
    inboxes = official_inboxes(official_text, official) if accepted else []
    return {**answer, "domain": domain, "accepted": accepted,
            "inboxes": inboxes,
            "related_route": answer if not original_target else None,
            "validation": "quoted_official_source_and_opportunity_link" if accepted else "official_domain_or_relationship_not_confirmed"}


def recover_state(state, artifacts, organizations, people, events, contacts, *, client):
    """Shared daily/bulk hook, after Grok and before optional Apollo."""
    from .verification import ContactVerifier
    resolver = TregResolver(client, ContactVerifier(state))
    results = []
    for organization in organizations:
        result = resolver.resolve(organization, [p for p in people if p.organization_id == organization.organization_id],
                                  [c for c in contacts if c.organization_id == organization.organization_id],
                                  opportunities=[e for e in events if e.organization_id == organization.organization_id])
        results.append({"organization_id": organization.organization_id, **result})
        if result["status"] != "found":
            continue
        person = Person.model_validate(result["person"])
        state.save_person(person)
        people.append(person)
        if result.get("domain") and not organization.domain:
            state.save_organization(organization.model_copy(update={"domain": result["domain"]}))
        for event in events:
            if event.organization_id != organization.organization_id:
                continue
            contact = ContactCandidate(contact_candidate_id=stable_uuid("treg-contact", event.lead_event_id, person.person_id, result["email"]),
                run_id=artifacts.run_id, lead_event_id=event.lead_event_id, organization_id=organization.organization_id,
                person_id=person.person_id, person_name=person.name, title=person.title,
                email=result["email"], linkedin=result["linkedin"], provider=result["provider"],
                verification_status=VerificationStatus.UNKNOWN, verification_reason=result["reason"],
                evidence=person.evidence + [Evidence(url="https://treg.to/people-search", supports="Email returned by authenticated Treg provider; exact response in recovery cache", provider=result["provider"])])
            contacts.append(contact)
        # A replacement provider has repaired these exact research contracts;
        # keep qualification, event, identity and other reviews untouched.
        resolved = []
        for review in state.reviews_for_run(artifacts.run_id):
            repaired = (review.stage == "decision-makers" and review.record_id == organization.organization_id) or (review.stage == "contacts" and review.record_id == person.person_id)
            if repaired and review.state == "open" and review.reason_code == "model_contract_invalid":
                state.resolve_review(review.review_id)
                resolved.append(review.review_id)
        results[-1]["resolved_research_reviews"] = resolved
    contacts = select_best(contacts)
    for contact in contacts:
        state.save_contact(contact)
    artifact = artifacts.write_json("contacts", "treg-recovery.json", {"companies": results, "accounting": client.stats()})
    for result in results:
        if result["status"] == "deferred":
            state.add_review(ReviewItem(
                review_id=stable_uuid("review", artifacts.run_id, "treg-deferred", result["organization_id"]),
                run_id=artifacts.run_id, stage="contacts", record_type="organization",
                record_id=result["organization_id"], reason_code="treg_enrichment_deferred",
                validation_errors=result.get("errors") or ["provider_deferred"],
                raw_artifact_path=(artifact or {}).get("path", ""),
            ))
        elif result["status"] in {"found", "existing_email"}:
            for review in state.reviews_for_run(artifacts.run_id):
                if (review.reason_code == "treg_enrichment_deferred"
                        and review.record_id == result["organization_id"] and review.state == "open"):
                    state.resolve_review(review.review_id)
    return list({p.person_id: p for p in people}.values()), contacts
