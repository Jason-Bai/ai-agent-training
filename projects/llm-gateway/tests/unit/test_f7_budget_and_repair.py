from registry_helpers import ModelRegistry
"""F7 A/J/G: service boundaries and actual Provider calls."""
from services.models import ConcurrencyPolicy
from services.concurrency import ConcurrencyLimiter
import pytest
from pydantic import ValidationError
from contracts import LLMRequest, StreamRequest


def body(**kw):
    return dict(model='general-primary', prompt={'name':'basic_text','version':'1.0.0'},
                messages=[{'role':'user','content':'订单A123商品破损，请分析'}], **kw)


@pytest.mark.parametrize('contract',[LLMRequest,StreamRequest])
def test_f7_initial_is_optional_and_strict_positive(contract):
    assert contract.model_validate(body()).initial_output_tokens is None
    assert contract.model_validate(body(initial_output_tokens=1)).initial_output_tokens == 1
    for value in (None,True,False,'1',1.5,0,-1):
        with pytest.raises(ValidationError):
            contract.model_validate(body(initial_output_tokens=value))

from errors import ProviderCallError, GatewayError
from providers.base import ProviderResult
from services.gateway import GatewayService
from services.prompts import PromptRegistry, DEFAULT_TEMPLATES
from services.token_budget import TokenBudget
from services.trace import TraceStore
from f4_cases import registry, payload, TEXT, PARSED


class Sequence:
    def __init__(self, *items):
        self.items=list(items)
        self.calls=[]
    async def complete(self, model, messages, **options):
        self.calls.append((model, messages, options))
        item=self.items.pop(0)
        if isinstance(item,BaseException): raise item
        return item


def service(provider, reg=None):
    from dataclasses import replace
    from services.models import RecoveryPolicy
    reg=reg or replace(registry(), recovery=RecoveryPolicy())
    traces=TraceStore()
    svc=GatewayService({'mock-main':provider,'mock-backup':provider}, traces,
                       PromptRegistry(DEFAULT_TEMPLATES),reg,TokenBudget.from_models(reg.endpoints.values()), limiter=ConcurrencyLimiter((reg).concurrency))
    trace = traces.start_request('00000000-0000-0000-0000-000000000007')
    trace.agent_id = 'unit-agent'
    return svc,traces,trace


async def test_f7_connection_failure_retries_same_input_then_success():
    p=Sequence(ProviderCallError(code='provider_connection_error'),ProviderResult(text='ok'))
    svc,traces,trace=service(p)
    data,_=await svc._complete(LLMRequest.model_validate(body()),trace)
    assert data.text=='ok' and len(p.calls)==2
    assert p.calls[0][1]==p.calls[1][1]
    assert [a.status for a in traces.attempts]==['failed','completed']


async def test_f7_truncation_grows_directly_to_authorization():
    p=Sequence(ProviderResult(text='partial',finish_reason='length'),ProviderResult(text='complete'))
    svc,traces,trace=service(p)
    data,_=await svc._complete(LLMRequest.model_validate(body(initial_output_tokens=32,max_output_tokens=128)),trace)
    assert data.text=='complete'
    assert [c[2]['max_output_tokens'] for c in p.calls]==[32,128]


async def test_f7_repair_keeps_system_and_schema_and_adds_untrusted_candidate():
    p=Sequence(ProviderResult(text='not JSON'),ProviderResult(text=TEXT))
    svc,traces,trace=service(p)
    data,_=await svc._complete(LLMRequest.model_validate(payload()),trace)
    assert data.parsed==PARSED and len(p.calls)==2
    first,second=p.calls[0][1],p.calls[1][1]
    assert second[:-2]==first
    assert second[-2].role=='assistant' and second[-2].content=='not JSON'
    assert second[-1].role=='user' and 'invalid_json' in second[-1].content
    assert p.calls[0][2]['response_schema']==p.calls[1][2]['response_schema']


@pytest.mark.parametrize('value',[None,32,128])
async def test_f7_initial_budget_and_authorized_ceiling(value):
    p=Sequence(ProviderResult(text='ok'))
    svc,_,trace=service(p)
    kw={'max_output_tokens':128}
    if value is not None: kw['initial_output_tokens']=value
    await svc._complete(LLMRequest.model_validate(body(**kw)),trace)
    assert p.calls[0][2]['max_output_tokens']==(value or 128)


async def test_f7_initial_above_ceiling_rejected_before_attempt():
    p=Sequence(ProviderResult(text='ok'))
    svc,traces,trace=service(p)
    with pytest.raises(GatewayError) as e:
        await svc._complete(LLMRequest.model_validate(body(initial_output_tokens=129,max_output_tokens=128)),trace)
    assert e.value.code=='invalid_output_budget'
    assert not p.calls and not traces.attempts

async def test_f7_exhaustion_retains_diagnostic_stop_reason():
    p=Sequence(ProviderCallError(code='provider_connection_error'),ProviderCallError(code='provider_connection_error'))
    svc,_,trace=service(p)
    with pytest.raises(ProviderCallError):
        await svc._complete(LLMRequest.model_validate(body()),trace)
    assert trace._recovery_stop_reason

async def test_f7_candidates_never_follow_backup_recursively():
    from services.models import RecoveryPolicy
    base=registry();models=dict(base.endpoints)
    models['general-primary']=models['general-primary'].model_copy(update={'fallback_models':('general-backup',)})
    models['general-backup']=models['general-backup'].model_copy(update={'fallback_models':('third',)})
    models['third']=models['general-backup'].model_copy(update={'fallback_models':()})
    reg=ModelRegistry(models,base.providers,recovery=RecoveryPolicy(max_attempts=3,max_attempts_per_endpoint=1,base_delay_seconds=0,max_delay_seconds=0), concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))
    p=Sequence(ProviderCallError(code='provider_connection_error'),ProviderCallError(code='provider_connection_error'),ProviderResult(text='must not run'))
    svc,traces,trace=service(p,reg)
    with pytest.raises(ProviderCallError):await svc._complete(LLMRequest.model_validate(body()),trace)
    assert len(p.calls)==2 and [a.model for a in traces.attempts]==['general-primary','general-backup']


@pytest.mark.parametrize('code',['provider_connection_error','provider_rate_limited','output_truncated'])
async def test_f7_health_probe_never_recovers(code):
    p=Sequence(ProviderCallError(code=code),ProviderResult(text='must not run'))
    svc,traces,trace=service(p);trace.operation='provider_probe'
    with pytest.raises(ProviderCallError):await svc.probe('general-primary',trace)
    assert len(p.calls)==len(traces.attempts)==1
