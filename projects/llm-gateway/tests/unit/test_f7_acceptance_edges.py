"""Supplement confirmed F7 parameter branches; post-implementation verification."""
import asyncio
from copy import deepcopy
from time import monotonic

import pytest

from contracts import LLMRequest, StreamRequest
from errors import ProviderCallError
from f4_cases import payload, TEXT, PARSED
from providers.base import ProviderResult
from services.models import RecoveryPolicy
from services.trace import TraceStore
from test_f7_ordinary_edges import SequenceProvider, setup, text_request, ok, connection


@pytest.mark.parametrize('code,metadata',[
    ('provider_connection_error',{}),('provider_timeout',{}),('provider_rate_limited',{}),
    *[('provider_unavailable',{'upstream_http_status':n}) for n in [500,502,503,504]],
    ('provider_unavailable',{'transient':True}),
    ('provider_unavailable',{'finish_reason':'aborted'}),
    ('provider_unavailable',{'finish_reason':'insufficient_system_resource'}),
])
async def test_r1_each_transient_branch_retries_then_succeeds(code,metadata):
    provider=SequenceProvider(ProviderCallError(code=code,**metadata),ok())
    svc,traces,trace=setup(provider)
    result,_=await svc._complete(text_request(timeout_seconds=1),trace)
    assert result.text=='订单A123需要人工确认' and len(provider.calls)==2
    assert provider.calls[0][2]==provider.calls[1][2]
    assert [a.status for a in traces.attempts]==['failed','completed']
    assert [a.error_code for a in traces.attempts]==[code,None]
    assert [a.model for a in traces.attempts]==['general-primary','general-primary']


async def test_r4_streaming_incapable_backup_is_skipped_without_attempt_or_call():
    from providers.mock import MockProvider
    from providers.base import ProviderStreamEvent
    primary=MockProvider(stream_events=[ProviderStreamEvent(status='failed',error_code='provider_connection_error')])
    backup=MockProvider()
    svc,traces,trace=setup(primary,backup,{'general-backup':{'supports_streaming':False}})
    body=text_request().model_dump(exclude_none=True)
    execution=svc.prepare_stream(StreamRequest.model_validate(body),trace)
    assert [p.model_name for p,_ in execution.candidates]==['general-primary']
    assert execution.snapshot['skipped_candidates']==[{'model':'general-backup','code':'unsupported_capability'}]
    assert not primary.stream_calls and not backup.stream_calls and not traces.attempts
    events=[]
    async def start(): pass
    async def send(name,data):events.append((name,data))
    await execution.run(start,send,asyncio.Event())
    assert len(primary.stream_calls)==2 and not backup.stream_calls
    assert len(traces.attempts)==2 and all(a.model=='general-primary' for a in traces.attempts)
    assert events[-1][0]=='response.failed' and events[-1][1]['code']=='provider_connection_error'


@pytest.mark.parametrize('variant',['missing_order_id','unknown_field','wrong_enum'])
async def test_j1_each_schema_boundary_repairs_successfully(variant):
    if variant=='missing_order_id':
        import json
        candidate=deepcopy(PARSED);del candidate['order_id'];bad=json.dumps(candidate,ensure_ascii=False)
        request=LLMRequest.model_validate(payload());good=TEXT;expected=PARSED
    elif variant=='unknown_field':
        import json
        candidate=dict(PARSED,unapproved_refund=True);bad=json.dumps(candidate,ensure_ascii=False)
        request=LLMRequest.model_validate(payload());good=TEXT;expected=PARSED
    else:
        schema={'type':'object','properties':{'order_id':{'type':'string'},'action':{'type':'string','enum':['draft_ticket','ask_order']}},
                'required':['order_id','action'],'additionalProperties':False}
        request=LLMRequest.model_validate(dict(payload(),response_schema=schema))
        bad='{"order_id":"A123","action":"execute_refund"}'
        good='{"order_id":"A123","action":"draft_ticket"}';expected={'order_id':'A123','action':'draft_ticket'}
    primary=SequenceProvider(ok(bad),ok(good));backup=SequenceProvider(ok(good))
    svc,traces,trace=setup(primary,backup)
    result,_=await svc._complete(request,trace)
    assert result.parsed==expected and len(primary.calls)==2 and not backup.calls
    assert [a.error_code for a in traces.attempts]==['schema_validation_failed',None]
    assert [a.reason for a in traces.attempts]==['initial','format_repair']
    assert primary.calls[0][2][0]==primary.calls[1][2][0]


@pytest.mark.parametrize('path',['growth','repair','fallback'])
async def test_t3_non_network_recovery_does_not_wait_local_backoff(path,monkeypatch):
    import services.recovery as recovery
    monkeypatch.setattr(recovery,'random',lambda:1.0)
    # Ten-second deterministic local backoff cannot fit this one-second request.
    # A successful second call therefore proves these paths do not apply it.
    policy=RecoveryPolicy(max_attempts_per_endpoint=1 if path=='fallback' else 2,
                          base_delay_seconds=10,max_delay_seconds=10)
    request=text_request(timeout_seconds=1)
    backup=SequenceProvider()
    if path=='growth':
        primary=SequenceProvider(ProviderResult(text='partial',finish_reason='length'),ok('whole answer'))
        request=text_request(timeout_seconds=1,initial_output_tokens=32)
    elif path=='repair':
        primary=SequenceProvider(ok('not JSON'),ok(TEXT))
        request=LLMRequest.model_validate(dict(payload(),timeout_seconds=1))
    else:
        primary=SequenceProvider(connection());backup=SequenceProvider(ok('whole answer'))
    svc,traces,trace=setup(primary,backup,policy=policy)
    result,_=await svc._complete(request,trace)
    assert result.text==(TEXT if path=='repair' else 'whole answer')
    assert len(primary.calls)+len(backup.calls)==2
    assert traces.attempts[-1].reason=={'growth':'output_growth','repair':'format_repair','fallback':'fallback'}[path]
    if path=='growth':assert [c[3]['max_output_tokens'] for c in primary.calls]==[32,128]


async def test_t5_same_registry_concurrent_requests_have_separate_waits():
    first_failed=asyncio.Event()
    class PerRequestProvider:
        def __init__(self): self.calls=[];self.attempts_a=0
        async def complete(self,model,messages,**options):
            content=messages[-1].content
            self.calls.append((content,monotonic()))
            if content=='订单A123':
                self.attempts_a+=1
                if self.attempts_a==1:
                    first_failed.set()
                    raise ProviderCallError(code='provider_rate_limited',retry_after_seconds=.3)
            return ok(content)
    provider=PerRequestProvider()
    svc,traces,trace_a=setup(provider,changes={'general-primary':{'fallback_models':[]}})
    a=text_request(timeout_seconds=2).model_copy(update={'messages':[
        text_request().messages[0].model_copy(update={'content':'订单A123'})]})
    b=a.model_copy(update={'messages':[a.messages[0].model_copy(update={'content':'订单B456'})]})
    task=asyncio.create_task(svc._complete(a,trace_a))
    try:
        await asyncio.wait_for(first_failed.wait(),1)
        trace_b=traces.start_request('00000000-0000-0000-0000-000000000088')
        trace_b.agent_id='unit-agent'
        data_b,_=await svc._complete(b,trace_b)
        assert data_b.text=='订单B456'
        assert not task.done(), 'B inherited A retry wait instead of proceeding independently'
        data_a,_=await asyncio.wait_for(task,2)
        assert data_a.text=='订单A123'
    finally:
        if not task.done():task.cancel()
        await asyncio.gather(task,return_exceptions=True)
    a_times=[when for content,when in provider.calls if content=='订单A123']
    b_times=[when for content,when in provider.calls if content=='订单B456']
    assert len(a_times)==2 and len(b_times)==1
    assert a_times[1]-a_times[0]>=.3 and b_times[0]<a_times[1]
    assert trace_a.attempts==2 and trace_b.attempts==1
