"""Real ASGI send boundaries must not own persisted generation results."""
import asyncio
import json
import logging
from time import monotonic

from legacy_identity import ASGI_AUTH
import pytest
import httpx

from test_f5o2_http import server, case, make_app, body, observe, frames
from providers.mock import MockProvider
from providers.base import ProviderStreamEvent
from services.subscriptions import EventSubscription


async def invoke(app, method, path, send, *, payload=None, query=b'', disconnect=None):
    delivered=False
    async def receive():
        nonlocal delivered
        if not delivered:
            delivered=True
            return {'type':'http.request','body':json.dumps(payload).encode() if payload else b'','more_body':False}
        await (disconnect or asyncio.Event()).wait()
        return {'type':'http.disconnect'}
    scope={'type':'http','asgi':{'version':'3.0'},'method':method,'path':path,'raw_path':path.encode(),
           'query_string':query,'headers':[ASGI_AUTH, (b'content-type',b'application/json')],
           'scheme':'http','server':('test',80),'client':('test',1),'http_version':'1.1'}
    await app(scope,receive,send)


@pytest.mark.parametrize('position',['headers','delta','heartbeat','terminal','eof'])
@pytest.mark.parametrize('mode',['block','error'])
async def test_each_send_failure_is_local_to_subscription(server,case,monkeypatch,position,mode,caplog):
    name,config=case
    original=EventSubscription.__init__
    def quick(self,*args,**kwargs):
        original(self,*args,**kwargs)
        self.send_timeout_seconds=.025;self.heartbeat_seconds=.01;self.poll_seconds=.005
    monkeypatch.setattr(EventSubscription,'__init__',quick)
    release=asyncio.Event()
    async def gate():
        await release.wait()
    provider=MockProvider(stream_events=[ProviderStreamEvent(delta='A123'),gate,
        ProviderStreamEvent(status='completed',upstream_terminal_received=True)])
    app=make_app(config,provider);sent=[];hit=False
    caplog.set_level(logging.WARNING)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            created=await client.post('/v1/llm/stream',json=body());rid=created.json()['data']['request_id']
            async def send(message):
                nonlocal hit
                text=message.get('body',b'').decode()
                target=(position=='headers' and message['type']=='http.response.start' or
                        position=='delta' and 'event: response.delta' in text or
                        position=='heartbeat' and 'event: response.heartbeat' in text or
                        position=='terminal' and 'event: response.completed' in text or
                        position=='eof' and message['type']=='http.response.body' and not message.get('more_body',False))
                if position in {'terminal','eof'} and 'event: response.delta' in text:
                    release.set()
                if target:
                    hit=True
                    if mode=='block':await asyncio.Event().wait()
                    raise OSError('PRIVATE TRANSPORT DETAILS')
                sent.append(message)
            await asyncio.wait_for(invoke(app,'GET',f'/v1/llm/stream/{rid}/events',send),1)
            assert hit
            release.set()
            deadline=monotonic()+2
            while observe(server,name,rid)[0]['status']!='completed' and monotonic()<deadline:
                await asyncio.sleep(.01)
            row,attempts,events=observe(server,name,rid)
            assert row['status']=='completed' and row['error_code'] is None
            assert len(attempts)==len(provider.stream_calls)==1 and events[-1]['event_type']=='response.completed'
            assert app.state.traces.requests[rid].delivery_error_code is None
            replay=await client.get(f'/v1/llm/stream/{rid}/events')
            assert frames(replay.text)[-1][1]=='response.completed'
            assert 'PRIVATE TRANSPORT DETAILS' not in caplog.text


@pytest.mark.parametrize('position',['headers','body'])
async def test_creation_delivery_failure_preserves_committed_pending(server,case,position):
    name,config=case;provider=MockProvider();app=make_app(config,provider);rid=None
    async with app.router.lifespan_context(app):
        async def send(message):
            nonlocal rid
            if message['type']=='http.response.start':
                rid=dict(message['headers'])[b'x-request-id'].decode()
            if (position=='headers' and message['type']=='http.response.start' or
                    position=='body' and message['type']=='http.response.body'):
                raise OSError('controlled delivery failure')
        with pytest.raises(OSError):
            await invoke(app,'POST','/v1/llm/stream',send,payload=body())
        await asyncio.sleep(1.1)
        row,attempts,events=observe(server,name,rid)
        assert row['status']=='pending' and row['started_at'] is None
        assert attempts==events==[] and provider.stream_calls==[]


async def test_history_replay_does_not_rewrite_original_metrics(server,case):
    name,config=case;app=make_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            rid=(await client.post('/v1/llm/stream',json=body())).json()['data']['request_id']
            url=f'/v1/llm/stream/{rid}/events'
            await client.get(url)
            await asyncio.sleep(1.1)
            original=observe(server,name,rid)[0]['timing_snapshot']
            assert json.loads(original)['ttft_ms'] is not None
            await client.get(url)
            await asyncio.sleep(1.1)
            assert observe(server,name,rid)[0]['timing_snapshot']==original
    app2=make_app(config)
    async with app2.router.lifespan_context(app2):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2),base_url='http://test') as client:
            await client.get(url)
            assert observe(server,name,rid)[0]['timing_snapshot']==original
            assert app2.state.traces.requests=={} and app2.state.traces.attempts==[]


@pytest.mark.parametrize('position',['headers','body'])
async def test_blocked_creation_response_is_not_generation_completion(server,case,position):
    name,config=case;provider=MockProvider();app=make_app(config,provider)
    blocked=asyncio.Event();release=asyncio.Event();rid=None
    async def send(message):
        nonlocal rid
        if message['type']=='http.response.start':
            rid=dict(message['headers'])[b'x-request-id'].decode()
        if (position=='headers' and message['type']=='http.response.start' or
                position=='body' and message['type']=='http.response.body'):
            blocked.set();await release.wait()
    async with app.router.lifespan_context(app):
        post=asyncio.create_task(invoke(app,'POST','/v1/llm/stream',send,payload=body()))
        try:
            await asyncio.wait_for(blocked.wait(),1)
            row,attempts,events=observe(server,name,rid)
            assert row['status']=='pending' and row['started_at'] is None
            assert attempts==events==[] and provider.stream_calls==[]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                response=await client.get(f'/v1/llm/stream/{rid}/events')
                assert frames(response.text)[-1][1]=='response.completed'
            release.set();await asyncio.wait_for(post,1)
            assert observe(server,name,rid)[0]['status']=='completed' and len(provider.stream_calls)==1
        finally:
            release.set()
            if not post.done():post.cancel()
            await asyncio.gather(post,return_exceptions=True)
