"""Selected automatic plan metadata must survive the real PostgreSQL boundary."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import threading

from fastapi.testclient import TestClient

from contracts import LLMRequest, StreamRequest
from errors import ProviderCallError
from f12_cases import request
from f12_cases import FixedCounter
from main import create_app
from services.gateway import GatewayService
from services.models import EndpointConfig, ProviderConfig
from services.prices import Catalog, PriceBook
from services.prompts import build_messages, fingerprint_messages
from services.token_budget import TokenBudget
from services.validation import prepare_schema
from settings import Settings
from test_f13_lifecycle import (f12_pg, lifecycle_case, application, PhaseProvider,
                                HEADERS, get_trace, bill, persisted, pending, cancel, NO_PERMITS)
from utils.json_codec import loads


FIELDS = ('structured_output_mode','prompt_fingerprint','schema_fingerprint',
          'input_token_count','counter_version')


def sql_metadata(case,rid):
    pg,name,_,_ = case
    with pg.connect(name) as sql:
        values = sql.execute('SELECT '+','.join(FIELDS)+' FROM tb_llm_request WHERE request_id=%s',(rid,)).fetchone()
    return dict(zip(FIELDS,values))


def test_auto_structured_nonfirst_group_persists_selected_generation_metadata(lifecycle_case):
    provider = PhaseProvider(('classification','{"group":"general"}'),('generation','{"order_id":"A123"}'))
    app = application(lifecycle_case,provider)
    payload = request(structured=True,model='auto-general').model_dump(exclude_none=True)
    with TestClient(app,headers=HEADERS) as client:
        reply = client.post('/v1/llm',json=payload)
        assert reply.status_code == 200,reply.text
        rid = reply.headers['x-request-id']; trace = get_trace(client,rid)
        assert trace['auto_routing']['resolved_group'] == 'general'
        expected = dict(structured_output_mode='json_schema',
            prompt_fingerprint=fingerprint_messages(provider.calls[1]['messages']),
            schema_fingerprint=prepare_schema(payload['response_schema']).fingerprint,
            input_token_count=100,counter_version='controlled-count-v1')
        assert {key:trace[key] for key in FIELDS} == expected
        assert sql_metadata(lifecycle_case,rid) == expected
        generation = trace['attempts'][1]
        assert generation['model'] == 'general-e'
        assert {key:generation[key] for key in ('prompt_fingerprint','input_token_count','counter_version')} == {
            key:expected[key] for key in ('prompt_fingerprint','input_token_count','counter_version')}


def mixed_application(case,provider,*,ordinary=False):
    _,_,directory,base = case
    endpoints = dict(base.endpoints)
    endpoints['general-e'] = EndpointConfig.model_validate(base.endpoints['general-e'].model_dump() | {
        'provider':'controlled-chat','structured_output_mode':'json_object','token_counter':'selected-counter'})
    groups = dict(base.model_groups)
    if ordinary:
        groups['ordinary-f12'] = groups['general'].model_copy(update={'candidates':('general-e','light-e')})
    definition = replace(base,endpoints=endpoints,model_groups=groups,providers={
        **base.providers,'controlled-chat':ProviderConfig(adapter='deepseek',api_protocol='chat_completions',
            base_url='http://127.0.0.1:9',credential_ref='unused-injected-chat')})
    app = create_app(settings=Settings(env='test',_env_file=None),config_dir=directory,
        model_registry=definition,providers={'controlled-main':provider,'controlled-chat':provider},
        token_budget=TokenBudget({'qwen_text_estimate_v1':FixedCounter(100,'first-count-v1'),
                                  'selected-counter':FixedCounter(350,'selected-count-v1')}))
    catalog = app.state.storage.prices.catalog.model_dump(mode='json')
    catalog['bindings']['controlled-chat'] = catalog['bindings']['controlled-main']
    app.state.storage.prices = PriceBook(Catalog.model_validate(catalog))
    return app


def expected_selected(app,payload,*,streaming=False):
    value = (StreamRequest if streaming else LLMRequest).model_validate(payload)
    schema = prepare_schema(payload['response_schema']) if 'response_schema' in payload else None
    contract = '{"mode":"json","schema":'+schema.canonical+'}' if schema else None
    messages = build_messages(value.prompt,value.messages,app.state.prompt_registry,output_contract=contract)
    metadata = dict(structured_output_mode='json_object' if schema else 'none',
        prompt_fingerprint=fingerprint_messages(messages),schema_fingerprint=schema.fingerprint if schema else None,
        input_token_count=350,counter_version='selected-count-v1')
    return metadata,messages


def assert_selected(case,client,rid,expected):
    data = get_trace(client,rid)
    assert data['auto_routing']['resolved_group'] == 'general'
    assert data['routing']['selected_endpoint'] == 'general-e'
    assert {key:data[key] for key in FIELDS} == expected
    assert sql_metadata(case,rid) == expected
    return data


def test_selected_json_object_uses_its_actual_user_schema_and_counter(lifecycle_case):
    provider = PhaseProvider(('classification','{"group":"general"}'),('generation','{"order_id":"A123"}'))
    app = mixed_application(lifecycle_case,provider)
    payload = request(structured=True,model='auto-general').model_dump(exclude_none=True)
    expected,messages = expected_selected(app,payload)
    with TestClient(app,headers=HEADERS) as client:
        reply = client.post('/v1/llm',json=payload)
        assert reply.status_code == 200,reply.text
        rid = reply.headers['x-request-id']; data = assert_selected(lifecycle_case,client,rid,expected)
        assert provider.calls[1]['messages'] == messages
        assert provider.calls[1]['options']['response_schema'] == payload['response_schema']
        user = messages[-1].content
        assert prepare_schema(payload['response_schema']).canonical in user
        native_messages = build_messages(LLMRequest.model_validate(payload).prompt,
            LLMRequest.model_validate(payload).messages,app.state.prompt_registry,output_contract='{"mode":"json"}')
        assert user != native_messages[-1].content
        classification,generation = data['attempts']
        assert classification['counter_version'] == 'first-count-v1' and classification['input_token_count'] == 100
        assert generation['api_protocol'] == 'chat_completions' and generation['model'] == 'general-e'
        assert {key:generation[key] for key in ('prompt_fingerprint','input_token_count','counter_version')} == {
            key:expected[key] for key in ('prompt_fingerprint','input_token_count','counter_version')}
        pg,name,_,_ = lifecycle_case
        with pg.connect(name) as sql:
            attempt = sql.execute('SELECT a.prompt_fingerprint,a.input_token_count,a.counter_version,a.input_snapshot '
                'FROM tb_llm_attempt a JOIN tb_llm_request r ON a.request_pk=r.id '
                'WHERE r.request_id=%s AND a.attempt_no=2',(rid,)).fetchone()
        assert attempt[:3] == (expected['prompt_fingerprint'],350,'selected-count-v1')
        assert prepare_schema(payload['response_schema']).canonical in loads(attempt[3])['messages'][-1]['content']
        assert app.state.concurrency_limiter.snapshot() == NO_PERMITS


def test_selected_metadata_survives_generation_billing_rejection_before_registration(lifecycle_case):
    pg,name,_,_ = lifecycle_case
    with pg.connect(name) as sql:
        sql.execute("UPDATE tb_billing_operation SET amount=.0004 WHERE operation_id='initial-test-funds'")
    provider = PhaseProvider(('classification','{"group":"general"}'))
    app = mixed_application(lifecycle_case,provider)
    payload = request(structured=True,model='auto-general').model_dump(exclude_none=True)
    expected,_ = expected_selected(app,payload)
    with TestClient(app,headers=HEADERS) as client:
        reply = client.post('/v1/llm',json=payload)
        assert reply.status_code == 402 and reply.json()['code'] == 'insufficient_budget',reply.text
        rid = reply.headers['x-request-id']; data = assert_selected(lifecycle_case,client,rid,expected)
        assert data['status'] == 'failed' and len(data['attempts']) == 1
        assert data['attempts'][0]['purpose'] == 'classification' and data['attempts'][0]['status'] == 'completed'
        saved = persisted(lifecycle_case,rid)
        assert len(saved['attempts']) == len(saved['charges']) == 1
        assert saved['charges'] == [(1,'settled',Decimal('.000056'))]
        assert Decimal(bill(client,rid)['charged']) == Decimal('.000056')
        assert len(provider.calls) == 1 and provider.calls[0]['phase'] == 'classification'
        assert app.state.concurrency_limiter.snapshot() == NO_PERMITS


def test_selected_metadata_survives_cancel_before_generation_registration(lifecycle_case,monkeypatch):
    selected = threading.Event(); cleaned = threading.Event(); hold = {}
    original = GatewayService.resolve_auto_plan
    async def pause_after_selection(service,*args,**options):
        plan = await original(service,*args,**options)
        hold['gate'] = asyncio.Event(); selected.set()
        try: await hold['gate'].wait()
        finally: cleaned.set()
        return plan
    monkeypatch.setattr(GatewayService,'resolve_auto_plan',pause_after_selection)
    provider = PhaseProvider(('classification','{"group":"general"}'))
    app = mixed_application(lifecycle_case,provider)
    payload = request(streaming=True,model='auto-general').model_dump(exclude_none=True)
    expected,_ = expected_selected(app,payload,streaming=True)
    with TestClient(app,headers=HEADERS) as client:
        rid = pending(client)
        with ThreadPoolExecutor(max_workers=1) as pool:
            reader = pool.submit(client.get,'/v1/llm/stream/'+rid+'/events')
            try:
                assert selected.wait(4), 'actual plan selection did not finish'
                assert cancel(client,rid) == 'canceled'
                assert 'response.canceled' in reader.result(timeout=5).text
            finally:
                if hold and not cleaned.is_set(): client.portal.call(hold['gate'].set)
        data = assert_selected(lifecycle_case,client,rid,expected)
        assert data['status'] == 'canceled' and cleaned.is_set()
        assert len(provider.calls) == len(data['attempts']) == 1
        assert data['attempts'][0]['purpose'] == 'classification' and data['attempts'][0]['status'] == 'completed'
        saved = persisted(lifecycle_case,rid)
        assert saved['charges'] == [(1,'settled',Decimal('.000056'))]
        assert [event[1] for event in saved['events'] if event[1] in {
            'response.completed','response.failed','response.canceled'}] == ['response.canceled']
        assert app.state.concurrency_limiter.snapshot() == NO_PERMITS


def test_ordinary_f12_recovery_keeps_original_entry_metadata(lifecycle_case):
    provider = PhaseProvider(('generation',ProviderCallError(code='provider_unavailable',transient=True,
        retry_after_seconds=60)),('generation','{"order_id":"A123"}'))
    app = mixed_application(lifecycle_case,provider,ordinary=True)
    payload = request(structured=True,model='ordinary-f12').model_dump(exclude_none=True)
    expected,_ = expected_selected(app,payload)
    with TestClient(app,headers=HEADERS) as client:
        reply = client.post('/v1/llm',json=payload)
        assert reply.status_code == 200,reply.text
        rid = reply.headers['x-request-id']; data = get_trace(client,rid)
        assert data['auto_routing'] is None
        assert data['routing']['selected_endpoint'] == 'general-e'
        assert len(provider.calls) == len(data['attempts']) == 2
        assert [attempt['model'] for attempt in data['attempts']] == ['general-e','light-e']
        assert data['attempts'][1]['counter_version'] == 'first-count-v1'
        assert data['attempts'][1]['input_token_count'] == 100
        assert data['attempts'][1]['prompt_fingerprint'] != expected['prompt_fingerprint']
        assert {key:data[key] for key in FIELDS} == expected
        assert sql_metadata(lifecycle_case,rid) == expected
