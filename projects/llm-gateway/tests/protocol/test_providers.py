import asyncio
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
from threading import Thread
import time
from types import SimpleNamespace

import pytest

from contracts import Message
from errors import ProviderCallError
from providers.base import validate_result
from providers.deepseek import DeepSeekProvider
from providers.openai_compatible import OpenAICompatibleProvider
from f3_cases import registry


SUCCESS={'id':'fake','object':'chat.completion','created':0,'model':'deepseek-flash','choices':[{'index':0,'message':{'role':'assistant','content':'TEST_REPLY'},'finish_reason':'stop'}],'usage':{'prompt_tokens':120,'completion_tokens':30,'total_tokens':150}}



def response_for_protocol(body, path):
    """Project Chat-shaped legacy test intent into a Responses wire fixture."""
    if not path.rstrip('/').endswith('/responses') or not isinstance(body, dict) or 'choices' not in body:
        return body
    wire={'id':'fixture','object':'response','model':body.get('model'),'status':'completed',
          'error':None,'incomplete_details':None,'output':[]}
    if body.get('usage') is not None:
        usage=body['usage']
        wire['usage']={new:usage[old] for old,new in [('prompt_tokens','input_tokens'),('completion_tokens','output_tokens'),('total_tokens','total_tokens')] if old in usage}
    for choice in body['choices']:
        message=choice['message'];reason=choice['finish_reason']
        if reason!='stop':
            wire['status']='incomplete'
            wire['incomplete_details']={'reason':{'length':'max_output_tokens','content_filter':'content_filter'}.get(reason,reason)}
        wire['output'].append({'type':'message','role':message['role'],'status':'completed',
                               'content':[{'type':'output_text','text':message['content']}]})
        if message.get('tool_calls'):wire['output'].append({'type':'function_call','arguments':'{}'})
    return wire


def sent_messages(call):
    return call['body']['input' if call['path'].rstrip('/').endswith('/responses') else 'messages']


def assert_sent_budget(call, budget):
    body=call['body']
    if call['path'].rstrip('/').endswith('/responses'):
        assert body['max_output_tokens']==budget
        assert body['reasoning']=={'effort':'none'}
        assert body['stream'] is False and body['store'] is False
    else:
        assert body['max_tokens']==budget
        assert body['thinking']=={'type':'disabled'}


@pytest.fixture
def server():
    state=SimpleNamespace(calls=[],status=200,body=deepcopy(SUCCESS),raw=None,delay=0)
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def handle(self):
            try: super().handle()
            except ConnectionResetError: pass  # Expected client cancellation in timeout cases.
        def log_message(self,*args): pass
        def do_POST(self):
            body=self.rfile.read(int(self.headers['content-length']))
            state.calls.append({'path':self.path,'headers':{key.lower(): value for key,value in self.headers.items()},'body':json.loads(body),'raw_body':body.decode('utf-8')})
            time.sleep(state.delay)
            data=state.raw if state.raw is not None else json.dumps(response_for_protocol(state.body,self.path)).encode()
            try:
                self.send_response(state.status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError): pass
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler);http.daemon_threads=True
    thread=Thread(target=lambda:http.serve_forever(poll_interval=.01),daemon=True);thread.start()
    state.url=f'http://127.0.0.1:{http.server_port}'
    yield state
    http.shutdown();http.server_close();thread.join(timeout=3)


@pytest.fixture(params=[DeepSeekProvider,OpenAICompatibleProvider],ids=['direct','sdk'])
def adapter_type(request): return request.param


async def invoke(adapter_type,server,*,timeout=2):
    adapter=adapter_type(server.url,'FAKE_CREDENTIAL_MARKER')
    try:
        result=await adapter.complete(registry().get_endpoint('general-primary'),[Message(role='system',content='Fixed'),Message(role='user',content='Question')],max_output_tokens=64,timeout_seconds=timeout)
        validate_result(result)
        return result
    finally: await adapter.aclose()


async def test_real_transport_mapping_and_success(adapter_type,server):
    result=await invoke(adapter_type,server)
    assert result.text=='TEST_REPLY'
    assert result.actual_model=='deepseek-flash'
    assert result.usage.total_tokens==150
    assert len(server.calls)==1
    call=server.calls[0]
    assert call['path']==('/responses' if adapter_type is OpenAICompatibleProvider else '/chat/completions')
    assert call['headers']['authorization']=='Bearer FAKE_CREDENTIAL_MARKER'
    assert call['body']['model']=='deepseek-flash'
    assert sent_messages(call)==[{'role':'system','content':'Fixed'},{'role':'user','content':'Question'}]
    assert_sent_budget(call,64)
    assert not call['body'].get('stream',False)


@pytest.mark.parametrize('status,code,mapped',[(401,'provider_auth_error',502),(402,'provider_quota_exceeded',503),(429,'provider_rate_limited',503),(400,'provider_rejected_request',502),(404,'provider_rejected_request',502),(422,'provider_rejected_request',502),(500,'provider_unavailable',503),(503,'provider_unavailable',503),(418,'provider_error',502)])
async def test_http_statuses_without_hidden_retries(adapter_type,server,status,code,mapped):
    server.status=status;server.body={'error':{'message':'PRIVATE_ERROR_MARKER','type':'upstream'}}
    with pytest.raises(ProviderCallError) as exc: await invoke(adapter_type,server)
    assert (exc.value.code,exc.value.status_code)==(code,mapped)
    assert 'PRIVATE_ERROR_MARKER' not in str(exc.value)
    assert len(server.calls)==1


@pytest.mark.parametrize('adapter_type',[DeepSeekProvider],ids=['direct'])
@pytest.mark.parametrize('reason,code',[('length','output_truncated'),('content_filter','provider_content_filtered'),('aborted','provider_unavailable'),('insufficient_system_resource','provider_unavailable'),('unknown','provider_invalid_response')])
async def test_completion_end_states(adapter_type,server,reason,code):
    server.body['choices'][0]['finish_reason']=reason
    with pytest.raises(ProviderCallError) as exc: await invoke(adapter_type,server)
    assert exc.value.code==code
    assert exc.value.usage.total_tokens==150
    assert exc.value.finish_reason==reason
    assert len(server.calls)==1


@pytest.mark.parametrize('damage',['json','list','model','choices','content','usage_string','usage_bool','usage_sum','usage_missing','tool','reason_none'])
async def test_wire_types_are_validated_before_sdk_coercion(adapter_type,server,damage):
    if damage=='json':server.raw=b'PRIVATE_ERROR_MARKER'
    elif damage=='list':server.body=[]
    elif damage=='model':server.body['model']=None
    elif damage=='choices':server.body['choices']=[]
    elif damage=='content':server.body['choices'][0]['message']['content']=123
    elif damage=='usage_string':server.body['usage']['prompt_tokens']='120'
    elif damage=='usage_bool':server.body['usage']['completion_tokens']=True
    elif damage=='usage_sum':server.body['usage']['total_tokens']=151
    elif damage=='usage_missing':del server.body['usage']['prompt_tokens']
    elif damage=='tool':server.body['choices'][0]['message']['tool_calls']=[{'id':'unexpected'}]
    else:server.body['choices'][0]['finish_reason']=None
    with pytest.raises(ProviderCallError) as exc: await invoke(adapter_type,server)
    assert exc.value.code=='provider_invalid_response'
    assert len(server.calls)==1


async def test_network_timeout_is_classified_and_not_retried(adapter_type,server):
    server.delay=1.2
    with pytest.raises(ProviderCallError) as exc: await invoke(adapter_type,server,timeout=1)
    assert exc.value.code=='provider_timeout'
    assert len(server.calls)==1


async def test_connection_failure(adapter_type,server):
    # Allocate then close a local ephemeral port instead of assuming port 1.
    with socket.socket() as closed:
        closed.bind(('127.0.0.1', 0))
        port = closed.getsockname()[1]
    server.url=f'http://127.0.0.1:{port}'
    with pytest.raises(ProviderCallError) as exc: await invoke(adapter_type,server)
    assert exc.value.code=='provider_connection_error'
    assert server.calls==[]


async def test_client_reuse_and_explicit_close(adapter_type,server):
    adapter=adapter_type(server.url,'FAKE_CREDENTIAL_MARKER')
    try:
        for i in range(2):
            result=await adapter.complete(registry().get_endpoint('general-primary'),[Message(role='user',content=str(i))],max_output_tokens=16,timeout_seconds=2)
            assert result.text=='TEST_REPLY'
        assert len(server.calls)==2
    finally:await adapter.aclose()
    closed = adapter.client.is_closed
    assert closed() if callable(closed) else closed


async def test_configured_base_path_is_preserved(adapter_type,server):
    server.url += '/v1/'
    await invoke(adapter_type,server)
    assert server.calls[0]['path']==('/v1/responses' if adapter_type is OpenAICompatibleProvider else '/v1/chat/completions')


async def test_redirect_is_not_followed(adapter_type,server):
    server.status=302
    with pytest.raises(ProviderCallError) as exc: await invoke(adapter_type,server)
    assert exc.value.code=='provider_error'
    assert len(server.calls)==1


async def test_concurrent_call_options_are_not_client_state(adapter_type,server):
    adapter=adapter_type(server.url,'FAKE_CREDENTIAL_MARKER')
    model=registry().get_endpoint('general-primary')
    try:
        results=await asyncio.gather(*[
            adapter.complete(model,[Message(role='user',content=label)],max_output_tokens=budget,timeout_seconds=2)
            for label,budget in [('first',16),('second',32)]
        ])
        assert all(result.text=='TEST_REPLY' for result in results)
        calls={sent_messages(call)[0]['content']:call['body'] for call in server.calls}
        assert set(calls)=={'first','second'}
        key='max_output_tokens' if adapter_type is OpenAICompatibleProvider else 'max_tokens'
        assert calls['first'][key]==16
        assert calls['second'][key]==32
    finally:await adapter.aclose()
