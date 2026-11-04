"""Bailian wire contract: actual SDK transport against an owned HTTP server."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from contracts import Message
from providers.bailian import BailianProvider
from providers.openai_compatible import OpenAICompatibleProvider
from providers.factory import create_provider
from errors import ProviderCallError
from test_providers import server
from test_f5_providers import stream_server,frame,response

MODELS=('qwen3.8-flash','qwen3.7-plus','qwen3.8-max')


def completed(model):
    value=response();value['model']=model
    value['usage']['input_tokens_details']={'cached_tokens':4}
    return value


async def test_bailian_schema_uses_documented_nested_contract(server):
    server.body=completed('qwen3.8-flash')
    provider=BailianProvider(server.url+'/compatible-mode/v1','FAKE-F13-KEY')
    try:
        await provider.complete(SimpleNamespace(model_id='qwen3.8-flash'),
            [Message(role='system',content='Fixed'),Message(role='user',content='JSON')],
            max_output_tokens=128,timeout_seconds=2,
            response_schema={'type':'object','properties':{'x':{'type':'number','minimum':Decimal('1.0000000000000001')}},
                             'required':['x'],'additionalProperties':False})
    finally:await provider.aclose()
    sent=server.calls[0]
    assert sent['path']=='/compatible-mode/v1/responses'
    assert sent['body']['text']['format']['json_schema']['strict'] is True
    assert '1.0000000000000001' in sent['raw_body']


@pytest.mark.parametrize('model',MODELS)
@pytest.mark.parametrize('structured',[False,True])
async def test_each_bailian_model_preserves_request_and_usage(server,model,structured):
    server.body=completed(model)
    provider=BailianProvider(server.url+'/compatible-mode/v1','FAKE-F13-KEY')
    messages=[Message(role='system',content='Fixed职责'),Message(role='user',content='订单 A123')]
    schema={'type':'object','properties':{'order_id':{'type':'string'}},
            'required':['order_id'],'additionalProperties':False}
    try:
        result=await provider.complete(SimpleNamespace(model_id=model),messages,
            max_output_tokens=128,timeout_seconds=2,response_schema=schema if structured else None)
    finally:await provider.aclose()
    assert result.actual_model==model and result.text=='订单 A123'
    assert result.usage.total_tokens==15 and result.cached_input_tokens==4
    assert result.billing_observed_at is not None
    assert len(server.calls)==1
    call=server.calls[0];body=call['body']
    assert call['path']=='/compatible-mode/v1/responses'
    assert call['headers']['authorization']=='Bearer FAKE-F13-KEY'
    assert body['model']==model and body['input']==[m.model_dump() for m in messages]
    assert body['reasoning']=={'effort':'none'} and body['max_output_tokens']==128
    assert body['stream'] is False and body['store'] is False
    assert not {'tools','previous_response_id','enable_thinking'} & body.keys()
    if structured:assert body['text']['format']['json_schema']=={'name':'gateway_response','schema':schema,'strict':True}
    else:assert 'text' not in body


@pytest.mark.parametrize('status,code',[(401,'provider_auth_error'),(402,'provider_quota_exceeded'),
    (429,'provider_rate_limited'),(503,'provider_unavailable'),(400,'provider_rejected_request')])
async def test_bailian_http_errors_are_single_calls(server,status,code):
    server.status=status
    provider=BailianProvider(server.url,'FAKE-F13-KEY')
    try:
        with pytest.raises(ProviderCallError) as caught:
            await provider.complete(SimpleNamespace(model_id=MODELS[0]),[Message(role='user',content='Q')],
                max_output_tokens=128,timeout_seconds=2)
        assert caught.value.code==code
    finally:await provider.aclose()
    assert len(server.calls)==1


@pytest.mark.parametrize('change',['model','usage','unknown-output','incomplete','failed','cache-conflict'])
async def test_bailian_terminal_validation_keeps_real_evidence(server,change):
    body=completed(MODELS[0])
    if change=='model':body['model']=None
    elif change=='usage':body['usage']['total_tokens']=True
    elif change=='unknown-output':body['output'].append({'type':'function_call','arguments':'{}'})
    elif change=='incomplete':body.update(status='incomplete',incomplete_details={'reason':'max_output_tokens'})
    elif change=='failed':body.update(status='failed',error={'code':'server_error','message':'private'})
    else:body['usage']['input_tokens_details']['cached_tokens']=11
    server.body=body;provider=BailianProvider(server.url,'FAKE-F13-KEY')
    try:
        if change in ('incomplete','cache-conflict'):
            result=await provider.complete(SimpleNamespace(model_id=MODELS[0]),[Message(role='user',content='Q')],
                max_output_tokens=128,timeout_seconds=2)
            if change=='incomplete':assert result.finish_reason=='length' and result.usage.total_tokens==15
            else:assert result.cached_input_tokens is None and result.usage.total_tokens==15
        else:
            with pytest.raises(ProviderCallError) as caught:
                await provider.complete(SimpleNamespace(model_id=MODELS[0]),[Message(role='user',content='Q')],
                    max_output_tokens=128,timeout_seconds=2)
            assert caught.value.code==('provider_unavailable' if change=='failed' else 'provider_invalid_response')
    finally:await provider.aclose()
    assert len(server.calls)==1


async def test_factory_selects_bailian_and_owned_client_closes():
    provider=create_provider(SimpleNamespace(adapter='bailian',base_url='http://127.0.0.1:9'),'FAKE-F13-KEY')
    assert isinstance(provider,BailianProvider)
    await provider.aclose()
    assert provider.client.is_closed


async def test_bailian_queued_and_unknown_early_model_reach_real_terminal(stream_server):
    terminal=response();terminal['model']='qwen3.8-flash'
    stream_server.data=(frame({'type':'response.created','response':{'object':'response','status':'queued','model':''}})
        +frame({'type':'response.in_progress','response':{'object':'response','status':'in_progress'}})
        +frame({'type':'response.output_text.delta','delta':'订单 A123'})
        +frame({'type':'response.completed','response':terminal}))
    provider=BailianProvider(stream_server.url,'FAKE-F13-KEY')
    try:
        events=[e async for e in provider.stream(SimpleNamespace(model_id='qwen3.8-flash'),
            [Message(role='user',content='Question')],max_output_tokens=128,timeout_seconds=2)]
    finally:await provider.aclose()
    assert ''.join(e.delta for e in events)=='订单 A123'
    assert events[-1].status=='completed' and events[-1].actual_model=='qwen3.8-flash'
    assert events[-1].usage.total_tokens==15


@pytest.mark.parametrize('model',MODELS)
async def test_each_bailian_model_streams_typed_deltas_and_final_usage(stream_server,model):
    stream_server.split=True
    stream_server.data=(frame({'type':'response.created','response':{'object':'response','status':'queued'}})
        +frame({'type':'response.output_text.delta','delta':'订单'})
        +frame({'type':'response.output_text.delta','delta':' A123'})
        +frame({'type':'response.completed','response':completed(model)}))
    provider=BailianProvider(stream_server.url+'/compatible-mode/v1','FAKE-F13-KEY')
    try:events=[e async for e in provider.stream(SimpleNamespace(model_id=model),
        [Message(role='user',content='Q')],max_output_tokens=128,timeout_seconds=2)]
    finally:await provider.aclose()
    assert [e.delta for e in events if e.delta]==['订单',' A123']
    final=events[-1]
    assert final.status=='completed' and final.actual_model==model and final.usage.total_tokens==15
    assert final.cached_input_tokens==4 and final.upstream_terminal_received
    assert len(stream_server.calls)==1
    assert stream_server.calls[0]['path']=='/compatible-mode/v1/responses'
    assert stream_server.calls[0]['body']['reasoning']=={'effort':'none'}


@pytest.mark.parametrize('kind',['unknown','invalid-status','invalid-model','invalid-terminal','missing-tail'])
async def test_bailian_invalid_streams_fail_without_faking_success(stream_server,kind):
    queued={'type':'response.created','response':{'object':'response','status':'queued'}}
    tail=frame({'type':'unknown.event'})
    if kind=='invalid-status':queued['response']['status']='completed'
    elif kind=='invalid-model':queued['response']['model']=123
    elif kind=='invalid-terminal':
        body=completed(MODELS[0]);body['model']=''
        tail=frame({'type':'response.completed','response':body})
    elif kind=='missing-tail':tail=b''
    stream_server.data=frame(queued)+tail
    provider=BailianProvider(stream_server.url,'FAKE-F13-KEY')
    try:events=[e async for e in provider.stream(SimpleNamespace(model_id=MODELS[0]),
        [Message(role='user',content='Q')],max_output_tokens=128,timeout_seconds=2)]
    finally:await provider.aclose()
    assert events[-1].status=='failed' and events[-1].error_code=='provider_invalid_response'
    assert events[-1].actual_model is None


async def test_bailian_pending_profile_does_not_relax_openai_profile(stream_server):
    stream_server.data=frame({'type':'response.created','response':{'object':'response','status':'queued'}})
    provider=OpenAICompatibleProvider(stream_server.url,'FAKE-F13-KEY')
    try:events=[e async for e in provider.stream(SimpleNamespace(model_id=MODELS[0]),
        [Message(role='user',content='Q')],max_output_tokens=128,timeout_seconds=2)]
    finally:await provider.aclose()
    assert events[-1].status=='failed' and events[-1].upstream_status is None


@pytest.mark.parametrize('status,code',[(401,'provider_auth_error'),(429,'provider_rate_limited'),
                                     (503,'provider_unavailable')])
async def test_bailian_stream_http_errors_have_no_sdk_retry(stream_server,status,code):
    stream_server.status=status;stream_server.data=b'{}'
    provider=BailianProvider(stream_server.url,'FAKE-F13-KEY')
    try:events=[e async for e in provider.stream(SimpleNamespace(model_id=MODELS[0]),
        [Message(role='user',content='Q')],max_output_tokens=128,timeout_seconds=2)]
    finally:await provider.aclose()
    assert len(events)==1 and events[0].error_code==code
    assert len(stream_server.calls)==1 and provider.client.is_closed


@pytest.mark.parametrize('condition,code',[
    ('length','output_truncated'),('failed','provider_unavailable'),
    ('refusal','provider_refused'),('broken-usage','provider_invalid_response')])
async def test_bailian_stream_terminal_errors_preserve_confirmed_usage(stream_server,condition,code):
    body=completed(MODELS[0]);prefix=b''
    if condition=='length':body.update(status='incomplete',incomplete_details={'reason':'max_output_tokens'})
    elif condition=='failed':body.update(status='failed',error={'code':'server_error','message':'private'})
    elif condition=='refusal':prefix=frame({'type':'response.refusal.delta','delta':'refused'})
    else:body['usage']['total_tokens']=True
    stream_server.data=prefix+frame({'type':'response.'+body['status'],'response':body})
    provider=BailianProvider(stream_server.url,'FAKE-F13-KEY')
    try:events=[e async for e in provider.stream(SimpleNamespace(model_id=MODELS[0]),
        [Message(role='user',content='Q')],max_output_tokens=128,timeout_seconds=2)]
    finally:await provider.aclose()
    final=events[-1]
    assert final.error_code==code and final.status=='failed'
    assert final.actual_model==MODELS[0]
    if condition=='broken-usage':assert final.usage is None and not final.upstream_terminal_received
    else:
        assert final.usage.total_tokens==15 and final.cached_input_tokens==4
        assert final.billing_observed_at is not None and final.upstream_terminal_received


async def test_bailian_invalid_json_is_normalized_without_exposing_body(server):
    server.raw=b'PRIVATE-INVALID-JSON';provider=BailianProvider(server.url,'FAKE-F13-KEY')
    try:
        with pytest.raises(ProviderCallError) as caught:
            await provider.complete(SimpleNamespace(model_id=MODELS[0]),[Message(role='user',content='Q')],
                max_output_tokens=128,timeout_seconds=2)
        assert caught.value.code=='provider_invalid_response'
        assert 'PRIVATE-INVALID-JSON' not in str(caught.value)
    finally:await provider.aclose()


async def test_bailian_complete_timeout_remains_one_call_and_client_closes(server):
    server.delay=.2;provider=BailianProvider(server.url,'FAKE-F13-KEY')
    try:
        with pytest.raises(ProviderCallError) as caught:
            await provider.complete(SimpleNamespace(model_id=MODELS[0]),[Message(role='user',content='Q')],
                max_output_tokens=128,timeout_seconds=.05)
        assert caught.value.code=='provider_timeout'
    finally:await provider.aclose()
    assert len(server.calls)==1 and provider.client.is_closed


async def test_queued_disconnect_keeps_unknown_actual_model_and_original_status(stream_server):
    stream_server.data=frame({'type':'response.created','response':{'object':'response','status':'queued','model':''}})
    provider=BailianProvider(stream_server.url,'FAKE-F13-KEY')
    try:
        events=[e async for e in provider.stream(SimpleNamespace(model_id='qwen3.8-flash'),
            [Message(role='user',content='Question')],max_output_tokens=128,timeout_seconds=2)]
    finally:await provider.aclose()
    assert events[-1].status=='failed'
    assert events[-1].upstream_status=='queued'
    assert events[-1].actual_model is None and events[-1].usage is None
    assert not events[-1].upstream_terminal_received
