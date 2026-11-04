from registry_helpers import ModelRegistry
"""F7 service edge evidence. Real PromptRegistry/TokenBudget; no PG or paid API."""
from services.models import ConcurrencyPolicy
from services.concurrency import ConcurrencyLimiter
import asyncio
from copy import deepcopy
from decimal import Decimal
from time import monotonic

import pytest

from contracts import LLMRequest, Usage
from errors import GatewayError, ProviderCallError
from f4_cases import registry, payload, TEXT, PARSED
from providers.base import ProviderResult
from services.gateway import GatewayService
from services.models import ProviderConfig, RecoveryPolicy
from services.prompts import DEFAULT_TEMPLATES, PromptRegistry, build_messages
from services.token_budget import TokenBudget
from services.trace import TraceStore


class SequenceProvider:
    def __init__(self, *items):
        self.items=list(items); self.calls=[]

    async def complete(self, model, messages, **options):
        self.calls.append((monotonic(),model,deepcopy(messages),deepcopy(options)))
        assert self.items, 'unexpected extra Provider call'
        item=self.items.pop(0)
        if isinstance(item, BaseException): raise item
        if callable(item): return await item()
        return item


def text_request(**kw):
    return LLMRequest.model_validate(dict(model='general-primary',prompt={'name':'basic_text','version':'1.0.0'},
        messages=[{'role':'user','content':'订单A123商品破损，请分析'}],max_output_tokens=128,**kw))


def setup(primary, backup=None, changes=None, *, policy=None, providers=None):
    changes=deepcopy(changes or {})
    changes.setdefault('general-primary',{}).setdefault('fallback_models',['general-backup'])
    r=registry(changes)
    r=ModelRegistry(r.endpoints,providers or r.providers,model_groups=r.model_groups,recovery=policy or RecoveryPolicy(base_delay_seconds=0,max_delay_seconds=0), concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))
    traces=TraceStore(); budget=TokenBudget.from_models(r.endpoints.values())
    svc=GatewayService({'mock-main':primary,'mock-backup':backup or SequenceProvider()},traces,
        PromptRegistry(DEFAULT_TEMPLATES),r,budget, limiter=ConcurrencyLimiter((r).concurrency))
    trace=traces.start_request('00000000-0000-0000-0000-000000000077')
    trace.agent_id = 'unit-agent'
    return svc,traces,trace


def connection(**kw): return ProviderCallError(code='provider_connection_error',**kw)
def ok(text='订单A123需要人工确认',usage=None): return ProviderResult(text=text,usage=usage)


async def test_r2_actual_backup_and_three_calls():
    a=SequenceProvider(connection(),connection());b=SequenceProvider(ok())
    svc,t,tr=setup(a,b);data,_=await svc._complete(text_request(),tr)
    assert data.text=='订单A123需要人工确认'
    assert len(a.calls)==2 and len(b.calls)==1
    assert [x.model for x in t.attempts]==['general-primary','general-primary','general-backup']
    assert [x.status for x in t.attempts]==['failed','failed','completed']


@pytest.mark.parametrize('fallback', [True,False])
async def test_r2_exhaustion_preserves_last_real_error(fallback):
    a=SequenceProvider(connection(),ProviderCallError(code='provider_timeout'))
    b=SequenceProvider(ProviderCallError(code='provider_rate_limited'))
    svc,t,tr=setup(a,b,{'general-primary':{'fallback_models':['general-backup'] if fallback else []}})
    with pytest.raises(ProviderCallError) as e: await svc._complete(text_request(),tr)
    assert e.value.code==('provider_rate_limited' if fallback else 'provider_timeout')
    assert len(a.calls)==2 and len(b.calls)==int(fallback)


@pytest.mark.parametrize('code,meta',[(x,{}) for x in ['provider_auth_error','provider_quota_exceeded',
    'provider_rejected_request','provider_error','provider_invalid_response','provider_refused','provider_content_filtered']]+
    [('provider_unavailable',{'upstream_http_status':v}) for v in [501,505]])
async def test_r3_permanent_failure_never_uses_backup(code,meta):
    a=SequenceProvider(ProviderCallError(code=code,**meta));b=SequenceProvider(ok())
    svc,t,tr=setup(a,b)
    with pytest.raises(ProviderCallError) as e:await svc._complete(text_request(),tr)
    assert e.value.code==code and len(a.calls)==1 and not b.calls
    assert len(t.attempts)==1 and t.attempts[0].error_code==code


@pytest.mark.parametrize('change,structured',[
    ({'structured_output_mode':'none'},True),({'max_output_tokens':64,'default_output_tokens':64},False),
    ({'context_window_tokens':1153,'default_output_tokens':64,'max_output_tokens':128},False)])
async def test_r4_incompatible_backup_has_zero_attempts(change,structured):
    a=SequenceProvider(connection(),connection());b=SequenceProvider(ok())
    svc,t,tr=setup(a,b,{'general-backup':change})
    req=LLMRequest.model_validate(payload()) if structured else text_request()
    with pytest.raises(ProviderCallError) as e:await svc._complete(req,tr)
    assert e.value.code=='provider_connection_error' and not b.calls
    assert len(t.attempts)==2 and tr._skipped_candidates[0]['model']=='general-backup'


async def test_r5_structured_fallback_reassembles_and_recounts():
    a=SequenceProvider(connection(),connection());b=SequenceProvider(ok(TEXT))
    svc,t,tr=setup(a,b);req=LLMRequest.model_validate(payload());data,_=await svc._complete(req,tr)
    assert data.parsed==PARSED
    source=a.calls[0][2];target=b.calls[0][2]
    assert source[0]==target[0] and source!=target
    assert '"schema":' in source[-1].content and '"schema":' not in target[-1].content
    assert a.calls[0][3]['response_schema']==b.calls[0][3]['response_schema']
    assert t.attempts[0].prompt_fingerprint!=t.attempts[-1].prompt_fingerprint
    for attempt,call in [(t.attempts[0],a.calls[0]),(t.attempts[-1],b.calls[0])]:
        count=svc.token_budget.count(call[1],call[2],response_schema=call[3]['response_schema'])
        assert attempt.input_token_count==count.input_tokens


@pytest.mark.parametrize('second', [ProviderResult(text='half',finish_reason='length'),ok('bad JSON')])
async def test_r6_two_primary_slots_do_not_grant_growth_or_repair(second):
    a=SequenceProvider(connection(),second);b=SequenceProvider(ok(TEXT))
    svc,t,tr=setup(a,b)
    req=LLMRequest.model_validate(dict(payload(),initial_output_tokens=32))
    with pytest.raises((GatewayError,ProviderCallError)) as e:await svc._complete(req,tr)
    assert e.value.code in {'output_truncated','invalid_json'}
    assert len(a.calls)==2 and not b.calls


@pytest.mark.parametrize('result,code',[
    (ok('again invalid'),'invalid_json'),(ok('{}'),'schema_validation_failed'),
    (ProviderResult(text='half',finish_reason='length'),'output_truncated'),
    (connection(),'provider_connection_error'),
    (ProviderResult(text='',finish_reason='refusal'),'provider_refused')])
async def test_j3_repair_outcome_never_opens_another_chain(result,code):
    a=SequenceProvider(ok('bad JSON'),result);b=SequenceProvider(ok(TEXT))
    svc,t,tr=setup(a,b)
    with pytest.raises((GatewayError,ProviderCallError)) as e:await svc._complete(LLMRequest.model_validate(payload()),tr)
    assert e.value.code==code and len(a.calls)==2 and not b.calls
    assert [x.reason for x in t.attempts]==['initial','format_repair']


async def test_j4_oversized_failed_candidate_stops_without_trimming():
    a=SequenceProvider(ok('x '*10000));b=SequenceProvider(ok(TEXT))
    svc,t,tr=setup(a,b,{'general-primary':{'context_window_tokens':3000}})
    with pytest.raises(GatewayError) as e:await svc._complete(LLMRequest.model_validate(payload()),tr)
    assert e.value.code=='invalid_json' and len(a.calls)==1 and not b.calls
    assert tr._recovery_stop_reason=='repair_preflight_failed' and len(t.attempts)==1


@pytest.mark.parametrize('raw',['{"order_id":"A123","order_id":"B456"}', '{"x":NaN}',TEXT+' trailing'])
async def test_j5_strict_parser_failures_use_single_repair(raw):
    a=SequenceProvider(ok(raw),ok(TEXT));svc,t,tr=setup(a)
    data,_=await svc._complete(LLMRequest.model_validate(payload()),tr)
    assert data.parsed==PARSED and [x.reason for x in t.attempts]==['initial','format_repair']


async def test_j5_truncated_json_grows_instead_of_repair():
    a=SequenceProvider(ProviderResult(text='{"order_id":',finish_reason='length'),ok(TEXT))
    svc,t,tr=setup(a)
    data,_=await svc._complete(LLMRequest.model_validate(dict(payload(),initial_output_tokens=32)),tr)
    assert data.parsed==PARSED and [x.reason for x in t.attempts]==['initial','output_growth']
    assert len(a.calls[1][2])==len(a.calls[0][2])


async def test_j6_backup_format_failure_has_no_fourth_call():
    a=SequenceProvider(connection(),connection());b=SequenceProvider(ok('bad JSON'))
    svc,t,tr=setup(a,b)
    with pytest.raises(GatewayError) as e:await svc._complete(LLMRequest.model_validate(payload()),tr)
    assert e.value.code=='invalid_json' and len(a.calls)==2 and len(b.calls)==1


@pytest.mark.parametrize('initial',[None,32])
async def test_g2_at_authorized_max_truncation_stops(initial):
    items=[ProviderResult(text='half',finish_reason='length')]*(1 if initial is None else 2)
    a=SequenceProvider(*items);b=SequenceProvider(ok())
    svc,t,tr=setup(a,b)
    with pytest.raises(ProviderCallError) as e:await svc._complete(text_request(**({'initial_output_tokens':initial} if initial else {})),tr)
    assert e.value.code=='output_truncated' and not b.calls and len(a.calls)==len(items)


async def test_g3_growth_survives_network_fallback():
    a=SequenceProvider(ProviderResult(text='partial',finish_reason='length'),connection());b=SequenceProvider(ok('whole answer'))
    svc,t,tr=setup(a,b);data,_=await svc._complete(text_request(initial_output_tokens=32),tr)
    assert data.text=='whole answer'
    assert [call[3]['max_output_tokens'] for call in a.calls+b.calls]==[32,128,128]


@pytest.mark.parametrize('unknown_index',[None,0,1,2])
async def test_p4_usage_aggregates_every_attempt_or_null(unknown_index):
    values=[Usage(input_tokens=3,output_tokens=2,total_tokens=5),Usage(input_tokens=4,output_tokens=1,total_tokens=5),Usage(input_tokens=2,output_tokens=2,total_tokens=4)]
    if unknown_index is not None:values[unknown_index]=None
    a=SequenceProvider(connection(usage=values[0]),connection(usage=values[1]));b=SequenceProvider(ok(usage=values[2]))
    svc,t,tr=setup(a,b);data,_=await svc._complete(text_request(),tr)
    assert data.usage==(Usage(input_tokens=9,output_tokens=5,total_tokens=14) if unknown_index is None else None)
    assert [x.usage for x in t.attempts]==values


@pytest.mark.parametrize('extra',[0,1])
async def test_a3_exact_context_boundary_reserves_max_not_initial(extra):
    a=SequenceProvider(ok());svc,_,_=setup(a)
    req=text_request(initial_output_tokens=1)
    model=svc.model_registry.get_endpoint('general-primary')
    messages=build_messages(req.prompt,req.messages,svc.prompt_registry)
    count=svc.token_budget.count(model,messages).input_tokens
    window=count+128+model.safety_margin_tokens-extra
    svc,t,tr=setup(a,changes={'general-primary':{'context_window_tokens':window,'default_output_tokens':128,'fallback_models':[]}})
    if extra:
        with pytest.raises(GatewayError) as e:await svc._complete(req,tr)
        assert e.value.code=='context_length_exceeded' and not a.calls and not t.attempts
    else:
        await svc._complete(req,tr);assert a.calls[0][3]['max_output_tokens']==1


@pytest.mark.parametrize('change,expected',[
    ({'model':'missing'},'unknown_model'),
    ({'prompt':{'name':'missing','version':'1.0.0'}},'unknown_prompt_template'),
    ({'prompt':{'name':'context_answer','version':'1.2.0'}},'missing_prompt_variable')])
async def test_a4_deterministic_rejections_zero_calls(change,expected):
    a=SequenceProvider(ok());b=SequenceProvider(ok());svc,t,tr=setup(a,b)
    values=text_request().model_dump(exclude_none=True);values.update(change)
    with pytest.raises(GatewayError) as e:await svc._complete(LLMRequest.model_validate(values),tr)
    assert e.value.code==expected and not a.calls and not b.calls and not t.attempts


async def test_t4_retry_after_larger_than_remaining_skips_to_independent_backup():
    a=SequenceProvider(ProviderCallError(code='provider_rate_limited',retry_after_seconds=15));b=SequenceProvider(ok())
    svc,t,tr=setup(a,b);req=text_request(timeout_seconds=1)
    await svc._complete(req,tr)
    assert len(a.calls)==1 and len(b.calls)==1 and [x.reason for x in t.attempts]==['initial','fallback']


async def test_t5_same_origin_credential_cannot_bypass_wait():
    a=SequenceProvider(ProviderCallError(code='provider_rate_limited',retry_after_seconds=15));b=SequenceProvider(ok())
    definitions={
        'mock-main':ProviderConfig(adapter='deepseek',api_protocol='chat_completions',base_url='https://example.test/v1',credential_ref='fake-key'),
        'mock-backup':ProviderConfig(adapter='openai_compatible',api_protocol='responses',base_url='https://example.test:443/v2',credential_ref='fake-key')}
    svc,t,tr=setup(a,b,providers=definitions)
    with pytest.raises(ProviderCallError) as e:await svc._complete(text_request(timeout_seconds=1),tr)
    assert e.value.code=='provider_rate_limited' and len(a.calls)==1 and not b.calls


async def test_t1_real_normal_generation_over_ten_seconds_is_allowed():
    async def delayed():
        await asyncio.sleep(10.05)
        return ok('complete after ten seconds')
    a=SequenceProvider(delayed);svc,t,tr=setup(a)
    start=monotonic();data,_=await svc._complete(text_request(timeout_seconds=15),tr)
    assert monotonic()-start>=10 and data.text=='complete after ten seconds' and len(a.calls)==1


@pytest.mark.parametrize('case,expected',[('total','request_timeout'),('same','request_timeout'),('backup','provider_timeout')])
async def test_t1_attempt_limit_and_request_limit_have_distinct_errors(case,expected):
    async def blocked():
        await asyncio.sleep(2)
        return ok()
    if case=='backup':
        a=SequenceProvider(connection(),connection());b=SequenceProvider(blocked)
        svc,t,tr=setup(a,b,{'general-backup':{'max_timeout_seconds':1,'default_timeout_seconds':1}})
        request=text_request(timeout_seconds=2)
    else:
        a=SequenceProvider(blocked);b=SequenceProvider(ok())
        changes={'general-primary':{'max_timeout_seconds':1,'default_timeout_seconds':1}} if case=='same' else None
        svc,t,tr=setup(a,b,changes);request=text_request(timeout_seconds=1)
    with pytest.raises((GatewayError,ProviderCallError)) as e:await svc._complete(request,tr)
    assert e.value.code==expected
    assert len(a.calls)==(2 if case=='backup' else 1)
    assert len(b.calls)==int(case=='backup')


async def test_t2_slow_output_validation_consumes_total_deadline(monkeypatch):
    import time
    import services.gateway as module
    original=module.validate_output
    def slow_validation(*args):
        time.sleep(1.05)
        return original(*args)
    monkeypatch.setattr(module,'validate_output',slow_validation)
    a=SequenceProvider(ok(TEXT));svc,t,tr=setup(a)
    with pytest.raises(GatewayError) as e:
        await svc._complete(LLMRequest.model_validate(dict(payload(),timeout_seconds=1)),tr)
    assert e.value.code=='request_timeout' and len(a.calls)==1


async def test_t4_actual_next_send_observes_retry_after():
    a=SequenceProvider(ProviderCallError(code='provider_rate_limited',retry_after_seconds=.05),ok())
    svc,t,tr=setup(a);await svc._complete(text_request(),tr)
    assert len(a.calls)==2 and a.calls[1][0]-a.calls[0][0]>=.05


async def test_j6_early_fallback_leaves_one_shared_repair_slot():
    a=SequenceProvider(ProviderCallError(code='provider_rate_limited',retry_after_seconds=15))
    b=SequenceProvider(ok('bad JSON'),ok(TEXT));svc,t,tr=setup(a,b)
    data,_=await svc._complete(LLMRequest.model_validate(dict(payload(),timeout_seconds=1)),tr)
    assert data.parsed==PARSED and len(a.calls)==1 and len(b.calls)==2
    assert [x.reason for x in t.attempts]==['initial','fallback','format_repair']


@pytest.mark.parametrize('overflow',[0,1])
async def test_j4_repair_exact_context_boundary(overflow):
    from contracts import Message
    from services.validation import prepare_schema
    a=SequenceProvider(ok('bad JSON'),ok(TEXT));svc,t,tr=setup(a)
    request=LLMRequest.model_validate(payload());schema=prepare_schema(request.response_schema)
    contract='{"mode":"json","schema":'+schema.canonical+'}'
    original=build_messages(request.prompt,request.messages,svc.prompt_registry,output_contract=contract)
    repair=original+[Message(role='assistant',content='bad JSON'),Message(role='user',content=svc._repair_feedback('invalid_json'))]
    model=svc.model_registry.get_endpoint('general-primary')
    count=svc.token_budget.count(model,repair,response_schema=schema.schema).input_tokens
    svc,t,tr=setup(a,changes={'general-primary':{'default_output_tokens':128,'context_window_tokens':count+128+model.safety_margin_tokens-overflow}})
    if overflow:
        with pytest.raises(GatewayError) as e:await svc._complete(request,tr)
        assert e.value.code=='invalid_json' and len(a.calls)==1
    else:
        data,_=await svc._complete(request,tr)
        assert data.parsed==PARSED and len(a.calls)==2


async def test_g3_incompatible_backup_does_not_lower_grown_budget():
    a=SequenceProvider(ProviderResult(text='partial',finish_reason='length'),connection());b=SequenceProvider(ok())
    svc,t,tr=setup(a,b,{'general-backup':{'max_output_tokens':64,'default_output_tokens':64}})
    with pytest.raises(ProviderCallError) as e:await svc._complete(text_request(initial_output_tokens=32),tr)
    assert e.value.code=='provider_connection_error' and not b.calls
    assert [c[3]['max_output_tokens'] for c in a.calls]==[32,128]


async def test_r5_responses_to_chat_fallback_builds_chat_schema_context():
    a=SequenceProvider(connection(),connection());b=SequenceProvider(ok(TEXT))
    definitions={'mock-main':ProviderConfig(adapter='mock',api_protocol='responses'),
                 'mock-backup':ProviderConfig(adapter='mock',api_protocol='chat_completions')}
    changes={'general-primary':{'structured_output_mode':'json_schema'},
             'general-backup':{'structured_output_mode':'json_object'}}
    # Construct the reversed protocol registry directly: the fixture validates its
    # default protocol pairing before accepting provider overrides.
    base=registry();items={k:v.model_copy(update=changes[k]) for k,v in base.endpoints.items()}
    items['general-primary']=items['general-primary'].model_copy(update={'fallback_models':('general-backup',)})
    r=ModelRegistry(items,definitions,recovery=RecoveryPolicy(base_delay_seconds=0,max_delay_seconds=0), concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))
    traces=TraceStore();svc=GatewayService({'mock-main':a,'mock-backup':b},traces,PromptRegistry(DEFAULT_TEMPLATES),r,TokenBudget.from_models(r.endpoints.values()), limiter=ConcurrencyLimiter((r).concurrency))
    tr=traces.start_request('00000000-0000-0000-0000-000000000078')
    tr.agent_id = 'unit-agent'
    data,_=await svc._complete(LLMRequest.model_validate(payload()),tr)
    assert data.parsed==PARSED and len(a.calls)==2 and len(b.calls)==1
    assert '"schema":' not in a.calls[0][2][-1].content and '"schema":' in b.calls[0][2][-1].content
    assert a.calls[0][2][0]==b.calls[0][2][0]
    for attempt,call in [(traces.attempts[0],a.calls[0]),(traces.attempts[-1],b.calls[0])]:
        assert attempt.input_token_count==svc.token_budget.count(call[1],call[2],response_schema=call[3]['response_schema']).input_tokens


async def test_j5_repair_preserves_decimal_const_without_rounding():
    exact=Decimal('1.0000000000000001')
    schema={'type':'object','properties':{'value':{'type':'number','const':exact}},'required':['value'],'additionalProperties':False}
    request=LLMRequest.model_validate(dict(payload(),response_schema=schema))
    a=SequenceProvider(ok('{"value":1.0}'),ok('{"value":1.0000000000000001}'))
    svc,t,tr=setup(a);data,_=await svc._complete(request,tr)
    assert data.parsed['value']==exact and len(a.calls)==2
    assert a.calls[1][3]['response_schema']['properties']['value']['const']==exact
    assert [x.error_code for x in t.attempts]==['schema_validation_failed',None]


async def test_t2_multiple_calls_share_one_deadline():
    async def slow_failure():
        await asyncio.sleep(.4)
        raise connection()
    async def too_slow_success():
        await asyncio.sleep(.5)
        return ok()
    a=SequenceProvider(slow_failure,slow_failure);b=SequenceProvider(too_slow_success)
    svc,t,tr=setup(a,b);begin=monotonic()
    with pytest.raises(GatewayError) as e:await svc._complete(text_request(timeout_seconds=1),tr)
    assert e.value.code=='request_timeout' and len(a.calls)==2 and len(b.calls)==1
    assert b.calls[0][3]['timeout_seconds']<.3
    assert monotonic()-begin<1.5
