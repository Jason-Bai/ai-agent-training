from pricing_support import write_pricing
"""B lifecycle: real HTTP routes/owned PostgreSQL, scripted Provider evidence.

These are controlled paid-path fixtures, not observations of an actual supplier.
No Router, fee calculator, lifecycle or validator is replaced.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import threading
import time

from fastapi.testclient import TestClient
import pytest
import yaml

from contracts import Usage
from f12_cases import FixedCounter, request
from f13_cases import registry
from legacy_identity import HEADERS, provision
from main import create_app
from postgres_support import migrate
from providers.base import ProviderResult, ProviderStreamEvent
from services.classification import CLASSIFIER_TEMPLATE
from services.models import ProviderConfig
from services.token_budget import TokenBudget
from settings import Settings
from test_f12_stream_plan import f12_pg
from utils.json_codec import dumps, loads


MODELS = {'light': 'qwen3.8-flash', 'general': 'qwen3.7-plus', 'analysis': 'qwen3.8-max'}
NO_PERMITS = {'active_requests': 0, 'agents': {}}


def definitions():
    base = registry()
    endpoints = {name: value.model_copy(update={
        'provider': 'controlled-main', 'model_id': MODELS[name.removesuffix('-e')],
        'structured_output_mode': 'json_schema', 'min_output_tokens': 16,
        'token_counter': 'qwen_text_estimate_v1'}) for name, value in base.endpoints.items()}
    return replace(base, endpoints=endpoints, providers={'controlled-main': ProviderConfig(
        adapter='bailian', api_protocol='responses', base_url='http://127.0.0.1:9',
        credential_ref='unused-injected-fixture')})


@pytest.fixture
def lifecycle_case(f12_pg, tmp_path):
    name = f12_pg.create_database(); directory = tmp_path/'f13-lifecycle'; directory.mkdir()
    definition = definitions()
    document = {key: {name: value.model_dump(mode='json', exclude_none=True)
                     for name, value in getattr(definition, key).items()}
                for key in ('providers', 'endpoints', 'model_groups', 'auto_routes')}
    document.update(database=f12_pg.config(name), concurrency=definition.concurrency.model_dump(),
                    recovery=definition.recovery.model_dump())
    (directory/'gateway.yaml').write_text(yaml.safe_dump(document))
    # Deliberately synthetic tariffs, independent of public supplier prices.
    rates = dict(hit='1', miss='1', output='2')
    write_pricing(directory, {
        'bindings': {'controlled-main': 'controlled-fee-fixture'},
        'versions': [dict(supplier='controlled-fee-fixture', model=model, version='controlled-v1',
            effective_from='2020-01-01T00:00:00Z', peak=rates, off_peak=rates)
            for model in MODELS.values()]})
    migrate(directory); provision(directory)
    try:
        yield f12_pg, name, directory, definition
    finally:
        f12_pg.drop_database(name)


def reported(text, model, *, input_tokens=40, output_tokens=8):
    return ProviderResult(text=text, actual_model=model.model_id,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens,
                    total_tokens=input_tokens+output_tokens), cached_input_tokens=0,
        billing_observed_at=datetime.now(timezone.utc))


class PhaseProvider:
    """A finite phase script; the application must supply its real Prompt/Schema."""
    def __init__(self, *script):
        self.script = list(script); self.calls = []; self.closed = []; self.stream_closed = 0

    async def _next(self, transport, model, messages, options):
        phase = 'classification' if messages[0].content == CLASSIFIER_TEMPLATE.system_template else 'generation'
        assert self.script, 'unexpected controlled Provider call'
        expected, item = self.script.pop(0)
        assert phase == expected
        self.calls.append(dict(phase=phase, transport=transport, model=model.model_id,
                               messages=deepcopy(messages), options=deepcopy(options)))
        if phase == 'classification':
            material = loads(messages[-1].content)
            assert material['candidate_groups'] and material['task_material']['messages']
            assert options['response_schema'] == material['output_contract']['schema']
            assert options['response_schema']['additionalProperties'] is False
            assert options['max_output_tokens'] == 128
        try:
            if callable(item): return await item(model)
            if isinstance(item, BaseException): raise item
            tokens = (40, 8) if phase == 'classification' else (60, 12)
            return reported(item, model, input_tokens=tokens[0], output_tokens=tokens[1])
        finally:
            self.closed.append(phase)

    async def complete(self, model, messages, **options):
        return await self._next('complete', model, messages, options)

    async def stream(self, model, messages, **options):
        try:
            result = await self._next('stream', model, messages, options)
            yield ProviderStreamEvent(delta=result.text)
            yield ProviderStreamEvent(status='completed', actual_model=result.actual_model,
                usage=result.usage, cached_input_tokens=result.cached_input_tokens,
                billing_observed_at=result.billing_observed_at, upstream_terminal_received=True)
        finally:
            self.stream_closed += 1

    async def aclose(self):
        pass


def application(case, provider, *, definition=None):
    _, _, directory, original = case
    return create_app(settings=Settings(env='test', _env_file=None), config_dir=directory,
        model_registry=original if definition is None else definition,
        providers={'controlled-main': provider},
        token_budget=TokenBudget({'qwen_text_estimate_v1': FixedCounter(100, 'controlled-count-v1')}))


def body(*, streaming=False, model='auto-general'):
    return request(streaming=streaming, model=model).model_dump(exclude_none=True)


def get_trace(client, rid):
    result = client.get('/v1/traces/'+rid); assert result.status_code == 200, result.text
    return result.json()['data']


def bill(client, rid):
    result = client.get('/v1/billing/requests/'+rid); assert result.status_code == 200, result.text
    return result.json()['data']


def persisted(case, rid):
    pg, name, _, _ = case
    with pg.connect(name) as sql:
        row = sql.execute('SELECT id,status,timing_snapshot FROM tb_llm_request WHERE request_id=%s', (rid,)).fetchone()
        attempts = sql.execute('SELECT attempt_no,model,status,error_code,input_tokens,output_tokens,total_tokens FROM tb_llm_attempt WHERE request_pk=%s ORDER BY attempt_no', (row[0],)).fetchall()
        events = sql.execute('SELECT seq,event_type,payload FROM tb_llm_stream_event WHERE request_pk=%s ORDER BY seq', (row[0],)).fetchall()
        charges = sql.execute('SELECT attempt_no,state,agent_charge FROM tb_billing_attempt WHERE request_pk=%s ORDER BY attempt_no', (row[0],)).fetchall()
    return dict(status=row[1], metadata=loads(row[2]) if row[2] else {}, attempts=attempts, events=events, charges=charges)


def pending(client):
    result = client.post('/v1/llm/stream', json=body(streaming=True))
    assert result.status_code == 202, result.text
    return result.json()['data']['request_id']


def cancel(client, rid):
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        result = client.post('/v1/llm/stream/'+rid+'/cancel')
        assert result.status_code in (200, 202), result.text
        state = result.json()['data']['status']
        if state in {'completed', 'failed', 'canceled'}: return state
        assert state == 'canceling'
        time.sleep(.01)
    pytest.fail('cancel did not reach terminal')


def assert_auto(trace, *, source='classifier', fallback=None):
    auto = trace['auto_routing']
    assert auto['profile'] == 'auto-general'
    assert auto['candidate_groups'] == [dict(group=name,status='eligible',code=None)
                                       for name in ('light','general','analysis')]
    assert auto['resolved_group'] == 'general' and auto['source'] == source
    assert auto['fallback_reason'] == fallback
    assert auto['classifier_prompt_name'] == 'task_router' and auto['classifier_prompt_version'] == '1.0.0'
    assert auto['classification_duration_ms'] >= 0


def test_two_phase_completion_persists_purposes_aggregates_usage_and_reloads(lifecycle_case):
    provider = PhaseProvider(('classification', '{"group":"general"}'), ('generation', '订单A123充电器'))
    app = application(lifecycle_case, provider)
    with TestClient(app, headers=HEADERS) as client:
        result = client.post('/v1/llm', json=body()); assert result.status_code == 200, result.text
        rid = result.headers['x-request-id']; data = get_trace(client, rid)
        assert result.json()['data']['usage'] == {'input_tokens':100, 'output_tokens':20, 'total_tokens':120}
        assert result.json()['data']['text'] == '订单A123充电器'
        assert_auto(data)
        assert [(item['attempt_no'],item['purpose']) for item in data['attempts']] == [(1,'classification'),(2,'generation')]
        saved = persisted(lifecycle_case, rid)
        assert saved['status'] == 'completed'
        assert saved['metadata']['attempt_purposes'] == {'1':'classification','2':'generation'}
        assert dumps(saved['metadata']['auto_routing'],sort_keys=True) == dumps(data['auto_routing'],sort_keys=True)
        assert [item[1] for item in saved['attempts']] == ['light-e','general-e']
        assert [item[1:] for item in saved['charges']] == [('settled',Decimal('.000056')),('settled',Decimal('.000084'))]
        assert Decimal(bill(client,rid)['charged']) == Decimal('.00014')
        assert app.state.concurrency_limiter.snapshot() == NO_PERMITS
    unused = PhaseProvider()
    with TestClient(application(lifecycle_case, unused), headers=HEADERS) as client:
        restored = get_trace(client,rid)
        assert restored['auto_routing'] == data['auto_routing']
        assert [item['purpose'] for item in restored['attempts']] == ['classification','generation']
        assert persisted(lifecycle_case,rid) == saved and not unused.calls
    assert [call['phase'] for call in provider.calls] == ['classification','generation']


def test_invalid_classification_retains_failed_charged_attempt_and_generates_default(lifecycle_case):
    provider = PhaseProvider(('classification','not JSON'),('generation','默认回答'))
    with TestClient(application(lifecycle_case,provider),headers=HEADERS) as client:
        result = client.post('/v1/llm',json=body()); assert result.status_code == 200,result.text
        rid = result.headers['x-request-id']; data = get_trace(client,rid)
        assert_auto(data,source='default_fallback',fallback='invalid_json')
        assert [(a['purpose'],a['status'],a['error_code']) for a in data['attempts']] == [
            ('classification','failed','invalid_json'),('generation','completed',None)]
        assert result.json()['data']['usage']['total_tokens'] == 120
        saved = persisted(lifecycle_case,rid)
        assert saved['attempts'][0][2:4] == ('failed','invalid_json')
        assert [charge[1] for charge in saved['charges']] == ['settled','settled']
        assert Decimal(bill(client,rid)['charged']) == Decimal('.00014')
        assert [call['phase'] for call in provider.calls] == ['classification','generation']


def test_pending_is_free_first_subscription_classifies_once_and_replays_read_only(lifecycle_case):
    provider = PhaseProvider(('classification','{"group":"general"}'),('generation','订单A123充电器'))
    app = application(lifecycle_case,provider)
    with TestClient(app,headers=HEADERS) as client:
        rid = pending(client); before = persisted(lifecycle_case,rid)
        assert before['status'] == 'pending' and not before['attempts'] and not before['charges'] and not before['events']
        assert not provider.calls and app.state.concurrency_limiter.snapshot() == NO_PERMITS
        url = '/v1/llm/stream/'+rid+'/events'
        first = client.get(url); assert first.status_code == 200,first.text
        assert 'response.completed' in first.text
        saved = persisted(lifecycle_case,rid); data = get_trace(client,rid); billing = bill(client,rid)
        assert_auto(data)
        assert [a['purpose'] for a in data['attempts']] == ['classification','generation']
        assert [call['transport'] for call in provider.calls] == ['complete','stream']
        assert [event[0] for event in saved['events']] == list(range(1,len(saved['events'])+1))
        assert [event[1] for event in saved['events'] if event[1] in {'response.completed','response.failed','response.canceled'}] == ['response.completed']
        assert client.get(url,params={'after_seq':0}).text == first.text
        assert client.get(url,params={'after_seq':saved['events'][-1][0]}).status_code == 204
        assert persisted(lifecycle_case,rid) == saved and bill(client,rid) == billing
        assert len(provider.calls) == 2 and provider.stream_closed == 1
        assert app.state.concurrency_limiter.snapshot() == NO_PERMITS


def test_cancel_during_classification_stops_generation_and_releases_one_permit(lifecycle_case):
    entered = threading.Event(); cleaned = threading.Event()
    async def held(model):
        entered.set()
        try: await asyncio.sleep(60)
        finally: cleaned.set()
    provider = PhaseProvider(('classification',held))
    app = application(lifecycle_case,provider)
    with TestClient(app,headers=HEADERS) as client:
        rid = pending(client)
        with ThreadPoolExecutor(max_workers=1) as pool:
            read = pool.submit(client.get,'/v1/llm/stream/'+rid+'/events')
            assert entered.wait(4), 'classification did not start'
            assert app.state.concurrency_limiter.snapshot() == {'active_requests':1,'agents':{'legacy-test':1}}
            assert cancel(client,rid) == 'canceled'
            assert 'response.canceled' in read.result(timeout=5).text
        assert cleaned.is_set() and provider.closed == ['classification']
        assert [call['phase'] for call in provider.calls] == ['classification']
        data = get_trace(client,rid); saved = persisted(lifecycle_case,rid)
        assert data['status'] == 'canceled' and [a['purpose'] for a in data['attempts']] == ['classification']
        assert len(saved['attempts']) == len(saved['charges']) == 1
        assert saved['charges'][0][1:] == ('pending',None)
        assert [e[1] for e in saved['events'] if e[1] in {'response.canceling','response.canceled'}] == ['response.canceling','response.canceled']
        assert app.state.concurrency_limiter.snapshot() == NO_PERMITS


def test_cancel_after_classification_commit_prevents_generation_without_losing_charge(lifecycle_case):
    entered = threading.Event(); cleaned = threading.Event()
    provider = PhaseProvider(('classification','{"group":"general"}'))
    app = application(lifecycle_case,provider); finish = app.state.storage.finish_attempt
    async def observed_finish(trace,attempt,**options):
        status = await finish(trace,attempt,**options)
        if attempt.purpose == 'classification' and status == 'running':
            entered.set()
            try: await asyncio.sleep(60)
            finally: cleaned.set()
        return status
    app.state.storage.finish_attempt = observed_finish
    with TestClient(app,headers=HEADERS) as client:
        rid = pending(client)
        with ThreadPoolExecutor(max_workers=1) as pool:
            read = pool.submit(client.get,'/v1/llm/stream/'+rid+'/events')
            assert entered.wait(4), 'classification commit barrier was not reached'
            before = persisted(lifecycle_case,rid)
            assert before['attempts'][0][2] == 'completed'
            assert before['charges'][0][1:] == ('settled',Decimal('.000056'))
            assert cancel(client,rid) == 'canceled'
            assert 'response.canceled' in read.result(timeout=5).text
        assert cleaned.is_set() and [call['phase'] for call in provider.calls] == ['classification']
        saved = persisted(lifecycle_case,rid); data = get_trace(client,rid)
        assert data['status'] == 'canceled' and [a['purpose'] for a in data['attempts']] == ['classification']
        assert saved['attempts'][0][2] == 'completed'
        assert saved['charges'] == before['charges']
        assert Decimal(bill(client,rid)['charged']) == Decimal('.000056')
        assert app.state.concurrency_limiter.snapshot() == NO_PERMITS


def test_classification_actual_cost_creates_debt_and_blocks_new_generation(lifecycle_case):
    pg,name,_,_ = lifecycle_case
    with pg.connect(name) as sql:
        sql.execute("UPDATE tb_billing_operation SET amount=.0004 WHERE operation_id='initial-test-funds'")
    async def expensive(model): return reported('{"group":"general"}',model,input_tokens=1000,output_tokens=16)
    provider = PhaseProvider(('classification',expensive)); app = application(lifecycle_case,provider)
    with TestClient(app,headers=HEADERS) as client:
        result = client.post('/v1/llm',json=body()); assert result.status_code == 402,result.text
        assert result.json()['code'] == 'insufficient_budget'
        rid = result.headers['x-request-id']; saved = persisted(lifecycle_case,rid)
        assert saved['status'] == 'failed' and [call['phase'] for call in provider.calls] == ['classification']
        assert len(saved['charges']) == 1 and saved['charges'][0][1:] == ('settled',Decimal('.001032'))
        assert len(saved['attempts']) == 1, 'denied generation must not create an Attempt'
        assert [a['purpose'] for a in get_trace(client,rid)['attempts']] == ['classification']
        assert Decimal(client.get('/v1/billing/summary').json()['data']['available']) == Decimal('-.000632')
        assert app.state.concurrency_limiter.snapshot() == NO_PERMITS


@pytest.mark.parametrize('change',['profile','endpoint','unrelated'])
def test_pending_configuration_is_frozen_only_for_related_assets(lifecycle_case,change):
    _,_,_,definition = lifecycle_case
    before = PhaseProvider()
    with TestClient(application(lifecycle_case,before),headers=HEADERS) as client: rid = pending(client)
    if change == 'profile':
        profile = definition.auto_routes['auto-general'].model_copy(update={'default_group':'analysis'})
        updated = replace(definition,auto_routes={'auto-general':profile})
    elif change == 'endpoint':
        endpoints = dict(definition.endpoints)
        endpoints['analysis-e'] = endpoints['analysis-e'].model_copy(update={'max_output_tokens':2048})
        updated = replace(definition,endpoints=endpoints)
    else:
        groups = dict(definition.model_groups)
        groups['unrelated'] = groups['general'].model_copy(update={'routing_description':'unreferenced group'})
        updated = replace(definition,model_groups=groups)
    provider = PhaseProvider(('classification','{"group":"general"}'),('generation','订单A123充电器'))
    app = application(lifecycle_case,provider,definition=updated)
    with TestClient(app,headers=HEADERS) as client:
        response = client.get('/v1/llm/stream/'+rid+'/events')
        data = get_trace(client,rid); saved = persisted(lifecycle_case,rid)
        if change == 'unrelated':
            assert 'response.completed' in response.text and data['status'] == 'completed'
            assert len(provider.calls) == 2 and len(saved['charges']) == 2
        else:
            assert 'execution_configuration_changed' in response.text,response.text
            assert data['status'] == 'failed' and data['error_code'] == 'execution_configuration_changed'
            assert not provider.calls and not saved['attempts'] and not saved['charges']
        assert not before.calls and app.state.concurrency_limiter.snapshot() == NO_PERMITS


def test_historical_attempt_without_purpose_defaults_to_generation_read_only(lifecycle_case):
    provider = PhaseProvider(('generation','历史普通回答'))
    with TestClient(application(lifecycle_case,provider),headers=HEADERS) as client:
        result = client.post('/v1/llm',json=body(model='general')); assert result.status_code == 200,result.text
        rid = result.headers['x-request-id']
    pg,name,_,_ = lifecycle_case
    with pg.connect(name) as sql:
        metadata, = sql.execute('SELECT timing_snapshot FROM tb_llm_request WHERE request_id=%s',(rid,)).fetchone()
        metadata = loads(metadata); metadata.pop('attempt_purposes',None); metadata.pop('auto_routing',None)
        sql.execute('UPDATE tb_llm_request SET timing_snapshot=%s WHERE request_id=%s',(dumps(metadata),rid))
    saved = persisted(lifecycle_case,rid); unused = PhaseProvider()
    with TestClient(application(lifecycle_case,unused),headers=HEADERS) as client:
        data = get_trace(client,rid)
        assert data['auto_routing'] is None and data['attempts'][0]['purpose'] == 'generation'
        assert data['status'] == 'completed' and persisted(lifecycle_case,rid) == saved
        assert not unused.calls
