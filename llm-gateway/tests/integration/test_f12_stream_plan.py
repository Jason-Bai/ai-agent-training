"""F12 PG acceptance: route snapshots, no-call rejection, restart and readonly replay."""
from dataclasses import replace
import json

from fastapi.testclient import TestClient
import pytest
import yaml

from f12_cases import registry, request
from legacy_identity import HEADERS, provision
from main import create_app
from postgres_support import PostgresServer, migrate
from providers.mock import MockProvider
from settings import Settings
from services.models import RecoveryPolicy


@pytest.fixture(scope='module')
def f12_pg():
    server=PostgresServer().start()
    try:yield server
    finally:server.close()


@pytest.fixture
def f12_case(f12_pg,tmp_path):
    name=f12_pg.create_database();definition=registry()
    config=tmp_path/'f12-config';config.mkdir()
    document={'providers':{n:v.model_dump(exclude_none=True) for n,v in definition.providers.items()},
        'endpoints':{n:v.model_dump(mode='json') for n,v in definition.endpoints.items()},
        'model_groups':{n:v.model_dump(mode='json') for n,v in definition.model_groups.items()},
        'concurrency':definition.concurrency.model_dump(),'recovery':definition.recovery.model_dump(),
        'database':f12_pg.config(name)}
    (config/'gateway.yaml').write_text(yaml.safe_dump(document))
    migrate(config);provision(config)
    try:yield name,config,definition
    finally:f12_pg.drop_database(name)


def app(config,definition,a=None,b=None):
    return create_app(settings=Settings(env='test',_env_file=None),config_dir=config,
        model_registry=definition,
        providers={'mock-main':a or MockProvider(text='A123 primary'),
                   'mock-backup':b or MockProvider(text='A123 backup')})


def pending(client):
    response=client.post('/v1/llm/stream',json=request(streaming=True).model_dump(exclude_none=True))
    assert response.status_code==202,response.text
    return response.json()['data']['request_id']


def trace(client,rid):
    response=client.get('/v1/traces/'+rid);assert response.status_code==200,response.text
    return response.json()['data']


def rows(pg,name,rid):
    with pg.connect(name) as sql:
        r=sql.execute('SELECT id,status,last_event_seq,input_snapshot FROM tb_llm_request WHERE request_id=%s',(rid,)).fetchone()
        attempts=sql.execute('SELECT model,status FROM tb_llm_attempt WHERE request_pk=%s ORDER BY attempt_no',(r[0],)).fetchall()
        events=sql.execute('SELECT seq,event_type,payload FROM tb_llm_stream_event WHERE request_pk=%s ORDER BY seq',(r[0],)).fetchall()
        bills=sql.execute('SELECT state FROM tb_billing_attempt WHERE request_pk=%s',(r[0],)).fetchall()
    return r,attempts,events,bills


def test_pending_route_is_persisted_without_attempt_or_charge(f12_case,f12_pg):
    name,config,definition=f12_case;a=MockProvider();b=MockProvider()
    with TestClient(app(config,definition,a,b),headers=HEADERS) as client:
        rid=pending(client);data=trace(client,rid)
        assert data['status']=='pending' and data['attempts']==[]
        assert data['routing']['selected_endpoint']=='chat-a'
        row,attempts,events,bills=rows(f12_pg,name,rid)
        assert row[1]=='pending' and not attempts and not events and not bills
        assert not a.stream_calls and not b.stream_calls
        snapshot=json.loads(row[3])
        assert snapshot['routing']==data['routing']


def test_resume_reads_committed_plan_without_new_generation_or_billing(f12_case,f12_pg):
    name,config,definition=f12_case;a=MockProvider(text='订单A123充电器');b=MockProvider()
    application=app(config,definition,a,b)
    with TestClient(application,headers=HEADERS) as client:
        rid=pending(client);url=f'/v1/llm/stream/{rid}/events'
        initial=client.get(url);assert initial.status_code==200
        assert 'response.completed' in initial.text
        before=rows(f12_pg,name,rid)
        assert len(before[1])==1 and before[1][0][0]=='chat-a'
        assert client.get(url,params={'after_seq':0}).text==initial.text
        assert client.get(url,params={'after_seq':before[0][2]}).status_code==204
        assert rows(f12_pg,name,rid)==before
        assert len(a.stream_calls)==1 and not b.stream_calls
        assert application.state.concurrency_limiter.snapshot()['active_requests']==0


@pytest.mark.parametrize('change',['same','unrelated'])
@pytest.mark.parametrize('production',[False,True],ids=['zero','production'])
def test_application_restart_preserves_equivalent_pending_plan(f12_case,f12_pg,change,production):
    name,config,definition=f12_case
    if production:definition=replace(definition,recovery=RecoveryPolicy())
    with TestClient(app(config,definition),headers=HEADERS) as client:rid=pending(client)
    if change=='unrelated':
        definition=registry(extra_groups={'unused':{'candidates':['responses-b']}},policy=definition.recovery)
    a,b=MockProvider(),MockProvider()
    with TestClient(app(config,definition,a,b),headers=HEADERS) as client:
        result=client.get(f'/v1/llm/stream/{rid}/events')
        assert 'response.completed' in result.text,result.text
        assert trace(client,rid)['status']=='completed'
        assert len(a.stream_calls)==1 and not b.stream_calls


@pytest.mark.parametrize('change',['order','disabled','budget','excluded_changed','recovery'])
def test_related_configuration_change_fails_without_call(f12_case,f12_pg,change):
    name,config,definition=f12_case
    if change=='excluded_changed':definition=registry({'responses-b':{'enabled':False}})
    with TestClient(app(config,definition),headers=HEADERS) as client:rid=pending(client)
    if change=='order':updated=registry(order=('responses-b','chat-a'))
    elif change=='disabled':updated=registry({'chat-a':{'enabled':False}})
    elif change=='budget':updated=registry(group_changes={'max_output_tokens':2048})
    elif change=='excluded_changed':updated=registry()
    else:updated=replace(definition,recovery=definition.recovery.model_copy(update={'max_attempts':2}))
    a,b=MockProvider(),MockProvider()
    with TestClient(app(config,updated,a,b),headers=HEADERS) as client:
        response=client.get(f'/v1/llm/stream/{rid}/events')
        assert 'execution_configuration_changed' in response.text,response.text
        data=trace(client,rid)
        assert data['status']=='failed' and data['error_code']=='execution_configuration_changed'
        assert not a.stream_calls and not b.stream_calls and data['attempts']==[]
        row,attempts,events,bills=rows(f12_pg,name,rid)
        assert not attempts and not bills
        assert [e[1] for e in events]==['response.failed']


def test_pre_versioned_pending_is_rejected_but_event_history_remains_readable(f12_case,f12_pg):
    name,config,definition=f12_case
    with TestClient(app(config,definition),headers=HEADERS) as client:rid=pending(client)
    row,_,_,_=rows(f12_pg,name,rid);snapshot=json.loads(row[3])
    for key in ['snapshot_version','routing_configuration','routing']:snapshot.pop(key,None)
    with f12_pg.connect(name) as sql:
        sql.execute('UPDATE tb_llm_request SET input_snapshot=%s WHERE request_id=%s',(json.dumps(snapshot),rid))
    a,b=MockProvider(),MockProvider()
    with TestClient(app(config,definition,a,b),headers=HEADERS) as client:
        url=f'/v1/llm/stream/{rid}/events'
        first=client.get(url)
        assert 'execution_configuration_changed' in first.text
        assert client.get(url).text==first.text
        assert client.get(url,params={'after_seq':1}).status_code==204
        assert not a.stream_calls and not b.stream_calls


@pytest.mark.parametrize('field,value',[('model','chat-a'),('provider','mock-main'),('capabilities',['streaming'])])
def test_public_rejection_has_no_attempt_bill_or_input_snapshot(f12_case,f12_pg,field,value):
    name,config,definition=f12_case;a,b=MockProvider(),MockProvider()
    with TestClient(app(config,definition,a,b),headers=HEADERS) as client:
        body=request().model_dump(exclude_none=True);body[field]=value
        result=client.post('/v1/llm',json=body)
        assert result.status_code in (400,422),result.text
        rid=result.headers['x-request-id']
        row,attempts,events,bills=rows(f12_pg,name,rid)
        assert row[3] is None and not attempts and not events and not bills
        assert not a.calls and not b.calls
