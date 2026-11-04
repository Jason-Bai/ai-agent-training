"""Real SDK/httpcore2 loopback checks; no PostgreSQL or supplier access."""
import asyncio
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Event, Thread
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
import pytest

from contracts import Message
from providers.bailian import BailianProvider
from transport_observation import install


KEY = 'fake-private-api-key-NEVER-RECORD'
BODY = 'private-user-body-NEVER-RECORD'
ANSWER = 'private-model-answer-NEVER-RECORD'
HEADER = 'private-response-header-NEVER-RECORD'
CANCEL = 'private-cancellation-message-NEVER-RECORD'


@pytest.fixture
def server():
    state = SimpleNamespace(hold=False,received=Event(),release=Event(),calls=0,bytes_received=0)
    response = dict(object='response',status='completed',model='fixture-model',error=None,incomplete_details=None,
        output=[dict(type='message',role='assistant',status='completed',content=[dict(type='output_text',text=ANSWER)])],
        usage=dict(input_tokens=10,output_tokens=5,total_tokens=15,input_tokens_details={'cached_tokens':0}))
    payload = json.dumps(response).encode()
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self,*args): pass
        def do_POST(self):
            self.close_connection = True
            data = self.rfile.read(int(self.headers['content-length']))
            assert self.headers['authorization'] == 'Bearer '+KEY
            assert BODY in data.decode()
            state.calls += 1; state.bytes_received = len(data); state.received.set()
            if state.hold: state.release.wait(5)
            try:
                self.send_response(200)
                self.send_header('content-type','application/json')
                self.send_header('content-length',str(len(payload)))
                self.send_header('connection','close')
                self.send_header('x-private-response',HEADER)
                self.end_headers(); self.wfile.write(payload); self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError):
                pass
    http = ThreadingHTTPServer(('127.0.0.1',0),Handler); http.daemon_threads = True
    worker = Thread(target=lambda:http.serve_forever(poll_interval=.01),daemon=True); worker.start()
    state.url = 'http://127.0.0.1:'+str(http.server_port)
    try: yield state
    finally:
        state.release.set(); http.shutdown(); http.server_close(); worker.join(3)


def application(provider,tmp_path,monkeypatch):
    @asynccontextmanager
    async def lifespan(app): yield
    app = FastAPI(lifespan=lifespan)
    trusted = []
    async def begin(trace,attempt,**options):
        trusted.append((trace.request_id,attempt.number))
    app.state.storage = SimpleNamespace(begin_attempt=begin)
    app.state.providers = {'local':provider,'same-instance-alias':provider}
    install(app,tmp_path,monkeypatch)
    return app,trusted


async def invoke(app,provider,rid,number):
    await app.state.storage.begin_attempt(SimpleNamespace(request_id=rid),SimpleNamespace(number=number))
    return await provider.complete(SimpleNamespace(model_id='fixture-model'),
        [Message(role='user',content=BODY)],max_output_tokens=16,timeout_seconds=3)


def saved(tmp_path,rid):
    path = tmp_path/('transport-'+rid+'.json')
    assert path.exists(), 'real transport stages must be retained by request_id'
    raw = path.read_text()
    for secret in (KEY,BODY,ANSWER,HEADER,CANCEL,'authorization','x-private-response'):
        assert secret not in raw
    value = json.loads(raw); assert value['request_id'] == rid
    return value


def assert_phases(attempt,*,headers_received):
    events = attempt['events']; names = [entry['event'] for entry in events]
    assert 'connection.connect_tcp.started' in names and 'connection.connect_tcp.complete' in names
    assert 'http11.send_request_headers.complete' in names
    assert 'http11.send_request_body.complete' in names
    assert 'http11.receive_response_headers.started' in names
    assert ('http11.receive_response_headers.complete' in names) is headers_received
    assert 'tls' not in attempt['observed_stages'], 'plain HTTP cannot demonstrate TLS'
    assert {'connect','send_headers','send_body','receive_headers'} <= set(attempt['observed_stages'])
    assert [entry['elapsed_ms'] for entry in events] == sorted(entry['elapsed_ms'] for entry in events)
    assert all(entry['elapsed_ms'] >= 0 and set(entry) == {'event','elapsed_ms','exception_type'} for entry in events)


async def test_real_http200_records_stages_without_private_transport_info(tmp_path,monkeypatch,server):
    provider = BailianProvider(server.url,KEY); previous_events = []
    async def existing_request_hook(request):
        async def previous(event,info): previous_events.append(event)
        request.extensions['trace'] = previous
    provider.client._client.event_hooks['request'].append(existing_request_hook)
    app,trusted = application(provider,tmp_path,monkeypatch)
    rid = str(uuid4())
    try:
        async with app.router.lifespan_context(app):
            result = await invoke(app,provider,rid,3)
        assert result.text == ANSWER and result.usage.total_tokens == 15
        assert server.received.is_set() and server.bytes_received > 0 and server.calls == 1
        assert trusted == [(rid,3)]
        value = saved(tmp_path,rid); assert len(value['attempts']) == 1
        attempt = value['attempts'][0]; assert attempt['attempt_no'] == 3
        assert attempt['invocation_status'] == 'returned' and attempt['invocation_exception_type'] is None
        assert attempt['call_cancelled'] is False
        assert_phases(attempt,headers_received=True)
        assert [entry['event'] for entry in attempt['events']] == previous_events
        assert app.state.transport_observation_errors == []
    finally: await provider.aclose()


async def test_cancellation_before_response_headers_preserves_original_cancel_and_stages(tmp_path,monkeypatch,server):
    server.hold = True
    provider = BailianProvider(server.url,KEY); app,trusted = application(provider,tmp_path,monkeypatch)
    rid = str(uuid4()); task = None
    try:
        async with app.router.lifespan_context(app):
            task = asyncio.create_task(invoke(app,provider,rid,1))
            assert await asyncio.to_thread(server.received.wait,2), 'server did not receive the complete real request body'
            task.cancel(CANCEL)
            with pytest.raises(asyncio.CancelledError) as raised:
                await task
        assert raised.value.args == (CANCEL,), 'transport observation changed the original cancellation'
        assert server.calls == 1 and server.bytes_received > 0 and trusted == [(rid,1)]
        value = saved(tmp_path,rid); attempt = value['attempts'][0]
        assert attempt['invocation_status'] == 'cancelled' and attempt['call_cancelled'] is True
        assert attempt['invocation_exception_type'] == 'CancelledError'
        assert_phases(attempt,headers_received=False)
        failed = [event for event in attempt['events'] if event['event'] == 'http11.receive_response_headers.failed']
        assert len(failed) == 1 and failed[0]['exception_type'] == 'CancelledError'
        assert app.state.transport_observation_errors == []
    finally:
        server.release.set()
        if task is not None and not task.done():
            task.cancel(); await asyncio.gather(task,return_exceptions=True)
        await provider.aclose()


async def test_observation_write_error_is_explicit_and_does_not_change_original_result(tmp_path,monkeypatch,server):
    provider = BailianProvider(server.url,KEY)
    app,_ = application(provider,tmp_path/'absent-directory',monkeypatch)
    try:
        async with app.router.lifespan_context(app):
            result = await invoke(app,provider,str(uuid4()),1)
        assert result.text == ANSWER and result.usage.total_tokens == 15 and server.calls == 1
        assert app.state.transport_observation_errors
        assert {error['error_type'] for error in app.state.transport_observation_errors} == {'FileNotFoundError'}
        assert all(set(error) == {'request_id','attempt_no','error_type'} for error in app.state.transport_observation_errors)
    finally:
        await provider.aclose()
