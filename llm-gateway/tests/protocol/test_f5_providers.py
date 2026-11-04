"""F5 wire tests exercise real HTTP transports and the SDK's raw stream API."""
import asyncio
import json
import time
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import pytest

from contracts import Message
from providers.deepseek import DeepSeekProvider
from providers.openai_compatible import OpenAICompatibleProvider
from providers.mock import MockProvider
from f3_cases import registry


def chat(text='', reason=None, usage=None):
    return {'object':'chat.completion.chunk','model':'deepseek-flash','choices':[{'index':0,'delta':{'content':text},'finish_reason':reason}], 'usage':usage}


def response(status='completed'):
    return {'object':'response','model':'deepseek-flash','status':status,'error':None,'incomplete_details':None,
            'output':[{'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':'订单 A123'}]}],
            'usage':{'input_tokens':10,'output_tokens':5,'total_tokens':15}}


def frame(data,event=None):
    return ((f'event: {event}\n' if event else '')+'data: '+(data if isinstance(data,str) else json.dumps(data,ensure_ascii=False))+'\n\n').encode()


@pytest.fixture
def stream_server():
    state=SimpleNamespace(data=b'',status=200,calls=[],split=False,delay_headers=0,delay_body=0,reset=False)
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def handle(self):
            try:super().handle()
            except ConnectionResetError:pass
        def do_POST(self):
            body=self.rfile.read(int(self.headers['content-length']))
            state.calls.append({'path':self.path,'body':json.loads(body)})
            if state.reset:
                self.close_connection=True;self.connection.shutdown(socket.SHUT_RDWR);return
            time.sleep(state.delay_headers)
            self.send_response(state.status);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(state.data)));self.end_headers()
            try:
                time.sleep(state.delay_body)
                for data in ([bytes([b]) for b in state.data] if state.split else [state.data]):self.wfile.write(data);self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    thread=Thread(target=lambda:server.serve_forever(poll_interval=.01),daemon=True);thread.start()
    state.url=f'http://127.0.0.1:{server.server_port}'
    yield state
    server.shutdown();server.server_close();thread.join(3)


@pytest.mark.parametrize('kind',[DeepSeekProvider,OpenAICompatibleProvider,MockProvider])
def test_stream_contract_exists(kind):
    assert callable(getattr(kind,'stream',None)), 'Provider must expose incremental stream'


async def collect(kind,server):
    provider=kind(server.url,'FAKE_KEY')
    try:
        assert callable(getattr(provider,'stream',None))
        return [e async for e in provider.stream(registry().get_endpoint('general-primary'),[Message(role='user',content='Question')],max_output_tokens=64,timeout_seconds=2)]
    finally:await provider.aclose()


@pytest.mark.parametrize('kind',[DeepSeekProvider,OpenAICompatibleProvider])
async def test_wire_success(kind,stream_server):
    stream_server.split=True
    if kind is DeepSeekProvider:
        stream_server.data=frame(chat('订单'))+frame(chat(' A123'))+frame(chat(reason='stop',usage={'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}))+frame('[DONE]')
    else:
        stream_server.data=frame({'type':'response.output_text.delta','delta':'订单'})+frame({'type':'response.output_text.delta','delta':' A123'})+frame({'type':'response.output_text.done','text':'订单 A123'})+frame({'type':'response.completed','response':response()})
    events=await collect(kind,stream_server)
    assert ''.join(e.delta for e in events)=='订单 A123'
    assert events[-1].status=='completed' and events[-1].upstream_terminal_received
    assert events[-1].usage.total_tokens==15
    assert len(stream_server.calls)==1
    sent=stream_server.calls[0]
    assert sent['body']['stream'] is True
    if kind is DeepSeekProvider:
        assert sent['path']=='/chat/completions';assert sent['body']['stream_options']=={'include_usage':True}
    else:
        assert sent['path']=='/responses';assert sent['body']['store'] is False;assert sent['body']['reasoning']=={'effort':'none'}


@pytest.mark.parametrize('kind',[DeepSeekProvider,OpenAICompatibleProvider])
@pytest.mark.parametrize('damage',['eof','json','truncated','usage_bool','tool','unknown','refusal','filtered','failed'])
async def test_failure_mapping(kind,stream_server,damage):
    if kind is DeepSeekProvider:
        body=chat('first');end=chat(reason='stop')
        if damage=='truncated':end['choices'][0]['finish_reason']='length'
        if damage=='usage_bool':end['usage']={'prompt_tokens':True,'completion_tokens':1,'total_tokens':2}
        if damage=='tool':end['choices'][0]['delta']['tool_calls']=[{}]
        if damage=='unknown':end['choices'][0]['finish_reason']='UNKNOWN'
        if damage=='refusal':end['choices'][0]['delta']['refusal']='PRIVATE'
        if damage=='filtered':end['choices'][0]['finish_reason']='content_filter'
        if damage=='failed':end['choices'][0]['finish_reason']='aborted'
        stream_server.data=frame(body)+(frame('INVALID') if damage=='json' else b'' if damage=='eof' else frame(end)+frame('[DONE]'))
    else:
        end=response()
        if damage=='truncated':end.update(status='incomplete',incomplete_details={'reason':'max_output_tokens'})
        if damage=='filtered':end.update(status='incomplete',incomplete_details={'reason':'content_filter'})
        if damage=='usage_bool':end['usage']['input_tokens']=True
        if damage=='tool':end['output'].append({'type':'function_call'})
        if damage=='unknown':end['status']='UNKNOWN'
        if damage=='refusal':end['output'][0]['content']=[{'type':'refusal','refusal':'PRIVATE'}]
        if damage=='failed':end.update(status='failed',error={'code':'server_error','message':'PRIVATE'})
        event='response.'+end['status']
        stream_server.data=frame({'type':'response.output_text.delta','delta':'first'})+(frame('INVALID') if damage=='json' else b'' if damage=='eof' else frame({'type':event,'response':end}))
    events=await collect(kind,stream_server)
    assert events[-1].status=='failed'
    expected={'truncated':'output_truncated','refusal':'provider_refused','filtered':'provider_content_filtered','failed':'provider_unavailable'}.get(damage,'provider_invalid_response')
    assert events[-1].error_code==expected
    if damage in {'eof','json','usage_bool','tool','unknown'}:assert events[-1].upstream_terminal_received is False
    assert 'PRIVATE' not in repr(events)
    assert len(stream_server.calls)==1


@pytest.mark.parametrize('kind',[DeepSeekProvider,OpenAICompatibleProvider])
@pytest.mark.parametrize('status,code',[(401,'provider_auth_error'),(402,'provider_quota_exceeded'),(429,'provider_rate_limited'),(400,'provider_rejected_request'),(404,'provider_rejected_request'),(422,'provider_rejected_request'),(500,'provider_unavailable')])
async def test_http_error_no_retries(kind,stream_server,status,code):
    stream_server.status=status;stream_server.data=b'PRIVATE'
    events=await collect(kind,stream_server)
    assert events[-1].error_code==code and not events[-1].upstream_terminal_received
    assert len(stream_server.calls)==1

async def test_refusal_events_wait_for_remote_terminal(stream_server):
    body=response();body['output'][0]['content']=[{'type':'refusal','refusal':'PRIVATE'}]
    stream_server.data=frame({'type':'response.content_part.added','part':{'type':'refusal','refusal':''}})+frame({'type':'response.refusal.delta','delta':'PRIVATE'})+frame({'type':'response.refusal.done','refusal':'PRIVATE'})+frame({'type':'response.completed','response':body})
    events=await collect(OpenAICompatibleProvider,stream_server)
    assert events[-1].error_code=='provider_refused'
    assert events[-1].upstream_terminal_received is True

@pytest.mark.parametrize('kind',[DeepSeekProvider,OpenAICompatibleProvider])
@pytest.mark.parametrize('damage',['bool','float','string','negative','sum'])
async def test_wire_usage_is_strict(kind,stream_server,damage):
    usage={'input_tokens':10,'output_tokens':5,'total_tokens':15}
    if damage=='sum':usage['total_tokens']=16
    else:usage['input_tokens']={'bool':True,'float':10.0,'string':'10','negative':-1}[damage]
    if kind is DeepSeekProvider:
        stream_server.data=frame(chat('text'))+frame(chat(reason='stop',usage={k:usage[v] for k,v in [('prompt_tokens','input_tokens'),('completion_tokens','output_tokens'),('total_tokens','total_tokens')]}))+frame('[DONE]')
    else:
        body=response();body['usage']=usage
        stream_server.data=frame({'type':'response.output_text.delta','delta':'text'})+frame({'type':'response.completed','response':body})
    events=await collect(kind,stream_server)
    assert events[-1].error_code=='provider_invalid_response'
    assert events[-1].upstream_terminal_received is False

@pytest.mark.parametrize('wire',[frame('[DONE]'),frame(chat(reason='stop')),frame(chat('text'))+frame('[DONE]')])
async def test_chat_requires_stop_and_done(stream_server,wire):
    stream_server.data=wire
    events=await collect(DeepSeekProvider,stream_server)
    assert events[-1].error_code=='provider_invalid_response'

@pytest.mark.parametrize('kind',[DeepSeekProvider,OpenAICompatibleProvider])
@pytest.mark.parametrize('close_early',[False,True])
async def test_real_adapter_yields_before_final_and_closes_own_response(kind,close_early):
    import select
    import socket
    from threading import Event
    release=Event();closed=Event();final_sent=Event()
    first=frame(chat('early')) if kind is DeepSeekProvider else frame({'type':'response.output_text.delta','delta':'early'})
    final=frame(chat(reason='stop'))+frame('[DONE]') if kind is DeepSeekProvider else frame({'type':'response.completed','response':response()})
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def handle(self):
            try:super().handle()
            except ConnectionResetError:pass
        def do_POST(self):
            self.rfile.read(int(self.headers['content-length']))
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(first)+len(final)));self.end_headers()
            self.wfile.write(first);self.wfile.flush()
            while not release.wait(.01):
                if select.select([self.connection],[],[],0)[0] and not self.connection.recv(1,socket.MSG_PEEK):
                    closed.set();return
            try:self.wfile.write(final);self.wfile.flush();final_sent.set()
            except (BrokenPipeError,ConnectionResetError):closed.set()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    thread=Thread(target=lambda:server.serve_forever(poll_interval=.01),daemon=True);thread.start()
    provider=kind(f'http://127.0.0.1:{server.server_port}','FAKE_KEY')
    stream=provider.stream(registry().get_endpoint('general-primary'),[Message(role='user',content='Question')],max_output_tokens=64,timeout_seconds=2)
    try:
        event=await asyncio.wait_for(anext(stream),1)
        assert event.delta=='early' and not final_sent.is_set()
        if close_early:
            await stream.aclose()
            assert await asyncio.to_thread(closed.wait,1), 'closing the generator must close upstream response'
            # The reusable client is still alive; only this response was closed.
            client=provider.client if kind is DeepSeekProvider else provider.client._client
            assert not client.is_closed
        else:
            release.set();events=[event async for event in stream]
            assert events[-1].status=='completed'
    finally:
        release.set();await stream.aclose();await provider.aclose()
        server.shutdown();server.server_close();thread.join(2)


@pytest.mark.parametrize('kind',[DeepSeekProvider,OpenAICompatibleProvider])
@pytest.mark.parametrize('phase',['delay_headers','delay_body'])
async def test_stream_transport_timeout_is_not_remote_terminal(kind,stream_server,phase):
    setattr(stream_server,phase,.1);stream_server.data=frame(chat('later'))
    provider=kind(stream_server.url,'FAKE_KEY')
    try:
        events=[event async for event in provider.stream(registry().get_endpoint('general-primary'),[Message(role='user',content='Q')],max_output_tokens=64,timeout_seconds=.02)]
        assert events[-1].error_code=='provider_timeout' and not events[-1].upstream_terminal_received
        assert len(stream_server.calls)==1
    finally:await provider.aclose()


@pytest.mark.parametrize('kind',[DeepSeekProvider,OpenAICompatibleProvider])
async def test_stream_reset_is_classified_as_connection_error(kind,stream_server):
    stream_server.reset=True
    events=await collect(kind,stream_server)
    assert events[-1].error_code=='provider_connection_error' and not events[-1].upstream_terminal_received
    assert len(stream_server.calls)==1


@pytest.mark.parametrize('tail',[b'',frame('INVALID')])
async def test_chat_missing_tail_keeps_valid_usage(stream_server,tail):
    stream_server.data=frame(chat('text'))+frame(chat(reason='stop',usage={'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}))+tail
    events=await collect(DeepSeekProvider,stream_server)
    assert events[-1].error_code=='provider_invalid_response'
    assert events[-1].usage is not None, 'validated Usage must survive a missing/corrupt DONE tail'
    assert events[-1].usage.total_tokens==15
    assert events[-1].actual_model=='deepseek-flash'
    assert events[-1].upstream_finish_reason=='stop'
    assert events[-1].upstream_terminal_received is False


@pytest.mark.parametrize('tail',[b'',frame('INVALID')])
async def test_responses_known_model_survives_later_protocol_failure(stream_server,tail):
    created=response('in_progress');created.update(output=[],usage=None)
    stream_server.data=frame({'type':'response.created','response':created})+tail
    events=await collect(OpenAICompatibleProvider,stream_server)
    assert events[-1].error_code=='provider_invalid_response'
    assert events[-1].actual_model=='deepseek-flash'
    assert events[-1].upstream_status=='in_progress'
    assert events[-1].usage is None and not events[-1].upstream_terminal_received


@pytest.mark.parametrize('tail',[b'',frame('INVALID')])
async def test_gateway_failure_trace_retains_observed_wire_usage(stream_server,tail):
    from contextlib import asynccontextmanager
    from fastapi.testclient import TestClient
    from main import create_app
    from settings import Settings
    stream_server.data=frame(chat('text'))+frame(chat(reason='stop',usage={'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}))+tail
    provider=DeepSeekProvider(stream_server.url,'FAKE_KEY')
    app=create_app(settings=Settings(env='test',_env_file=None),provider=provider,model_registry=registry())
    original=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(app):
        async with original(app):
            try:yield
            finally:await provider.aclose()
    app.router.lifespan_context=lifespan
    with TestClient(app) as client:
        response=client.post('/v1/llm/stream',json={'model':'general-primary','prompt':{'name':'basic_text','version':'1.0.0'},'messages':[{'role':'user','content':'A123'}]})
        assert response.status_code==202
        request_id=response.json()['data']['request_id']
        response=client.get(f'/v1/llm/stream/{request_id}/events')
        assert response.headers['x-request-id']==request_id
        assert not provider.client.is_closed
    trace=app.state.traces.requests[response.headers['x-request-id']]
    attempt=app.state.traces.attempts[0]
    assert response.status_code==200 and 'response.failed' in response.text
    assert trace.error_code==attempt.error_code=='provider_invalid_response'
    assert attempt.status=='failed' and trace.attempts==len(stream_server.calls)==1
    assert attempt.usage.model_dump()=={'input_tokens':10,'output_tokens':5,'total_tokens':15}
    assert attempt.upstream_finish_reason=='stop'
    assert trace.upstream_terminal_at is None and trace.generation_duration_ms is None
    assert provider.client.is_closed
