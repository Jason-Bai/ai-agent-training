from registry_helpers import ModelRegistry
"""Safe recovery hints cross real adapter/SDK boundaries without hidden retries."""
from services.models import ConcurrencyPolicy
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import json
import pytest
from contracts import Message
from errors import ProviderCallError
from providers.base import ProviderStreamEvent, error_for_status, parse_responses_response, validate_result, ProviderResult
from providers.deepseek import DeepSeekProvider
from providers.openai_compatible import OpenAICompatibleProvider
from f4_cases import registry

@pytest.mark.parametrize('status,transient', [(500,True),(502,True),(503,True),(504,True),(501,False),(505,False),(401,False),(402,False),(429,False),(422,False)])
def test_f7_status_evidence(status, transient):
    error = error_for_status(status)
    assert getattr(error, 'transient', None) is transient
    assert error.upstream_http_status == status

@pytest.mark.parametrize('value,expected', [('0',0),('15',15),('-1',None),('1.5',None),('NaN',None),('Infinity',None),('',None),('bad',None),('0, 1',None)])
def test_f7_retry_after_seconds(value, expected):
    from providers.base import retry_after_seconds
    assert retry_after_seconds([value]) == expected

def test_f7_retry_after_dates_duplicates_and_expiration():
    from providers.base import retry_after_seconds
    now = datetime(2026,11,2,tzinfo=timezone.utc)
    assert retry_after_seconds(['Mon, 02 Nov 2026 00:00:15 GMT'], now=now) == 15
    assert retry_after_seconds(['Sun, 01 Nov 2026 00:00:00 GMT'], now=now) == 0
    assert retry_after_seconds(['Mon, 02 Nov 2026 00:00:15 GMT','15'],now=now) is None
    assert retry_after_seconds([],now=now) is None

@pytest.mark.parametrize('reason', ['aborted','insufficient_system_resource'])
def test_f7_explicit_terminal_resource_evidence(reason):
    with pytest.raises(ProviderCallError) as caught:
        validate_result(ProviderResult(text='',finish_reason=reason))
    assert caught.value.transient is True

def test_f7_responses_server_error_evidence():
    with pytest.raises(ProviderCallError) as caught:
        parse_responses_response({'object':'response','model':'local','status':'failed',
            'error':{'code':'server_error','message':'private-error-do-not-copy'},'incomplete_details':None})
    assert caught.value.transient is True
    assert 'private-error' not in str(caught.value)

@pytest.mark.parametrize('fields', [{'retry_after_seconds':-1},{'retry_after_seconds':float('nan')},
    {'retry_after_seconds':True},{'transient':True},{'upstream_http_status':503}])
def test_f7_recovery_metadata_only_on_failure(fields):
    with pytest.raises(ValueError): ProviderStreamEvent(**fields)

@contextmanager
def upstream(status, headers):
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args): pass
        def do_POST(self):
            calls.append((self.path,json.loads(self.rfile.read(int(self.headers['content-length'])))))
            data=b'{"error":{"message":"fake-private-key","type":"server_error","code":"server_error"}}'
            self.send_response(status);self.send_header('Content-Type','application/json')
            for key,value in headers: self.send_header(key,value)
            self.send_header('Content-Length',str(len(data)));self.end_headers()
            self.wfile.write(data); self.wfile.flush()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    thread=Thread(target=lambda:server.serve_forever(poll_interval=.01),daemon=True);thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}',calls
    finally: server.shutdown();server.server_close();thread.join(3)

@pytest.mark.parametrize('adapter', ['deepseek','openai_compatible'])
@pytest.mark.parametrize('stream', [False,True])
@pytest.mark.parametrize('status,headers,expected', [(503,[('Retry-After','15')],15),
    (429,[('Retry-After','1'),('Retry-After','2')],None),(501,[],None)])
async def test_f7_wire_metadata_and_single_attempt(adapter, stream, status, headers, expected):
    with upstream(status,headers) as (url,calls):
        provider = (DeepSeekProvider if adapter=='deepseek' else OpenAICompatibleProvider)(url,'fake-local-key')
        model=registry().get_endpoint('general-primary')
        try:
            if stream:
                events=[e async for e in provider.stream(model,[Message(role='user',content='订单A123')],max_output_tokens=16,timeout_seconds=3)]
                assert len(events)==1 and events[0].status=='failed'
                result=events[0]
            else:
                with pytest.raises(ProviderCallError) as caught:
                    await provider.complete(model,[Message(role='user',content='订单A123')],max_output_tokens=16,timeout_seconds=3)
                result=caught.value
            assert getattr(result,'retry_after_seconds','missing') == expected
            assert result.transient is (status==503)
            assert result.upstream_http_status == status
            assert 'fake-private-key' not in str(result)
            assert len(calls)==1
            assert calls[0][0] == ('/chat/completions' if adapter=='deepseek' else '/responses')
        finally: await provider.aclose()

@pytest.mark.parametrize('value', ['Mon, 02 Nov 2026 00:00:15 GMT, 15',
    'Mon, 02 Nov 2026 00:00:15 GMT junk', 'Mon, 02 Nov 2026 00:00:15 +0100'])
def test_f7_invalid_or_combined_date_header_not_accepted(value):
    from providers.base import retry_after_seconds
    assert retry_after_seconds([value],now=datetime(2026,11,2,tzinfo=timezone.utc)) is None

@pytest.mark.parametrize('scripted', [False, True])
async def test_f7_mock_failure_keeps_recovery_evidence(scripted):
    from providers.mock import MockProvider
    from contracts import Usage
    usage=Usage(input_tokens=3,output_tokens=2,total_tokens=5)
    error=ProviderCallError(code='provider_unavailable',transient=True,retry_after_seconds=15.0,
                            upstream_http_status=503,usage=usage,actual_model='local')
    provider=MockProvider(stream_events=[error]) if scripted else MockProvider(error=error)
    events=[e async for e in provider.stream(registry().get_endpoint('general-primary'),[],max_output_tokens=16,timeout_seconds=3)]
    assert len(events)==1
    assert events[0].transient is True
    assert events[0].retry_after_seconds==15
    assert events[0].upstream_http_status==503
    assert events[0].usage==usage
    assert events[0].actual_model=='local'

@pytest.mark.parametrize('reason', ['aborted','insufficient_system_resource'])
async def test_f7_chat_terminal_event_keeps_resource_evidence(reason):
    from providers.streaming import chat_events
    async def lines():
        body={'object':'chat.completion.chunk','model':'local',
              'choices':[{'index':0,'delta':{},'finish_reason':reason}]}
        for line in ['data: '+json.dumps(body),'']: yield line
    events=[e async for e in chat_events(lines(),{})]
    assert len(events)==1 and events[0].transient is True

async def test_f7_responses_terminal_event_keeps_resource_evidence():
    from providers.streaming import responses_events
    async def lines():
        body={'type':'response.failed','response':{'object':'response','model':'local','status':'failed',
            'error':{'code':'server_error','message':'private'},'incomplete_details':None}}
        for line in ['data: '+json.dumps(body),'']: yield line
    events=[e async for e in responses_events(lines(),{})]
    assert len(events)==1 and events[0].transient is True


# These cases traverse real adapter -> Gateway recovery -> committed PG -> HTTP,
# complementing the single-call wire tests above (which do not prove recovery).
from f6_cases import server, case, observe

@contextmanager
def recovering_upstream(protocol,mode):
    from f7_cases import VALID
    calls=[]
    def frame(value): return 'data: '+(value if isinstance(value,str) else json.dumps(value))+'\n\n'
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args): pass
        def do_POST(self):
            content=json.loads(self.rfile.read(int(self.headers['content-length'])))
            calls.append((self.path,content))
            if len(calls)==1:
                status=503;kind='application/json'
                payload=json.dumps({'error':{'message':'fake-private-api-key-xyz fake-db-password-xyz Traceback (most recent call last):','type':'server_error','code':'server_error'}})
            else:
                status=200
                text=VALID if mode=='structured' else '本地完整回答'
                if protocol=='chat_completions':
                    if mode=='stream':
                        kind='text/event-stream'
                        payload=frame({'object':'chat.completion.chunk','model':'wire-chat','choices':[{'index':0,'delta':{'content':text},'finish_reason':None}]})
                        payload+=frame({'object':'chat.completion.chunk','model':'wire-chat','choices':[{'index':0,'delta':{},'finish_reason':'stop'}],
                            'usage':{'prompt_tokens':3,'completion_tokens':2,'total_tokens':5}})+frame('[DONE]')
                    else:
                        kind='application/json';payload=json.dumps({'model':'wire-chat','choices':[{'message':{'role':'assistant','content':text},'finish_reason':'stop'}],
                            'usage':{'prompt_tokens':3,'completion_tokens':2,'total_tokens':5}})
                else:
                    result={'object':'response','model':'wire-responses','status':'completed','error':None,'incomplete_details':None,
                        'output':[{'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':text}]}],
                        'usage':{'input_tokens':3,'output_tokens':2,'total_tokens':5}}
                    if mode=='stream':
                        kind='text/event-stream';payload=frame({'type':'response.output_text.delta','delta':text})+frame({'type':'response.completed','response':result})
                    else: kind='application/json';payload=json.dumps(result)
            wire=payload.encode()
            self.send_response(status);self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(wire)));self.end_headers()
            self.wfile.write(wire);self.wfile.flush()
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler);http.daemon_threads=True
    thread=Thread(target=lambda:http.serve_forever(poll_interval=.01),daemon=True);thread.start()
    try: yield f'http://127.0.0.1:{http.server_port}',calls
    finally: http.shutdown();http.server_close();thread.join(3)


@pytest.mark.parametrize('adapter,protocol,structured_mode',[
    ('deepseek','chat_completions','json_object'),('openai_compatible','responses','json_schema')])
@pytest.mark.parametrize('mode',['text','structured','stream'])
async def test_f7_real_adapters_gateway_recovers_with_committed_attempts(server,case,adapter,protocol,structured_mode,mode,caplog):
    from f7_cases import registry as recovery_registry,application,body,SCHEMA,VALID
    from services.models import ProviderConfig
    from test_f5o2_http import frames
    name,config=case
    with recovering_upstream(protocol,mode) as (url,calls):
        base=recovery_registry()
        candidates=dict(base.endpoints)
        candidates['general-primary']=candidates['general-primary'].model_copy(update={'structured_output_mode':structured_mode})
        definitions=dict(base.providers)
        definitions['mock-main']=ProviderConfig(adapter=adapter,api_protocol=protocol,base_url=url,credential_ref='fake-local-key-ref')
        models=ModelRegistry(candidates,definitions,recovery=base.recovery, concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))
        provider=(DeepSeekProvider if adapter=='deepseek' else OpenAICompatibleProvider)(url,'fake-local-key')
        try:
            async with application(config,provider,models=models) as (_,client):
                request=body(response_schema=SCHEMA) if mode=='structured' else body()
                if mode=='stream':
                    created=await client.post('/v1/llm/stream',json=request)
                    assert created.status_code==202,created.text
                    rid=created.json()['data']['request_id']
                    response=await client.get(f'/v1/llm/stream/{rid}/events')
                    parsed=frames(response.text)
                    assert [f[1] for f in parsed]==['response.delta','response.completed']
                else:
                    response=await client.post('/v1/llm',json=request)
                    assert response.status_code==200,response.text
                    rid=response.headers['x-request-id']
                    assert response.json()['data']['text']==(VALID if mode=='structured' else '本地完整回答')
                    if mode=='structured': assert response.json()['data']['parsed']==json.loads(VALID)
                row,attempts,saved=observe(server,name,rid)
                assert row['status']=='completed' and len(attempts)==2
                assert [a['status'] for a in attempts]==['failed','completed']
                assert [a['reason'] for a in attempts]==['initial','network_retry']
                assert all(a['adapter']==adapter and a['api_protocol']==protocol for a in attempts)
                assert len(calls)==2  # also detects hidden SDK/transport retries
                assert calls[0][0]==calls[1][0]==('/chat/completions' if protocol=='chat_completions' else '/responses')
                if mode=='structured':
                    outgoing=calls[-1][1]
                    if protocol=='chat_completions':
                        assert outgoing['response_format']=={'type':'json_object'}
                        assert '"schema":' in outgoing['messages'][-1]['content']
                    else:
                        assert outgoing['text']['format']['schema']==SCHEMA
                        assert '"schema":' not in outgoing['input'][-1]['content']
                for marker in ['fake-private-api-key-xyz','fake-db-password-xyz','Traceback (most recent call last):']:
                    assert marker not in response.text and marker not in caplog.text
                    assert all(marker not in e['payload'] for e in saved)
        finally: await provider.aclose()
