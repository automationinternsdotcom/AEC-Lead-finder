import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scout'))

from scout.v2.contracts import Organization, Person, VerificationStatus
from scout.v2.treg import TregClient, TregDeferred, TregResolver


def test_local_export_needs_no_treg_authorization_or_provider(monkeypatch, tmp_path, capsys):
    scripts = Path(__file__).resolve().parents[1] / 'skills/aether-bulk-enrichment/scripts'
    monkeypatch.syspath_prepend(str(scripts))
    import bulk_enrich
    import integration.treg_recovery as recovery
    import integration.handoff as handoff
    sales_db = tmp_path / 'sales.sqlite'
    (tmp_path / 'inventory.json').write_text(json.dumps({'sales_db': str(sales_db.resolve())}))
    (tmp_path / 'companies').mkdir()
    calls = []
    monkeypatch.setattr(recovery, 'build_handoff', lambda plan, results, output: calls.append((plan, results, output)) or output / 'sales_handoff.json')
    monkeypatch.setattr(handoff, 'load_handoff', lambda path: SimpleNamespace(recipients=[]))
    monkeypatch.setattr(recovery, 'recover', lambda *a, **k: pytest.fail('local export called provider recovery'))
    assert bulk_enrich.main(['--since', '2026-01-01', '--until', '2026-09-05', '--output', str(tmp_path), '--recover-sales-with-treg', str(sales_db), '--export-treg-recovery']) == 0
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out)['paid_provider_calls'] == 0


def test_paid_request_cache_and_cumulative_budget_survive_restart(tmp_path):
    paid = []
    def handler(request):
        if request.url.path == '/mcp/':
            return httpx.Response(200, json={'result': {'structuredContent': {'balance_micro': 10_000_000}}})
        if request.url.path.startswith('/catalog/'):
            return httpx.Response(200, json={'cost': {'usd': .0089, 'unit': 'success'}})
        paid.append(request)
        return httpx.Response(200, headers={'x-treg-cost-micro': '8900'}, json={'output': {'email': 'person@example.com'}})
    path = tmp_path / 'cache.sqlite'
    for _ in range(2):
        client = TregClient(path, token='test', budget_usd=.025, transport=httpx.MockTransport(handler))
        result = client.call('treg.people.email.find', {'full_name': 'One Person', 'domain': 'example.com'}, ceiling=.025)
        assert result['output']['email'] == 'person@example.com'
        client.close()
    assert len(paid) == 1
    client = TregClient(path, token='test', budget_usd=.025, transport=httpx.MockTransport(handler))
    with pytest.raises(TregDeferred, match='budget'):
        client.call('treg.people.email.find', {'full_name': 'Another Person', 'domain': 'example.com'}, ceiling=.025)
    assert len(paid) == 1
    client.close()


@pytest.mark.parametrize('value', ['""},evidence_quote:{citations:[1],confidence:', 'not a domain', 'https://[broken', 'javascript:bad', 'https://user:pass@example.com'])
def test_malformed_domain_fails_closed_without_crashing(value):
    from scout.v2.treg import domain_host
    assert domain_host(value) == ''


def test_cache_only_miss_makes_no_network_calls(tmp_path):
    client = TregClient(tmp_path/'cache.sqlite', cache_only=True,
        transport=httpx.MockTransport(lambda request: pytest.fail('cache-only made a network request')))
    with pytest.raises(TregDeferred, match='cache_only_miss'):
        client.call('exa.web.answer', {'query':'Acme'}, ceiling=.005)
    assert client.stats()['calls'] == 0
    client.close()


def test_unconfirmed_provider_domain_cannot_drive_opportunity_email_search():
    client = FakeClient({'hunter.x.domain-finder':{'data':[{'company_name':'Chase Bank','domain':'cambodian.com'}]},
        'companyenrich.companies.autocomplete':[], 'exa.web.answer':{'answer':{},'citations':[]}})
    result = TregResolver(client, Verifier()).resolve(Organization(organization_id='o', canonical_name='Chase Bank'), opportunities=[{}])
    assert result['status'] == 'needs_identity'
    assert not any(c[0] == 'treg.people.email.find' for c in client.calls)


def test_reviewed_shared_mailbox_fallback_is_labeled_and_suppression_safe():
    client = FakeClient({'icypeas.people.search.count':{'total':0,'success':True}, 'leadsforge.people.search':{'leads':[]}})
    identity = {'accepted':True,'domain':'acme.example','inboxes':[{'email':'info@acme.example',
        'source_url':'https://acme.example/contact','source_quote':'For general questions contact info@acme.example.', 'scope':'General inquiries'}]}
    org = Organization(organization_id='o', canonical_name='Acme')
    result = TregResolver(client, Verifier()).resolve(org, opportunities=[{}], reviewed_identity=identity)
    assert result['status'] == 'found'
    assert result['person']['scope'].startswith('company_mailbox:')
    assert result['person']['name'] == 'Acme team (shared mailbox)'
    result = TregResolver(client, Verifier(), excluded_emails=['info@acme.example']).resolve(org, opportunities=[{}], reviewed_identity=identity)
    assert result['status'] == 'no_match'


def test_uncertain_retry_reuses_idempotency_key(tmp_path):
    keys = []
    def handler(request):
        if request.url.path == '/mcp/':
            return httpx.Response(200, json={'result': {'structuredContent': {'balance_micro': 10_000_000}}})
        if request.url.path.startswith('/catalog/'):
            return httpx.Response(200, json={'cost': {'usd': .0089, 'unit': 'success'}})
        keys.append(request.headers['Idempotency-Key'])
        if len(keys) == 1:
            raise httpx.ReadTimeout('interrupted')
        return httpx.Response(200, json={'output': {}})
    client = TregClient(tmp_path/'cache.sqlite', token='test', transport=httpx.MockTransport(handler))
    with pytest.raises(TregDeferred):
        client.call('treg.people.email.find', {'full_name':'A B','domain':'example.com'}, ceiling=.025)
    client.call('treg.people.email.find', {'full_name':'A B','domain':'example.com'}, ceiling=.025)
    assert keys[0] == keys[1]
    client.close()


class FakeClient:
    email_cap = .025
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
    def call(self, endpoint, params, **kwargs):
        self.calls.append((endpoint, params))
        result = self.responses[endpoint]
        if isinstance(result, Exception):
            raise result
        return result


class Verifier:
    def verify(self, email, **kwargs):
        return SimpleNamespace(email=email, status=VerificationStatus.UNKNOWN, reason='email_domain_organization_mismatch_mx_valid')


def test_search_without_grok_person_allows_low_role_former_employer_email():
    client = FakeClient({
        'hunter.x.domain-finder': {'data': []},
        'companyenrich.companies.autocomplete': [{'name':'Acme','domain':'acme.example'}],
        'icypeas.people.search.count': {'success': True, 'total': 1},
        'icypeas.people.search': {'leads': [{'firstname': 'Jane', 'lastname': 'Doe', 'lastCompanyName': 'Acme', 'lastCompanyWebsite':'acme.example', 'lastJobTitle': 'Assistant', 'profileUrl': 'https://linkedin.com/in/jane'}]},
        'leadsforge.people.search': {'leads': []},
        'treg.people.email.find': {'output': {'email': 'jane@former-employer.com'}, '_treg': {'served_by': 'tomba'}}})
    result = TregResolver(client, Verifier()).resolve(Organization(organization_id='org', canonical_name='Acme'))
    assert result['status'] == 'found'
    assert result['email'] == 'jane@former-employer.com'
    assert result['person']['title'] == 'Assistant'


def test_wrong_company_not_accepted_for_no_domain():
    client = FakeClient({
        'hunter.x.domain-finder': {'data': [{'company_name': 'Other Company', 'domain': 'other.example'}]},
        'companyenrich.companies.autocomplete': [],
        'icypeas.people.search.count': {'success': True, 'total': 1},
        'icypeas.people.search': {'leads': [{'firstname': 'Jane', 'lastname': 'Doe', 'lastCompanyName': 'Other Company', 'profileUrl': 'https://linkedin.com/in/jane'}]}})
    result = TregResolver(client, Verifier()).resolve(Organization(organization_id='org', canonical_name='Acme'))
    assert result['status'] == 'needs_identity'
    assert not any(x[0] == 'treg.people.email.find' for x in client.calls)


def test_provider_failure_is_deferred_not_no_match():
    client = FakeClient({'icypeas.people.search.count': TregDeferred('HTTP 503'), 'leadsforge.people.search': {'leads': []}})
    result = TregResolver(client, Verifier()).resolve(Organization(organization_id='org', canonical_name='Acme', domain='acme.example'))
    assert result['status'] == 'deferred'


def test_existing_email_avoids_all_treg_calls():
    client = FakeClient({})
    person = Person(person_id='p', organization_id='org', name='Jane Doe')
    contact = SimpleNamespace(email='jane@example.com', verification_status=VerificationStatus.UNKNOWN,
        person_id='p', linkedin='', provider='model', verification_reason='domain_mx_valid_mailbox_unverified')
    result = TregResolver(client, Verifier()).resolve(Organization(organization_id='org',canonical_name='Acme'), [person], [contact])
    assert result['status'] == 'existing_email'
    assert client.calls == []


def test_existing_former_employer_email_triggers_better_match_search():
    client = FakeClient({
        'icypeas.people.search.count': {'success': True, 'total': 0},
        'leadsforge.people.search': {'leads': [{
            'firstName': 'Current', 'lastName': 'Manager', 'jobTitle': 'Operations Manager',
            'company': {'domain': 'acme.example'},
        }]},
        'treg.people.email.find': {'output': {'email': 'current@acme.example'}},
    })
    person = Person(person_id='former', organization_id='org', name='Former Person', title='Director')
    contact = SimpleNamespace(
        email='former@old.example', verification_status=VerificationStatus.UNKNOWN,
        person_id='former', linkedin='', provider='model',
        verification_reason='email_domain_organization_mismatch_mx_valid',
    )
    result = TregResolver(client, Verifier()).resolve(
        Organization(organization_id='org', canonical_name='Acme', domain='acme.example'),
        [person], [contact],
    )
    assert result['status'] == 'found'
    assert result['email'] == 'current@acme.example'
    assert any(call[0] == 'treg.people.email.find' for call in client.calls)


def test_existing_former_employer_email_survives_when_no_better_match_exists():
    client = FakeClient({
        'icypeas.people.search.count': {'success': True, 'total': 0},
        'leadsforge.people.search': {'leads': []},
        'treg.people.email.find': {'output': {}},
    })
    person = Person(person_id='former', organization_id='org', name='Former Person', title='Director')
    contact = SimpleNamespace(
        email='former@old.example', verification_status=VerificationStatus.UNKNOWN,
        person_id='former', linkedin='https://linkedin.com/in/former', provider='model',
        verification_reason='email_domain_organization_mismatch_mx_valid',
    )
    result = TregResolver(client, Verifier()).resolve(
        Organization(organization_id='org', canonical_name='Acme', domain='acme.example'),
        [person], [contact],
    )
    assert result['status'] == 'existing_email'
    assert result['email'] == 'former@old.example'
    assert 'fallback_after_no_better_match' in result['quality_signals']
    assert 'organization_domain_mismatch' in result['quality_signals']


def test_secondary_people_provider_recovers_no_icy_match():
    client = FakeClient({
        'icypeas.people.search.count': {'success': True, 'total': 0},
        'leadsforge.people.search': {'leads': [{'firstName': 'Jane', 'lastName': 'Doe', 'jobTitle': 'Owner', 'company': {'domain': 'acme.example'}}]},
        'treg.people.email.find': {'output': {'email': 'jane@acme.example'}}})
    result = TregResolver(client, Verifier()).resolve(Organization(organization_id='org', canonical_name='Acme', domain='acme.example'))
    assert result['status'] == 'found'
    assert result['person']['name'] == 'Jane Doe'


def test_free_autocomplete_recovers_only_unambiguous_exact_company():
    responses = {
        'hunter.x.domain-finder': {'data': []},
        'companyenrich.companies.autocomplete': [{'name':'Acme','domain':'acme.example'}, {'name':'Different Acme','domain':'other.example'}],
        'icypeas.people.search.count': {'success': True, 'total': 0},
        'leadsforge.people.search': {'leads': []}}
    result = TregResolver(FakeClient(responses), Verifier()).resolve(Organization(organization_id='org', canonical_name='Acme'))
    assert result['domain'] == 'acme.example'
    responses['companyenrich.companies.autocomplete'].append({'name':'Acme','domain':'ambiguous.example'})
    result = TregResolver(FakeClient(responses), Verifier()).resolve(Organization(organization_id='org', canonical_name='Acme'))
    assert result['domain'] == ''


def test_identity_requires_actual_official_citation_not_just_model_claim():
    from scout.v2.treg import research_organization_identity
    quote = 'Sunflower is redeveloping the Security Building in downtown Phoenix.'
    answer = {'company_name':'Sunflower','domain':'sunflower.example','relationship':'Owner-developer', 'confidence':'high',
        'official_source_url':'https://sunflower.example/security','article_source_url':'https://news.example/article','evidence_quote':quote,'explanation':'Documented redevelopment'}
    responses = {'exa.web.answer':{'answer':answer,'citations':[{'url':'https://sunflower.example/security','text':quote}]}}
    org=Organization(organization_id='org',canonical_name='Security Building')
    result=research_organization_identity(FakeClient(responses),org,[{'article_url':'https://news.example/article'}])
    assert not result['accepted']
    assert result['related_route']['company_name'] == 'Sunflower'
    org = Organization(organization_id='org', canonical_name='Sunflower')
    assert research_organization_identity(FakeClient(responses),org,[{}])['accepted']
    responses['exa.web.answer']['citations']=[]
    assert not research_organization_identity(FakeClient(responses),org,[{}])['accepted']


def test_completed_requests_refresh_without_erasing_spend(tmp_path, monkeypatch):
    paid = []
    def handler(request):
        if request.url.path == '/mcp/':
            return httpx.Response(200, json={'result': {'structuredContent': {'balance_micro': 10_000_000}}})
        if request.url.path.startswith('/catalog/'):
            return httpx.Response(200, json={'cost': {'usd': .0089, 'unit': 'success'}})
        paid.append(request.headers['Idempotency-Key'])
        return httpx.Response(200, headers={'x-treg-cost-micro': '8900'}, json={'output': {}})
    client = TregClient(tmp_path/'cache.sqlite', token='test', transport=httpx.MockTransport(handler))
    args = ('treg.people.email.find', {'full_name': 'A B', 'domain': 'example.com'})
    client.call(*args, ceiling=.025)
    with client.connection() as db:
        db.execute('UPDATE calls SET updated=updated-31*86400')
    client.call(*args, ceiling=.025)
    client.call(*args, ceiling=.025)
    assert len(set(paid)) == 2
    assert client.stats()['charged_upper_bound_micro'] == 17800
    client.close()


def test_state_recovery_without_grok_people_repairs_only_research_review(monkeypatch):
    from scout.v2.treg import recover_state
    from scout.v2 import verification
    saved_people, saved_contacts, resolved, artifacts = [], [], [], []
    reviews = [SimpleNamespace(review_id='research', stage='decision-makers', record_id='org', state='open', reason_code='model_contract_invalid'),
               SimpleNamespace(review_id='event-review', stage='qualification', record_id='event', state='open', reason_code='model_contract_invalid')]
    state = SimpleNamespace(save_person=saved_people.append, save_contact=saved_contacts.append,
        reviews_for_run=lambda run: reviews, resolve_review=resolved.append)
    sink = SimpleNamespace(run_id='run', write_json=lambda *args: artifacts.append(args))
    monkeypatch.setattr(verification, 'ContactVerifier', lambda state: Verifier())
    client = FakeClient({
        'icypeas.people.search.count': {'success': True, 'total': 0},
        'leadsforge.people.search': {'leads': [{'firstName': 'Jane', 'lastName': 'Doe', 'company': {'domain': 'acme.example'}}]},
        'treg.people.email.find': {'output': {'email': 'jane@acme.example'}}})
    client.stats = lambda: {}
    monkeypatch.setattr(sys.modules[TregResolver.__module__], 'research_organization_identity',
        lambda *a, **k: {'accepted':True, 'domain':'acme.example', 'official_source_url':'https://acme.example',
                        'relationship':'same company', 'evidence_quote':'Acme operates the Phoenix facility.'})
    client.responses.update({'hunter.x.domain-finder':{'data':[]}, 'companyenrich.companies.autocomplete':[]})
    people, contacts = recover_state(state, sink,
        [Organization(organization_id='org', canonical_name='Acme', domain='acme.example')], [],
        [SimpleNamespace(organization_id='org', lead_event_id='event')], [], client=client)
    assert len(people) == len(contacts) == 1
    assert contacts[0].selected and contacts[0].email == 'jane@acme.example'
    assert resolved == ['research']
    assert saved_people and saved_contacts and artifacts


def test_deferred_treg_does_not_abort_other_ingestion(monkeypatch):
    from scout.v2 import treg, verification
    reviews = []
    state = SimpleNamespace(save_contact=lambda c: None, add_review=reviews.append,
                            reviews_for_run=lambda run: [])
    sink = SimpleNamespace(run_id="daily-run", write_json=lambda *args: {"path": "recovery.json"})
    monkeypatch.setattr(verification, "ContactVerifier", lambda state: None)
    monkeypatch.setattr(treg, "TregResolver", lambda *args: SimpleNamespace(
        resolve=lambda *args, **kwargs: {"status": "deferred", "errors": ["Treg balance floor reached"]}))
    people, contacts = treg.recover_state(state, sink,
        [Organization(organization_id="org", canonical_name="Acme")], [], [], [],
        client=SimpleNamespace(stats=lambda: {}))
    assert people == contacts == []
    assert reviews[0].reason_code == "treg_enrichment_deferred"
    assert reviews[0].record_id == "org"


def test_handoff_alternate_email_keeps_invalid_identity_and_replaces_only_draft(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/aether-bulk-enrichment/scripts'))
    from integration.database import Database
    from integration.handoff import load_handoff
    from integration.models import CompanySync, LeadEventSync, RecipientSync, OutreachSequenceSync
    from integration.treg_recovery import build_handoff
    db_path = tmp_path/'sales.sqlite'
    db = Database(str(db_path))
    company = CompanySync(company_id='company', canonical_name='Acme', domain='acme.example')
    event = LeadEventSync(run_id='run', lead_event_id='event', company_id='company', organization_name='Acme',
        event_role='anchor', event='Opened a property', location='Phoenix', date_posted='2026-09-01', article_url='https://example.com/story',
        score=90, confidence='high', record_status='valid', actionable_route=True, crm_eligible=True)
    recipient = RecipientSync(recipient_id='old-recipient', company_id='company', person_id='person', contact_candidate_id='contact',
        full_name='Jane Doe', first_name='Jane', email='old@acme.example', source_provider='model', source_verification_status='unknown', role_score=90, rank=1, primary=True)
    db.upsert_company(company, source='test')
    db.upsert_lead_event(event)
    db.upsert_recipient(recipient)
    db.update_recipient('old-recipient', verification_status='invalid')
    why = 'Hi [first name] — Saw Acme opened in Phoenix.'
    sequence = OutreachSequenceSync(sequence_id='sequence', run_id='run', company_id='company', campaign_protocol='aether-sales-handoff-v1',
        anchor_lead_event_id='event', primary_recipient_id='old-recipient', why_template_key='opening', why_slots={'property':'Acme','location':'Phoenix'},
        why_sources=['https://example.com/story'], why_confidence='high', company_why_line=why, personalized_why_line=why,
        merge_snapshot={}, merge_hash='old', eligibility_status='blocked', eligibility_reasons=['warmy_verification_invalid'])
    db.save_sequence(sequence)
    profile = {'company_id':'company','run_id':'run','canonical_name':'Acme','anchor_lead_event_id':'event',
        'why_line':why,'why_template_key':'opening','why_slots':{'property':'Acme','location':'Phoenix'},'why_sources':['https://example.com/story'],
        'why_confidence':'high','why_line_status':'valid'}
    plan = {'sales_db':str(db_path), 'targets':[{'company':company.model_dump(mode='json'),'events':[event.model_dump(mode='json')],'profiles':[profile],'reviews':[]}]}
    result = {'company_id':'company','company':'Acme','status':'found','person':{'person_id':'person','organization_id':'company','name':'Jane Doe'},
        'email':'new@acme.example','provider':'treg:test','reason':'mailbox_unverified'}
    handoff = load_handoff(build_handoff(plan, [result], tmp_path/'output'))
    assert handoff.recipients[0].recipient_id == 'old-recipient'
    assert handoff.sequences[0].sequence_id == 'sequence'
    assert handoff.sequences[0].eligibility_status == 'ready'
    assert db.get_recipient(recipient_id='old-recipient')['verification_status'] == 'invalid'
    db.upsert_recipient(handoff.recipients[0])
    assert db.get_recipient(recipient_id='old-recipient')['verification_status'] == 'pending'
    with db.connection() as conn:
        history = conn.execute('SELECT snapshot FROM recipient_address_history').fetchone()
    assert json.loads(history[0])['verification_status'] == 'invalid'
    assert json.loads(history[0])['normalized_email'] == 'old@acme.example'
    assert load_handoff(build_handoff(plan, [result], tmp_path/'output')).sequences[0].eligibility_status == 'ready'
    # Once the primary is chosen, another company's duplicate recipient must
    # not make BOTH companies blocked on the next resumable projection.
    db.save_sequence(handoff.sequences[0])
    db.upsert_company(company.model_copy(update={'company_id':'other','canonical_name':'Other Company','domain':'other.example'}),source='test')
    db.upsert_recipient(handoff.recipients[0].model_copy(update={'company_id':'other','recipient_id':'other-person','person_id':'other-person'}))
    assert load_handoff(build_handoff(plan,[result],tmp_path/'output')).sequences[0].eligibility_status == 'ready'
    with db.connection() as conn:
        conn.execute("UPDATE outreach_sequences SET approval_state='approved' WHERE sequence_id='sequence'")
    preserved = load_handoff(build_handoff(plan, [result], tmp_path/'output2'))
    assert preserved.sequences == []


def test_provider_rate_slots_are_shared_between_workers(tmp_path):
    from integration.database import Database
    a=Database(tmp_path/'db.sqlite');b=Database(tmp_path/'db.sqlite')
    assert a.reserve_provider_slot('warmy.verification',8) == 0
    assert 7 < b.reserve_provider_slot('warmy.verification',8) <= 8
    assert 15 < a.reserve_provider_slot('warmy.verification',8) <= 16


def test_rate_limit_does_not_consume_worker_retry_budget(tmp_path):
    from integration.database import Database
    from integration.config import Settings
    from integration.providers import ProviderError
    from integration.worker import run_once
    db=Database(tmp_path/'db.sqlite')
    db.enqueue_work('scout.sequence.sync','limited',{})
    class Limited:
        def handle(self,item): raise ProviderError('warmy',429,'verification_rate_limited','wait')
        def close(self): pass
    assert run_once(Settings(worker_batch_size=1),db=db,workflows=Limited()) == 0
    with db.connection() as conn:
        row=conn.execute("SELECT status,attempt_count FROM work_items WHERE dedupe_key='limited'").fetchone()
    assert tuple(row) == ('pending',0)


def test_reviewed_import_requires_cached_proof_and_uses_no_paid_calls(tmp_path,monkeypatch):
    import sqlite3
    from integration.database import Database
    from integration import treg_recovery as recovery
    dbpath=tmp_path/'sales.sqlite';Database(dbpath).healthcheck()
    out=tmp_path/'out';out.mkdir()
    (out/'inventory.json').write_text(json.dumps({'sales_db':str(dbpath),'targets':[{'company':{'company_id':'c','canonical_name':'Acme'},'events':[]}]}))
    quote='Jane Doe, Operations Manager, jane@acme.example'
    with sqlite3.connect(out/'treg-cache.sqlite') as c:
        c.execute('CREATE TABLE calls(endpoint,status,response)')
        c.execute('INSERT INTO calls VALUES (?,?,?)',('exa.web.answer','done',json.dumps({'citations':[{'url':'https://acme.example/team','text':quote}]})))
    monkeypatch.setattr(recovery,'ContactVerifier',lambda state:Verifier())
    monkeypatch.setattr(recovery,'build_handoff',lambda plan,results,output:Path(output)/'handoff.json')
    row={'company_id':'c','name':'Jane Doe','title':'Operations Manager','email':'jane@acme.example','domain':'acme.example',
        'source_url':'https://acme.example/team','source_quote':'A fabricated quotation with no matching evidence','relationship_note':'Acme official team page'}
    review=tmp_path/'review.json';review.write_text(json.dumps([row]))
    with pytest.raises(ValueError,match='absent from cached source'):
        recovery.import_reviewed_contacts(dbpath,out,review)
    row['source_quote']=quote;review.write_text(json.dumps([row]))
    assert recovery.import_reviewed_contacts(dbpath,out,review)['paid_provider_calls']==0
    assert json.loads((out/'companies/c.json').read_text())['email']=='jane@acme.example'


def test_archived_pipedrive_lead_is_preserved_not_retried(monkeypatch):
    from integration.workflows import SalesWorkflows
    from integration.providers import ProviderError
    workflow=object.__new__(SalesWorkflows)
    updates=[]
    workflow.db=SimpleNamespace(update_lead_event=lambda event,**fields:updates.append((event,fields)))
    def archived(payload):raise ProviderError('pipedrive',403,'lead is archived','Archived lead cannot be updated.')
    workflow.sync_lead_event=archived
    workflow.handle(SimpleNamespace(kind='scout.lead.sync',payload={'lead_event':{'lead_event_id':'event'}}))
    assert updates == [('event',{'crm_state':'archived'})]
    def unrelated(payload):raise ProviderError('pipedrive',403,'forbidden','Permission denied')
    workflow.sync_lead_event=unrelated
    with pytest.raises(ProviderError):
        workflow.handle(SimpleNamespace(kind='scout.lead.sync',payload={'lead_event':{'lead_event_id':'event'}}))


def test_cache_preview_can_later_apply_without_researching_again(tmp_path, monkeypatch):
    from integration import treg_recovery as recovery
    from integration import handoff
    from integration.database import Database
    dbpath = tmp_path/'sales.sqlite'
    Database(dbpath).healthcheck()
    target = {'company':{'company_id':'c','canonical_name':'Acme','domain':'acme.example'},
              'events':[], 'people':[], 'contacts':[], 'profiles':[], 'reviews':[]}
    (tmp_path/'inventory.json').write_text(json.dumps({'sales_db':str(dbpath),'targets':[target],'excluded_emails':[]}))
    result = {'status':'found','email':'info@acme.example','domain':'acme.example',
              'person':{'person_id':'p'}, 'provider':'test'}
    resolved, imported = [], []
    monkeypatch.setattr(recovery.TregResolver, 'resolve', lambda *a, **k: resolved.append(1) or result.copy())
    monkeypatch.setattr(recovery,'build_handoff', lambda plan,results,output: Path(output)/'handoff.json')
    monkeypatch.setattr(handoff,'enqueue_handoff', lambda db,path: imported.append(path) or {'sequence_jobs':1})
    recovery.recover(tmp_path,dbpath,tmp_path,cache_only=True,workers=1)
    assert len(resolved)==1 and not imported
    recovery.recover(tmp_path,dbpath,tmp_path,cache_only=True,workers=1,apply=True)
    assert len(resolved)==1 and len(imported)==1


def test_exact_company_selector_only_researches_reviewed_batch_and_preserves_cohort(tmp_path, monkeypatch):
    from integration import treg_recovery as recovery
    from integration.database import Database
    dbpath = tmp_path/'sales.sqlite'
    Database(dbpath).healthcheck()
    targets = [
        {'company':{'company_id':cid,'canonical_name':name,'domain':'example.com'},
         'events':[], 'people':[], 'contacts':[], 'profiles':[], 'reviews':[]}
        for cid,name in [('a','Alpha'),('b','Beta')]
    ]
    (tmp_path/'inventory.json').write_text(json.dumps(
        {'sales_db':str(dbpath),'targets':targets,'excluded_emails':[]}))
    (tmp_path/'companies').mkdir()
    (tmp_path/'companies/b.json').write_text(json.dumps(
        {'company_id':'b','company':'Beta','status':'deferred','errors':['saved']}))
    calls=[]
    monkeypatch.setattr(recovery.TregResolver, 'resolve', lambda *a, **k:
        calls.append(a[1].organization_id) or {'status':'no_match','domain':'example.com'})
    monkeypatch.setattr(recovery,'build_handoff', lambda plan,results,output:
        Path(output)/'handoff.json')
    summary=recovery.recover(tmp_path,dbpath,tmp_path,cache_only=True,workers=1,company_ids=['a'])
    assert calls == ['a']
    assert summary['selected_company_ids'] == ['a']
    assert summary['processed'] == 1
    assert summary['cohort_outcomes'] == {'no_match':1,'deferred':1}
    assert {r['company_id'] for r in json.loads((tmp_path/'identity-review-queue.json').read_text())} == {'a','b'}
    with pytest.raises(ValueError,match='mutually exclusive'):
        recovery.recover(tmp_path,dbpath,tmp_path,cache_only=True,limit=1,company_ids=['a'])


def test_company_mailbox_handoff_uses_team_greeting(tmp_path):
    from integration.database import Database
    from integration.treg_recovery import build_handoff
    from integration.handoff import load_handoff
    from integration.models import CompanySync
    dbpath=tmp_path/'sales.sqlite'
    db=Database(dbpath)
    company=CompanySync(company_id='c',canonical_name='Acme',domain='acme.example')
    db.upsert_company(company,source='test')
    plan={'sales_db':str(dbpath),'targets':[{'company':company.model_dump(mode='json'),
          'events':[],'people':[],'profiles':[],'reviews':[]}]}
    result={'company_id':'c','company':'Acme','status':'found','email':'info@acme.example','domain':'acme.example',
            'person':{'person_id':'mailbox','organization_id':'c','name':'Acme team (shared mailbox)',
                      'scope':'company_mailbox: General inquiries'},'provider':'official','reason':'domain_mx_valid_mailbox_unverified'}
    recipient=load_handoff(build_handoff(plan,[result],tmp_path/'out')).recipients[0]
    assert recipient.first_name=='team'
    assert recipient.full_name=='Acme team (shared mailbox)'


def test_identity_exclusion_prevents_enrollment_even_if_sequence_is_ready():
    from integration.workflows import SalesWorkflows
    from integration.config import ActivationBlocked
    workflow=object.__new__(SalesWorkflows)
    workflow.settings=SimpleNamespace(warmy_campaign_id='campaign',warmy_campaign_manifest_hash='manifest')
    workflow.db=SimpleNamespace(
        get_sequence=lambda _: {'company_id':'company','primary_recipient_id':'person','eligibility_status':'ready'},
        valid_approval_for_sequence=lambda *a,**k:True,
        get_recipient=lambda **k:{'normalized_email':'wrong@example.com','verification_status':'valid'},
        is_suppressed=lambda _:False,
        get_state=lambda key:{'reason':'wrong company'} if key=='identity-exclusion:company:wrong@example.com' else None)
    with pytest.raises(ActivationBlocked,match='different company'):
        workflow.enroll_sequence({'sequence_id':'sequence'})


def test_accuracy_review_hold_pauses_only_the_exact_company_email_pair():
    from integration.workflows import SalesWorkflows
    from integration.config import ActivationBlocked
    workflow=object.__new__(SalesWorkflows)
    workflow.settings=SimpleNamespace(warmy_campaign_id='campaign',warmy_campaign_manifest_hash='manifest')
    workflow.db=SimpleNamespace(
        get_sequence=lambda _: {'company_id':'company','primary_recipient_id':'person','eligibility_status':'ready'},
        valid_approval_for_sequence=lambda *a,**k:True,
        get_recipient=lambda **k:{'normalized_email':'review@example.com','verification_status':'valid'},
        is_suppressed=lambda _:False,
        get_state=lambda key:{'reason':'verify employer'} if key=='accuracy-review-hold:company:review@example.com' else None)
    with pytest.raises(ActivationBlocked,match='accuracy review'):
        workflow.enroll_sequence({'sequence_id':'sequence'})
