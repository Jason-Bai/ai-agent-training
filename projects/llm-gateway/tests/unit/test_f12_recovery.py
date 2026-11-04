"""F12 T13–T15/T25: actual endpoint attempts and fixed probe behavior."""
from uuid import uuid4

import pytest

from errors import GatewayError, ProviderCallError
from f12_cases import registry, request, service, ScriptedProvider
from providers.base import ProviderResult
from services.prompts import fingerprint_messages


def transient():return ProviderCallError(code='provider_connection_error')


async def test_recovery_calls_two_primary_then_actual_backup_without_replacing_route():
    a=ScriptedProvider(transient(),transient());b=ScriptedProvider(ProviderResult(text='backup'))
    svc,trace,a,b,traces=service(primary=a,backup=b)
    result=await svc.complete(request(),trace)
    assert result.text=='backup'
    assert len(a.calls)==2 and len(b.calls)==1
    assert [attempt.model for attempt in traces.attempts]==['chat-a','chat-a','responses-b']
    assert [attempt.reason for attempt in traces.attempts]==['initial','network_retry','fallback']
    assert trace.routing['selected_endpoint']=='chat-a'
    assert svc.limiter.snapshot()['active_requests']==0


@pytest.mark.parametrize('code',['provider_auth_error','provider_quota_exceeded',
                                'provider_rejected_request','provider_refused'])
async def test_permanent_failure_cannot_escape_to_other_endpoint(code):
    a=ScriptedProvider(ProviderCallError(code=code))
    svc,trace,a,b,traces=service(primary=a)
    with pytest.raises(ProviderCallError) as caught:await svc.complete(request(),trace)
    assert caught.value.code==code
    assert len(a.calls)==1 and not b.calls and len(traces.attempts)==1


async def test_structured_fallback_uses_its_own_prompt_count_and_constraints():
    a=ScriptedProvider(transient(),transient())
    b=ScriptedProvider(ProviderResult(text='{"order_id":"A123"}'))
    svc,trace,a,b,traces=service(primary=a,backup=b)
    result=await svc.complete(request(structured=True),trace)
    assert result.parsed=={'order_id':'A123'}
    assert a.calls[0][1][0]==b.calls[0][1][0]
    assert '"schema":' in a.calls[0][1][-1].content
    assert '"schema":' not in b.calls[0][1][-1].content
    assert b.calls[0][2]['response_schema']['type']=='object'
    for attempt,calls in [(traces.attempts[0],a.calls),(traces.attempts[-1],b.calls)]:
        assert attempt.prompt_fingerprint==fingerprint_messages(calls[0][1])
    assert trace.prompt_fingerprint==traces.attempts[0].prompt_fingerprint
    assert traces.attempts[0].prompt_fingerprint!=traces.attempts[-1].prompt_fingerprint


async def test_length_grows_only_to_group_authorization_on_same_endpoint():
    a=ScriptedProvider(ProviderResult(text='partial',finish_reason='length'),ProviderResult(text='done'))
    svc,trace,a,b,traces=service(primary=a)
    result=await svc.complete(request(initial_output_tokens=16,max_output_tokens=128),trace)
    assert result.text=='done'
    assert [c[2]['max_output_tokens'] for c in a.calls]==[16,128]
    assert not b.calls
    assert [t.model for t in traces.attempts]==['chat-a','chat-a']


async def test_invalid_json_repairs_once_without_switching_endpoint():
    a=ScriptedProvider(ProviderResult(text='{'),ProviderResult(text='{"order_id":"A123"}'))
    svc,trace,a,b,traces=service(primary=a)
    result=await svc.complete(request(structured=True),trace)
    assert result.parsed=={'order_id':'A123'} and not b.calls
    assert len(a.calls[1][1])==len(a.calls[0][1])+2
    assert [t.reason for t in traces.attempts]==['initial','format_repair']


@pytest.mark.parametrize('disabled',[False,True])
async def test_probe_calls_first_enabled_once_and_never_uses_fallback(disabled):
    a=ScriptedProvider(transient());b=ScriptedProvider(transient())
    svc,trace,a,b,traces=service(registry({'chat-a':{'enabled':not disabled}}),a,b)
    trace.operation='provider_probe'
    with pytest.raises(ProviderCallError):await svc.probe('general-primary',trace)
    assert len(a.calls)==int(not disabled) and len(b.calls)==int(disabled)
    assert len(traces.attempts)==1
    assert trace.routing['strategy']=='probe_first_enabled'
    assert trace.routing['selected_endpoint']==('responses-b' if disabled else 'chat-a')
    assert svc.limiter.snapshot()['active_requests']==0


async def test_shared_endpoint_requests_keep_recovery_counts_isolated():
    a=ScriptedProvider(transient(),transient(),ProviderResult(text='second'))
    b=ScriptedProvider(ProviderResult(text='first'))
    svc,trace,a,b,traces=service(primary=a,backup=b)
    assert (await svc.complete(request(),trace)).text=='first'
    another=traces.start_request(str(uuid4()));another.agent_id='f12-other'
    assert (await svc.complete(request(),another)).text=='second'
    assert len(a.calls)==3 and len(b.calls)==1
    assert [t.model for t in traces.attempts if t.request_id==another.request_id]==['chat-a']


async def test_probe_cannot_skip_incompatible_first_enabled_for_healthy_backup():
    svc,trace,a,b,traces=service(registry({'chat-a':{'max_output_tokens':1}}))
    trace.operation='provider_probe'
    with pytest.raises(GatewayError) as caught:await svc.probe('general-primary',trace)
    assert caught.value.code=='invalid_output_budget'
    assert not a.calls and not b.calls and not traces.attempts


@pytest.mark.parametrize('endpoint_timeout',[9,10,11])
@pytest.mark.parametrize('disabled_prefix',[False,True])
async def test_probe_fixed_timeout_is_not_reduced_to_endpoint_cap(endpoint_timeout,disabled_prefix):
    selected='responses-b' if disabled_prefix else 'chat-a'
    changes={selected:{'max_timeout_seconds':endpoint_timeout}}
    if disabled_prefix:changes['chat-a']={'enabled':False}
    svc,trace,a,b,traces=service(registry(changes))
    trace.operation='provider_probe'
    if endpoint_timeout<10:
        with pytest.raises(GatewayError) as caught:await svc.probe('general-primary',trace)
        assert caught.value.code=='invalid_timeout'
        assert not a.calls and not b.calls and not traces.attempts
    else:
        await svc.probe('general-primary',trace)
        calls=b.calls if disabled_prefix else a.calls
        assert len(calls)==1 and 0<calls[0][2]['timeout_seconds']<=10
        assert not (a.calls if disabled_prefix else b.calls)
        assert len(traces.attempts)==1
    assert svc.limiter.snapshot()['active_requests']==0


async def test_attempt_timeouts_respect_endpoint_cap_and_request_remaining_budget():
    a=ScriptedProvider(transient(),transient());b=ScriptedProvider(ProviderResult(text='backup'))
    svc,trace,a,b,_=service(registry({'chat-a':{'max_timeout_seconds':1}}),a,b)
    assert (await svc.complete(request(timeout_seconds=7),trace)).text=='backup'
    assert all(0<c[2]['timeout_seconds']<=1 for c in a.calls)
    assert 0<b.calls[0][2]['timeout_seconds']<=7
