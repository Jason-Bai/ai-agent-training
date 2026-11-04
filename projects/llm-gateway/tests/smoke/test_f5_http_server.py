"""Actual subprocess + TCP SSE; file IPC exists only in the test bootstrap."""
from contextlib import contextmanager
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from legacy_identity import HEADERS
import pytest


BOOTSTRAP = '''
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
import json
from main import create_app as build_app
from providers.mock import MockProvider
from settings import Settings
import services.streaming as streaming
import services.subscriptions as subscriptions

ROOT=Path(__file__).parents[1]
streaming.STREAM_HEARTBEAT_SECONDS=.05
subscriptions.STREAM_HEARTBEAT_SECONDS=.05
_original_subscription_init=subscriptions.EventSubscription.__init__
def _fast_subscription(self,*args,**kwargs):
    kwargs.setdefault('heartbeat_seconds',.05)
    _original_subscription_init(self,*args,**kwargs)
subscriptions.EventSubscription.__init__=_fast_subscription
class ObservedProvider(MockProvider):
    def __init__(self):
        super().__init__()
        self.stream_calls=[]
        self.closed=[]
    async def stream(self, model, messages, *, max_output_tokens, timeout_seconds):
        from providers.base import ProviderStreamEvent
        label=messages[-1].content
        self.stream_calls.append(label)
        try:
            if label=='before':
                await asyncio.Event().wait()
            yield ProviderStreamEvent(delta=label)
            if label in {'gated','after','concurrent-a'}:
                while not (ROOT/('release-'+label)).exists():
                    await asyncio.sleep(.005)
            if label=='failed':
                yield ProviderStreamEvent(status='failed',error_code='provider_unavailable')
            else:
                yield ProviderStreamEvent(status='completed',upstream_terminal_received=True)
        finally:
            self.closed.append(label)

def create_app():
    provider=ObservedProvider()
    app=build_app(settings=Settings(env='test',_env_file=None),provider=provider)
    original=app.router.lifespan_context
    def snapshot():
        requests={}
        for k,v in app.state.traces.requests.items():
            requests[k]={**asdict(v),'ttft_ms':getattr(v,'ttft_ms',None),
                         'generation_duration_ms':getattr(v,'generation_duration_ms',None),'stream_duration_ms':getattr(v,'stream_duration_ms',None)}
        data={'requests':requests,'attempts':[asdict(v) for v in app.state.traces.attempts],
              'calls':provider.stream_calls,'closed':provider.closed,'client_closed':provider.close_calls}
        tmp=ROOT/'snapshot.tmp'
        tmp.write_text(json.dumps(data))
        tmp.replace(ROOT/'snapshot.json')
    async def observe():
        while True:
            snapshot()
            await asyncio.sleep(.005)
    @asynccontextmanager
    async def lifespan(app):
        async with original(app):
            task=asyncio.create_task(observe())
            try:yield
            finally:
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
                snapshot()
    app.router.lifespan_context=lifespan
    return app
'''


def body(label):
    return {'model':'general-primary','prompt':{'name':'basic_text','version':'1.0.0'},
            'messages':[{'role':'user','content':label}],'timeout_seconds':3}


def events(response):
    frame=[]
    for line in response.iter_lines():
        if line:
            frame.append(line)
        elif frame:
            fields=dict(line.split(': ',1) for line in frame)
            assert set(fields) in ({'event','data'},{'id','event','data'})
            yield fields['event'],json.loads(fields['data'])
            frame=[]
    assert not frame


@contextmanager
def server(runtime_dir):
    (runtime_dir/'src/f5_acceptance.py').write_text(BOOTSTRAP)
    with socket.socket() as listener,tempfile.TemporaryFile(mode='w+') as log:
        listener.bind(('127.0.0.1',0));listener.listen()
        port=listener.getsockname()[1]
        process=subprocess.Popen([sys.executable,'-m','uvicorn','--app-dir',str(runtime_dir/'src'),
            'f5_acceptance:create_app','--factory','--workers','1','--fd',str(listener.fileno()),'--log-level','warning'],
            cwd=runtime_dir/'unrelated-cwd',env={'PATH':os.defpath},pass_fds=(listener.fileno(),),stdout=log,stderr=subprocess.STDOUT)
        try:
            with httpx.Client(headers=HEADERS, base_url=f'http://127.0.0.1:{port}',timeout=3,trust_env=False) as client:
                deadline=time.monotonic()+10
                while True:
                    try:
                        assert client.get('/healthz').status_code==200
                        break
                    except httpx.TransportError:
                        if process.poll() is not None or time.monotonic()>deadline:
                            log.seek(0);pytest.fail(log.read())
                        time.sleep(.01)
                yield client
        finally:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)


def create_stream(client, payload):
    response=client.post('/v1/llm/stream',json=payload)
    assert response.status_code==202,response.text
    rid=response.json()['data']['request_id']
    return f'/v1/llm/stream/{rid}/events'


def next_text(iterator):
    for name,data in iterator:
        if name!='response.heartbeat':return name,data
    pytest.fail('Stream ended without text')


def observe(runtime_dir,predicate):
    deadline=time.monotonic()+4
    while time.monotonic()<deadline:
        path=runtime_dir/'snapshot.json'
        if path.exists():
            data=json.loads(path.read_text())
            if predicate(data):return data
        time.sleep(.01)
    pytest.fail('Controlled stream observation did not reach expected state')


def test_first_delta_arrives_before_provider_can_finish(runtime_dir):
    with server(runtime_dir) as client:
        with client.stream('GET',create_stream(client,body('gated'))) as response:
            assert response.status_code==200
            assert response.headers['content-type'].startswith('text/event-stream')
            request_id=response.headers['x-request-id']
            iterator=events(response)
            name,data=next_text(iterator)
            assert name=='response.delta' and data=={'request_id':request_id,'delta':'gated'}
            snapshot=observe(runtime_dir,lambda d:request_id in d['requests'])
            assert snapshot['requests'][request_id]['status']=='running'
            assert snapshot['closed']==[]
            (runtime_dir/'release-gated').touch()
            remaining=list(iterator)
            assert remaining[-1]==('response.completed',{'request_id':request_id,'usage':None})
            assert all(name=='response.heartbeat' for name,_ in remaining[:-1])
        snapshot=observe(runtime_dir,lambda d:d['requests'][request_id]['status']=='completed')
        assert snapshot['calls']==snapshot['closed']==['gated']
        assert snapshot['client_closed']==0
        assert snapshot['requests'][request_id]['ttft_ms'] is not None


@pytest.mark.parametrize('label',['before','after'])
def test_disconnect_leaves_generation_until_its_deadline(runtime_dir,label):
    with server(runtime_dir) as client:
        with client.stream('GET',create_stream(client,body(label))) as response:
            assert response.status_code==200
            request_id=response.headers['x-request-id']
            name,data=(next(events(response)) if label=='before' else next_text(events(response)))
            assert name==('response.heartbeat' if label=='before' else 'response.delta')
            assert data['request_id']==request_id
        snapshot=observe(runtime_dir,lambda d:label in d['closed'] and d['requests'][request_id]['status']=='failed')
        trace=snapshot['requests'][request_id]
        assert trace['error_code']=='request_timeout'
        assert (trace['ttft_ms'] is None)==(label=='before')
        assert snapshot['closed'].count(label)==1 and snapshot['client_closed']==0
        response=client.get(create_stream(client,body('healthy')))
        assert list(events(response))[-1][0]=='response.completed'


def test_provider_failure_remains_sse_failure(runtime_dir):
    with server(runtime_dir) as client:
        response=client.get(create_stream(client,body('failed')))
        assert response.status_code==200
        received=list(events(response))
        assert [name for name,_ in received if name!='response.heartbeat']==['response.delta','response.failed']
        assert received[-1][1]['code']=='provider_unavailable'
        request_id=response.headers['x-request-id']
        snapshot=observe(runtime_dir,lambda d:d['requests'][request_id]['status']=='failed')
        assert snapshot['closed']==['failed'] and snapshot['client_closed']==0


def test_concurrent_stream_survives_other_disconnect(runtime_dir):
    with server(runtime_dir) as client:
        with client.stream('GET',create_stream(client,body('concurrent-a'))) as response_a:
            id_a=response_a.headers['x-request-id']
            assert next_text(events(response_a))[0]=='response.delta'
            response_b=client.get(create_stream(client,body('concurrent-b')))
            id_b=response_b.headers['x-request-id']
            received_b=list(events(response_b))
            assert next(data for name,data in received_b if name=='response.delta')=={'request_id':id_b,'delta':'concurrent-b'}
            assert received_b[-1][0]=='response.completed'
        snapshot=observe(runtime_dir,lambda d:d['requests'][id_a]['status']=='running' and d['requests'][id_b]['status']=='completed')
        (runtime_dir/'release-concurrent-a').touch()
        snapshot=observe(runtime_dir,lambda d:d['requests'][id_a]['status']=='completed')
        assert snapshot['closed'].count('concurrent-a')==snapshot['closed'].count('concurrent-b')==1
        assert snapshot['client_closed']==0
        attempts={item['request_id']:item for item in snapshot['attempts']}
        assert attempts[id_a]['status']==attempts[id_b]['status']=='completed'


def test_total_deadline_stops_heartbeats_and_waiting_provider(runtime_dir):
    with server(runtime_dir) as client:
        received=[]
        with client.stream('GET',create_stream(client,{**body('before'),'timeout_seconds':1})) as response:
            assert response.status_code==200
            request_id=response.headers['x-request-id']
            try:
                received.extend(events(response))
            except httpx.TransportError:
                # At deadline the server may be unable to write a terminal/body end.
                pass
        snapshot=observe(runtime_dir,lambda d:d['requests'][request_id]['status']=='failed' and 'before' in d['closed'])
        trace=snapshot['requests'][request_id]
        assert trace['error_code']=='request_timeout'
        assert trace['ttft_ms'] is None and trace['generation_duration_ms'] is None
        assert snapshot['closed'].count('before')==1 and snapshot['client_closed']==0
        assert any(name=='response.heartbeat' for name,_ in received)
        assert all(name!='response.completed' for name,_ in received)
        terminal=[data for name,data in received if name=='response.failed']
        assert len(terminal)<=1
        if terminal:assert terminal[0]['code']=='request_timeout'
