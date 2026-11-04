import asyncio
from copy import deepcopy
from decimal import Decimal
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from contracts import Usage
from f3_cases import MOCK_USAGE
from f4_cases import make_app, payload, PARSED, SCHEMA, TEXT
from providers.base import ProviderResult
from providers.mock import MockProvider
from services.prompts import fingerprint_messages
from services.token_counters.deepseek_v41 import DeepSeekV41Counter
from services.token_budget import TokenBudget


def send(body, provider=None, **options):
    provider=provider or MockProvider(text=TEXT)
    app=make_app(provider,**options)
    with TestClient(app,raise_server_exceptions=False) as client:
        response=client.post('/v1/llm',json=body)
    trace=app.state.traces.requests[response.headers['X-Request-ID']]
    attempts=[a for a in app.state.traces.attempts if a.request_id==trace.request_id]
    assert set(response.json())=={'success','code','data','message'}
    assert len(provider.calls)==trace.attempts==len(attempts)
    return response,trace,attempts,provider


@pytest.mark.parametrize('model,mode',[('general-primary','json_object'),('general-backup','json_schema')])
def test_structured_success_public_shape_constraints_and_trace(model,mode):
    body=payload(model)
    original=deepcopy(body)
    response,trace,attempts,provider=send(body)
    assert response.status_code==200,response.json()
    assert response.json()['data']=={'text':TEXT,'parsed':PARSED,'usage':MOCK_USAGE}
    assert trace.status==attempts[0].status=='completed'
    assert trace.attempts==1 and trace.structured_output_mode==mode
    assert trace.schema_fingerprint and trace.prompt_fingerprint==fingerprint_messages(provider.calls[0][1])
    assert trace.input_token_count>0
    sent=provider.calls[0][1][-1].content
    assert ('"schema":' in sent)==(mode=='json_object')
    assert provider.call_options[0]['response_schema']==json.loads(json.dumps(SCHEMA,sort_keys=True))
    assert body==original


@pytest.mark.parametrize('model',['general-primary','general-backup'])
def test_ordinary_text_on_structured_capable_model_is_unchanged(model):
    body=payload(model);body.pop('response_schema')
    response,trace,attempts,provider=send(body,MockProvider(text='普通回答'))
    assert response.status_code==200
    assert response.json()['data']=={'text':'普通回答','usage':MOCK_USAGE}
    assert trace.schema_fingerprint is None and trace.structured_output_mode=='none'
    assert 'response_schema' not in provider.call_options[0]


@pytest.mark.parametrize('value,code,status',[
    (None,'invalid_request',422),([], 'invalid_request',422),
    ({},'invalid_response_schema',400),({'type':'array'},'invalid_response_schema',400),
    ({'type':'object','unknown':True},'invalid_response_schema',400),
])
def test_schema_preflight_rejects_without_a_provider_attempt(value,code,status):
    body=payload();body['response_schema']=value
    response,trace,attempts,provider=send(body)
    assert (response.status_code,response.json()['code'])==(status,code)
    assert response.json()['data'] is None and trace.status=='failed'
    assert provider.calls==attempts==[]


@pytest.mark.parametrize('version',['1.0.0','1.1.0'])
def test_old_text_templates_do_not_silently_accept_schema(version):
    body=payload();body['prompt']['version']=version
    response,trace,attempts,provider=send(body)
    assert response.status_code==400 and response.json()['code']=='unsupported_capability'
    assert trace.schema_fingerprint and trace.prompt_fingerprint is None
    assert provider.calls==attempts==[]


def test_model_without_capability_is_rejected_before_assembly():
    response,trace,attempts,provider=send(payload(),changes={'general-primary':{'structured_output_mode':'none'}})
    assert response.status_code==400 and response.json()['code']=='unsupported_capability'
    assert provider.calls==attempts==[] and trace.prompt_fingerprint is None


@pytest.mark.parametrize('text,code',[
    ('{"order_id":','invalid_json'), ('```json\n{}\n```','invalid_json'),
    ('{} {}','invalid_json'), ('正文 {}','invalid_json'), ('{"a":1,"a":2}','invalid_json'),
    ('{"a":NaN}','invalid_json'),('[]','schema_validation_failed'),('null','schema_validation_failed'),
    ('{}','schema_validation_failed'),(TEXT.replace('"quantity":1','"quantity":"1"'),'schema_validation_failed'),
])
def test_completed_upstream_only_succeeds_after_json_and_schema_acceptance(text,code):
    usage=Usage(input_tokens=10,output_tokens=5,total_tokens=15)
    provider=MockProvider(result=ProviderResult(text=text,actual_model='fixture-model',usage=usage,upstream_status='completed'))
    response,trace,attempts,_=send(payload(),provider)
    assert response.status_code==502 and response.json()['code']==code
    assert response.json()['data'] is None and trace.status=='failed'
    attempt=attempts[0]
    assert attempt.status=='failed' and attempt.error_code==code
    assert attempt.upstream_status=='completed' and attempt.usage==usage and attempt.actual_model=='fixture-model'


@pytest.mark.parametrize('reason,code',[('length','output_truncated'),('refusal','provider_refused'),('content_filter','provider_content_filtered')])
def test_end_state_failure_precedes_json_parsing(reason,code):
    response,trace,attempts,_=send(payload(),MockProvider(result=ProviderResult(text='broken JSON',finish_reason=reason)))
    assert response.status_code==502 and response.json()['code']==code
    assert trace.status==attempts[0].status=='failed' and trace.attempts==1


@pytest.mark.parametrize('change,code',[
    ({'response_schema':{},'model':'absent','prompt':{'name':'absent','version':'1'}},'invalid_response_schema'),
    ({'model':'absent','prompt':{'name':'absent','version':'1'}},'unknown_prompt_template'),
    ({'model':'absent'},'unknown_model'),
    ({'model':'absent','prompt':{'name':'context_answer','version':'1.2.0','variables':{'context':'ok','output_contract':'override'}}},'invalid_prompt_variables'),
])
def test_preflight_order_and_no_partial_message_fingerprint(change,code):
    body=payload();body.update(change)
    response,trace,attempts,provider=send(body)
    assert response.json()['code']==code
    assert provider.calls==attempts==[] and trace.prompt_fingerprint is None


def test_native_schema_has_an_independent_fingerprint():
    first=payload('general-backup');second=deepcopy(first)
    second['response_schema']['properties']['order_id']['description']='订单编号'
    a=send(first);b=send(second)
    assert a[0].status_code==b[0].status_code==200
    assert a[1].prompt_fingerprint==b[1].prompt_fingerprint
    assert a[1].schema_fingerprint!=b[1].schema_fingerprint


def test_chat_counts_the_api_json_mode_flag_as_well_as_user_schema():
    response,trace,attempts,provider=send(payload())
    assert response.status_code==200
    counter=DeepSeekV41Counter()
    messages=provider.calls[0][1]
    expected=counter.count(messages,generation_constraint={'type':'json_object'})
    assert trace.input_token_count==expected


def test_structured_numbers_preserve_original_json_precision_on_the_wire():
    body=payload();body['response_schema']={'type':'object','properties':{'n':{'type':'number'}},'required':['n']}
    text=' {"n":1.0000000000000001} '
    response,trace,attempts,_=send(body,MockProvider(text=text))
    assert response.status_code==200
    data=json.loads(response.text,parse_float=Decimal)['data']
    assert data['parsed']['n']==Decimal('1.0000000000000001')
    assert data['text']==text


async def test_parallel_schemas_and_modes_do_not_share_assembly_state():
    entered=asyncio.Event();release=asyncio.Event()
    class Interleaved(MockProvider):
        async def complete(self,model,messages,**options):
            result=await super().complete(model,messages,**options)
            if model.structured_output_mode=='json_object':
                entered.set();await release.wait()
            else:
                await entered.wait();release.set()
            return result
    provider=Interleaved(text=TEXT);app=make_app(provider)
    bodies=[payload('general-primary'),payload('general-backup')]
    bodies[1]['response_schema']['description']='backup-only schema'
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            responses=await asyncio.gather(*[client.post('/v1/llm',json=body) for body in bodies])
    assert all(r.status_code==200 for r in responses)
    modes=set()
    for response in responses:
        trace=app.state.traces.requests[response.headers['X-Request-ID']]
        assert trace.status=='completed' and trace.attempts==1
        modes.add(trace.structured_output_mode)
    assert modes=={'json_object','json_schema'}
    assert len({app.state.traces.requests[r.headers['X-Request-ID']].schema_fingerprint for r in responses})==2
    calls={model.structured_output_mode:messages for model,messages in provider.calls}
    assert '"schema":' in calls['json_object'][-1].content
    assert '"schema":' not in calls['json_schema'][-1].content


@pytest.mark.parametrize('model',['general-primary','general-backup'])
@pytest.mark.parametrize('input_tokens,status',[(24,200),(25,400)])
def test_complete_generation_window_boundary_is_checked_before_attempt(model,input_tokens,status):
    class Counter:
        version='bounded-fixture-v1'
        supports_generation_constraints=True
        def supports(self,model):return True
        def count(self,messages,**options):return input_tokens
    changes={name:dict(default_output_tokens=16,max_output_tokens=32,safety_margin_tokens=8,context_window_tokens=48)
             for name in ['general-primary','general-backup']}
    body=payload(model);body['max_output_tokens']=16
    response,trace,attempts,provider=send(body,changes=changes,budget=TokenBudget({'deepseek_v41':Counter()}))
    assert response.status_code==status
    assert trace.input_token_count==input_tokens and trace.counter_version=='bounded-fixture-v1'
    if status==400:
        assert response.json()['code']=='context_length_exceeded' and provider.calls==attempts==[]
    else:
        assert provider.call_options[0]['max_output_tokens']==16 and trace.attempts==1


def test_counter_failure_keeps_confirmed_metadata_but_has_no_attempt():
    class Counter:
        version='failed-counter-v1'
        supports_generation_constraints=True
        def supports(self,model):return True
        def count(self,messages,**options):raise RuntimeError('PRIVATE_COUNTER_SENTINEL')
    response,trace,attempts,provider=send(payload(),budget=TokenBudget({'deepseek_v41':Counter()}))
    assert response.status_code==500 and response.json()['code']=='internal_error'
    assert trace.counter_version=='failed-counter-v1' and trace.schema_fingerprint and trace.prompt_fingerprint
    assert trace.input_token_count is None and provider.calls==attempts==[]
    assert 'PRIVATE_COUNTER_SENTINEL' not in response.text


@pytest.mark.parametrize('field,value,code',[
    ('max_output_tokens',4097,'invalid_output_budget'),('timeout_seconds',121,'invalid_timeout'),
])
def test_schema_does_not_bypass_existing_output_and_timeout_caps(field,value,code):
    body=payload();body[field]=value
    response,trace,attempts,provider=send(body)
    assert response.status_code==400 and response.json()['code']==code
    assert provider.calls==attempts==[]


@pytest.mark.parametrize('model',['general-primary','general-backup'])
def test_final_user_limit_still_applies_with_output_contract(model):
    body=payload(model);body['messages'][0]['content']='x'*20_000
    response,trace,attempts,provider=send(body)
    assert response.status_code==400 and response.json()['code']=='prompt_too_long'
    assert trace.schema_fingerprint and trace.prompt_fingerprint is None
    assert provider.calls==attempts==[]


def test_validation_program_error_does_not_leak_or_succeed(monkeypatch):
    def broken(*args):raise RuntimeError('PRIVATE_VALIDATION_SENTINEL')
    monkeypatch.setattr('services.gateway.validate_output',broken)
    response,trace,attempts,_=send(payload())
    assert response.status_code==500 and response.json()['code']=='internal_error'
    assert trace.status==attempts[0].status=='failed'
    assert 'PRIVATE_VALIDATION_SENTINEL' not in response.text


def test_schema_and_private_content_are_absent_from_trace():
    body=payload();marker='PRIVATE_BUSINESS_SENTINEL'
    body['prompt']['variables']['context']=marker
    body['response_schema']['description']=marker
    response,trace,attempts,_=send(body,MockProvider(text=marker))
    assert response.json()['code']=='invalid_json'
    assert marker not in response.text
    assert marker not in repr(vars(trace))+repr([vars(a) for a in attempts])


def test_openapi_explains_subset_without_adding_a_capability_route():
    app=make_app()
    document=app.openapi()
    assert '/v1/capabilities/response-schema' not in document['paths']
    assert 'docs/response-schema.md' in document['paths']['/v1/llm']['post']['description']
    field=document['components']['schemas']['LLMRequest']['properties']['response_schema']
    assert field['type']=='object' and 'default' not in field


@pytest.mark.parametrize('keyword',['minimum','const'])
def test_schema_decimal_literal_is_not_silently_rounded(keyword):
    body=payload()
    body['response_schema']={'type':'object','properties':{'n':{'type':'number',keyword:'EXACT_DECIMAL_LITERAL'}}}
    raw=json.dumps(body).replace('"EXACT_DECIMAL_LITERAL"','1.0000000000000001')
    provider=MockProvider(text='{"n":1}')
    app=make_app(provider)
    with TestClient(app,raise_server_exceptions=False) as client:
        response=client.post('/v1/llm',content=raw,headers={'Content-Type':'application/json'})
    assert response.status_code==502
    assert response.json()['code']=='schema_validation_failed'
    assert len(provider.calls)==1


@pytest.mark.parametrize('model',['general-primary','general-backup'])
def test_exact_schema_decimal_round_trip_is_not_rejected_or_rounded(model):
    body=payload(model)
    body['response_schema']={'type':'object','properties':{'n':{'const':'EXACT_DECIMAL_LITERAL'}}}
    raw=json.dumps(body).replace('"EXACT_DECIMAL_LITERAL"','1.0000000000000001')
    provider=MockProvider(text='{"n":1.0000000000000001}')
    app=make_app(provider)
    with TestClient(app,raise_server_exceptions=False) as client:
        response=client.post('/v1/llm',content=raw,headers={'Content-Type':'application/json'})
    assert response.status_code==200,response.json()['code']
    assert json.loads(response.text,parse_float=Decimal)['data']['parsed']['n']==Decimal('1.0000000000000001')
    actual=provider.call_options[0]['response_schema']['properties']['n']['const']
    assert isinstance(actual,Decimal) and actual==Decimal('1.0000000000000001')
    if model=='general-primary':assert '1.0000000000000001' in provider.calls[0][1][-1].content


def test_allowed_extra_json_does_not_fail_during_response_serialization():
    body=payload();body['response_schema']={'type':'object'};body['max_output_tokens']=2048
    text='{"x":'+'['*600+'0'+']'*600+'}'
    response,trace,attempts,_=send(body,MockProvider(text=text))
    assert response.status_code==200,response.json()['code']
    assert trace.status==attempts[0].status=='completed'
    assert '"parsed":'+text in response.text
