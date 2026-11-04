"""F5-O2 contracts observed through HTTP and independent committed PG reads."""
import asyncio
import json
import time
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from f4_cases import registry
from main import create_app
from postgres_support import PostgresServer, write_config, migrate
from providers.base import ProviderStreamEvent
from providers.mock import MockProvider
from settings import Settings
from contracts import Usage


@pytest.fixture(scope='module')
def server():
    value = PostgresServer().start()
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def case(server, tmp_path):
    name = server.create_database()
    config = write_config(tmp_path/'config', server, name, registry())
    migrate(config)
    try:
        yield name, config
    finally:
        server.drop_database(name)


def body(**changes):
    value = {'model':'general-primary','prompt':{'name':'basic_text','version':'1.0.0'},
             'messages':[{'role':'user','content':'订单 A123'}]}
    return value | changes


def make_app(config, provider=None, models=None):
    return create_app(settings=Settings(env='test', _env_file=None), config_dir=config,
                      model_registry=models or registry(), provider=provider or MockProvider())


def observe(server, name, rid):
    with server.connect(name, row_factory=dict_row) as conn:
        row = conn.execute('SELECT * FROM tb_llm_request WHERE request_id=%s', (rid,)).fetchone()
        if row is None:
            return None, [], []
        attempts = conn.execute('SELECT * FROM tb_llm_attempt WHERE request_pk=%s ORDER BY attempt_no', (row['id'],)).fetchall()
        events = conn.execute('SELECT * FROM tb_llm_stream_event WHERE request_pk=%s ORDER BY seq', (row['id'],)).fetchall()
        return row, attempts, events


def frames(text):
    values=[]
    for frame in text.split('\n\n'):
        fields=dict(line.split(': ',1) for line in frame.splitlines() if ': ' in line)
        if fields.get('event'):
            values.append((int(fields['id']) if 'id' in fields else None, fields['event'],json.loads(fields['data'])))
    return values


def test_create_is_pending_without_provider_or_deadline(server,case):
    name,config=case; provider=MockProvider()
    with TestClient(make_app(config,provider)) as client:
        response=client.post('/v1/llm/stream',json=body())
        assert response.status_code==202,response.text
        rid=response.headers['x-request-id']
        assert response.json()=={'success':True,'code':'ok','data':{'request_id':rid,'status':'pending'},'message':'请求已创建'}
        time.sleep(.15)
        row,attempts,events=observe(server,name,rid)
        assert row['status']=='pending' and row['last_event_seq']==0
        assert row['started_at'] is row['deadline_at'] is None
        assert row['input_snapshot'] is not None and attempts==events==[]
        assert provider.calls==provider.stream_calls==[]


def test_subscribe_and_replay_committed_sequence(server,case):
    name,config=case
    provider=MockProvider(stream_events=[ProviderStreamEvent(delta='订单 '),ProviderStreamEvent(delta='A123'),
        ProviderStreamEvent(delta='\n 已签收。'),ProviderStreamEvent(status='completed',upstream_terminal_received=True,
          usage=Usage(input_tokens=10,output_tokens=3,total_tokens=13))])
    with TestClient(make_app(config,provider)) as client:
        created=client.post('/v1/llm/stream',json=body());assert created.status_code==202
        rid=created.json()['data']['request_id'];url=f'/v1/llm/stream/{rid}/events'
        response=client.get(url)
        assert response.status_code==200 and response.headers['x-request-id']==rid
        items=frames(response.text);assert [f[0] for f in items]==[1,2,3,4]
        assert [f[1] for f in items]==['response.delta']*3+['response.completed']
        assert ''.join(f[2]['delta'] for f in items[:-1])=='订单 A123\n 已签收。'
        assert items[-1][2]['usage']=={'input_tokens':10,'output_tokens':3,'total_tokens':13}
        row,attempts,events=observe(server,name,rid)
        assert row['status']=='completed' and len(attempts)==1 and len(provider.stream_calls)==1
        assert [(e['seq'],e['event_type'],json.loads(e['payload'])) for e in events]==items
        replay=client.get(url+'?after_seq=2',headers={'Last-Event-ID':'4'})
        assert frames(replay.text)==items[2:]
        assert frames(client.get(url,headers={'Last-Event-ID':'4'}).text)==items
        assert client.get(url+'?after_seq=4').status_code==204
        assert client.get(url+'?after_seq=5').json()['code']=='invalid_event_cursor'
        assert len(provider.stream_calls)==1


@pytest.mark.parametrize('query',['after_seq=','after_seq=-1','after_seq=1.1','after_seq=true',
    'after_seq=no','after_seq=0&after_seq=0','other=1'])
def test_invalid_cursor_never_starts(server,case,query):
    name,config=case;provider=MockProvider()
    with TestClient(make_app(config,provider)) as client:
        result=client.post('/v1/llm/stream',json=body()); assert result.status_code==202
        rid=result.json()['data']['request_id']
        response=client.get(f'/v1/llm/stream/{rid}/events?'+query)
        assert response.status_code==422 and response.json()['code']=='invalid_request'
        assert observe(server,name,rid)[0]['status']=='pending' and provider.stream_calls==[]


def test_not_found_wrong_kind_future_and_bad_uuid(server,case):
    name,config=case;provider=MockProvider()
    with TestClient(make_app(config,provider)) as client:
        ordinary=client.post('/v1/llm',json=body()).headers['x-request-id']
        rejected=client.post('/v1/llm/stream',json=body(model='missing')).headers['x-request-id']
        for rid in [str(uuid4()),ordinary,rejected]:
            response=client.get(f'/v1/llm/stream/{rid}/events')
            assert response.status_code==404 and response.json()['code']=='not_found'
        assert client.get('/v1/llm/stream/not-uuid/events').status_code==422
        created=client.post('/v1/llm/stream',json=body());assert created.status_code==202
        rid=created.json()['data']['request_id']
        response=client.get(f'/v1/llm/stream/{rid}/events?after_seq=1')
        assert response.status_code==400 and response.json()['code']=='invalid_event_cursor'
        assert provider.stream_calls==[] and observe(server,name,rid)[0]['status']=='pending'


async def test_twenty_concurrent_subscriptions_generate_once(server,case):
    name,config=case;provider=MockProvider(delay=.15);app=make_app(config,provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            created=await client.post('/v1/llm/stream',json=body());assert created.status_code==202
            rid=created.json()['data']['request_id'];url=f'/v1/llm/stream/{rid}/events'
            results=await asyncio.gather(*(client.get(url) for _ in range(20)))
            assert all(r.status_code==200 for r in results)
            assert all(frames(r.text)==frames(results[0].text) for r in results)
            assert len(provider.stream_calls)==1
            row,attempts,events=observe(server,name,rid)
            assert row['status']=='completed' and len(attempts)==1 and len(events)==2


def test_pending_snapshot_survives_restart_and_config_drift(server,case):
    name,config=case;first=MockProvider()
    with TestClient(make_app(config,first)) as client:
        created=client.post('/v1/llm/stream',json=body());assert created.status_code==202
        rid=created.json()['data']['request_id']
    assert first.stream_calls==[]
    provider=MockProvider();app=make_app(config,provider,registry({'general-primary':{'default_output_tokens':512}}))
    with TestClient(app) as client:
        response=client.get(f'/v1/llm/stream/{rid}/events')
        assert response.status_code==200
        assert frames(response.text)[-1][2]['code']=='execution_configuration_changed'
        row,attempts,events=observe(server,name,rid)
        assert row['status']=='failed' and attempts==[] and provider.stream_calls==[]
