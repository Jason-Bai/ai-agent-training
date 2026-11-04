from pricing_support import write_pricing
"""Real Bailian + owned external Gateway/client lifecycle observations.

The gate pauses consumption after a genuine typed delta. It does not prove the
remote supplier is still generating; no upstream result or Usage is replaced.
"""
import asyncio
from decimal import Decimal
import json
import shutil

import pytest
import pytest_asyncio
import yaml

from f13_real_support import (MODELS, f13_live_pg, f13_real_environment,
                              configuration, real_prices, PRICE_VERSION)
from f10_support import Environment, CredentialHeaders, BOOTSTRAP, frames
from f11_support import GATED_BOOTSTRAP, counts, provider_records, release, wait_file
from postgres_support import migrate

pytestmark=pytest.mark.live
TERMINAL={'completed','failed','canceled'}


@pytest_asyncio.fixture
async def bailian_process(f13_real_environment,tmp_path):
    source=f13_real_environment
    pg=source.server;name=pg.create_database()
    directory=tmp_path/'f13-external';directory.mkdir()
    env=Environment(pg,name,directory,backend='real',
        concurrency={'max_requests':20,'max_requests_per_agent':5})
    env.config.mkdir()
    keyfile=env.config/'credentials.yaml'
    try:
        (env.config/'gateway.yaml').write_text(yaml.safe_dump(configuration(pg,name)))
        write_pricing(env.config, real_prices())
        # Runtime fixture-to-fixture copy only; no maintainer binding is read.
        shutil.copyfile(source.directory/'config/credentials.yaml',keyfile)
        keyfile.chmod(0o600)
        (directory/'f10_bootstrap.py').write_text(BOOTSTRAP+GATED_BOOTSTRAP)
        await asyncio.to_thread(migrate,env.config)
        await env.cli('create-agent','a','F13 external acceptance')
        token=await env.cli('issue','a')
        env.headers=CredentialHeaders(Authorization='Bearer '+token)
        await env.cli('deposit','a','10000','--operation-id','f13-external-initial')
        await env.start()
        await wait_file(directory/'f11-counts.json')
        yield env
    finally:
        for path in directory.glob('f11-held-*.json'):
            release(env,json.loads(path.read_text())['request_id'])
        # Preserve safe SQL state before removing the disposable database.
        try:
            with pg.connect(name) as sql:
                rows=sql.execute('SELECT request_id::text,status FROM tb_llm_request ORDER BY id').fetchall()
                attempts=sql.execute('SELECT request_pk,attempt_no,model,status,input_tokens,output_tokens,total_tokens FROM tb_llm_attempt ORDER BY request_pk,attempt_no').fetchall()
                bills=sql.execute('SELECT request_pk,attempt_no,state,agent_charge FROM tb_billing_attempt ORDER BY request_pk,attempt_no').fetchall()
            (directory/'f13-os-final.json').write_text(json.dumps(
                dict(requests=rows,attempts=attempts,billing=bills),default=str))
        finally:
            await env.stop()
            if env.listener is not None:env.listener.close()
            keyfile.unlink(missing_ok=True)
            pg.drop_database(name)


def body(group):
    return dict(model=group,prompt={'name':'basic_text','version':'1.0.0'},
        messages=[{'role':'user','content':'订单A123的商品是充电器。只用一句短句复述订单号和商品，不超过30字。'}],
        max_output_tokens=128,timeout_seconds=60)


async def create(env,group):
    reply=await env.client.post('/v1/llm/stream',json=body(group))
    assert reply.status_code==202,reply.text
    rid=reply.json()['data']['request_id']
    assert reply.headers['x-request-id']==rid and reply.json()['data']['status']=='pending'
    return rid


async def diagnostic(env,rid):
    reply=await env.client.get('/v1/traces/'+rid)
    assert reply.status_code==200,reply.text
    return reply.json()['data']


async def terminal(env,rid):
    async with asyncio.timeout(65):
        while True:
            trace=await diagnostic(env,rid)
            if trace['status'] in TERMINAL:return trace
            await asyncio.sleep(.02)


async def cancel(env,rid):
    async with asyncio.timeout(15):
        while True:
            reply=await env.client.post('/v1/llm/stream/'+rid+'/cancel')
            assert reply.status_code in (200,202),reply.text
            state=reply.json()['data']['status']
            if state in TERMINAL:return state
            assert state=='canceling'
            await asyncio.sleep(.02)


async def detail(env,rid):
    reply=await env.client.get('/v1/billing/requests/'+rid)
    assert reply.status_code==200,reply.text
    return reply.json()['data']


def sql_snapshot(env,rid):
    with env.pg.connect(env.name) as sql:
        pk,status,seq=sql.execute('SELECT id,status,last_event_seq FROM tb_llm_request WHERE request_id=%s',(rid,)).fetchone()
        attempts=sql.execute('SELECT model,status,input_tokens,output_tokens,total_tokens FROM tb_llm_attempt WHERE request_pk=%s ORDER BY attempt_no',(pk,)).fetchall()
        events=sql.execute('SELECT seq,event_type,payload FROM tb_llm_stream_event WHERE request_pk=%s ORDER BY seq',(pk,)).fetchall()
        bills=sql.execute('SELECT state,agent_charge FROM tb_billing_attempt WHERE request_pk=%s ORDER BY attempt_no',(pk,)).fetchall()
    return dict(status=status,last_seq=seq,attempts=attempts,events=events,bills=bills)


def event_assertions(saved,expected_terminal):
    events=saved['events']
    assert [row[0] for row in events]==list(range(1,len(events)+1))
    terminals=[row[1] for row in events if row[1] in {'response.completed','response.failed','response.canceled'}]
    assert terminals==['response.'+expected_terminal]
    return [(seq,kind,json.loads(payload)) for seq,kind,payload in events]


def save(env,rid,trace,billing,extra):
    (env.directory/('f13-os-'+rid+'.json')).write_text(json.dumps(dict(
        request_id=rid,trace=trace,billing=billing,extra=extra,
        scope='external owned one-worker Gateway and real HTTP client; real Bailian adapter; typed-consumption observation gate'),
        ensure_ascii=False,default=str))


@pytest.mark.parametrize('group',list(MODELS))
async def test_real_bailian_pending_cancel_has_zero_upstream_and_charge(bailian_process,group,record_property):
    env=bailian_process;rid=await create(env,group)
    before=sql_snapshot(env,rid)
    assert before['status']=='pending' and not before['attempts'] and not before['bills']
    assert await cancel(env,rid)=='canceled'
    trace=await terminal(env,rid);saved=sql_snapshot(env,rid);billing=await detail(env,rid)
    event_assertions(saved,'canceled')
    assert not saved['attempts'] and not saved['bills'] and trace['attempts']==[]
    assert provider_records(env,rid)==[] and not (env.directory/('availability-'+rid+'.json')).exists()
    assert Decimal(billing['charged'])==0
    await counts(env,{'active_requests':0,'agents':{}})
    save(env,rid,trace,billing,dict(group=group,operation='pending_cancel',upstream_calls=0))
    record_property('f13_request_id',rid)


async def first_delta(env,rid):
    received=[]
    async with env.client.stream('GET','/v1/llm/stream/'+rid+'/events') as reply:
        assert reply.status_code==200
        async for item in frames(reply):
            if item[1]!='response.heartbeat':received.append(item)
            if item[1]=='response.delta':break
    assert received and received[-1][1]=='response.delta'
    await wait_file(env.directory/('f11-held-'+rid+'.json'))
    current=await diagnostic(env,rid)
    assert current['status']=='running' and len(current['attempts'])==1
    assert current['attempts'][0]['usage'] is None
    assert provider_records(env,rid)==['stream_entered']
    await counts(env,{'active_requests':1,'agents':{'a':1}})
    return received


@pytest.mark.parametrize('group',list(MODELS))
async def test_real_bailian_cancel_after_genuine_delta_closes_local_iterator(bailian_process,group,record_property):
    env=bailian_process;(env.directory/'f11-arm').touch();rid=await create(env,group)
    try:
        prefix=await first_delta(env,rid)
        assert await cancel(env,rid)=='canceled'
        trace=await terminal(env,rid);saved=sql_snapshot(env,rid);billing=await detail(env,rid)
        replay=event_assertions(saved,'canceled')
        assert len(saved['attempts'])==len(saved['bills'])==1
        assert saved['attempts'][0][0]==group+'-endpoint'
        assert provider_records(env,rid)==['stream_entered','stream_closed']
        observed=await wait_file(env.directory/('availability-'+rid+'.json'))
        assert len(observed['attempts'])==1 and observed['attempts'][0]['read_closed'] is True
        metering=trace['attempts'][0]['usage']
        if metering is None:
            assert billing['attempts'][0]['state']=='pending'
            assert billing['attempts'][0]['agent_charge'] is None
        else:
            assert billing['attempts'][0]['state'] in {'settled','pending'}
        assert sum(kind=='response.canceling' for _,kind,_ in replay)==1
        await counts(env,{'active_requests':0,'agents':{}})
        save(env,rid,trace,billing,dict(group=group,operation='running_cancel',
            first_seq=prefix[-1][0],local_iterator_closed=True,
            supplier_still_generating_at_gate='not asserted'))
        record_property('f13_request_id',rid)
    finally:release(env,rid)


@pytest.mark.parametrize('group',list(MODELS))
async def test_real_bailian_external_delta_disconnect_background_completion_and_resume(bailian_process,group,record_property):
    env=bailian_process;(env.directory/'f11-arm').touch();rid=await create(env,group)
    try:
        prefix=await first_delta(env,rid)
        # The external client has disconnected and the generator still owns its
        # original permit. Release without resubscribing, then observe completion.
        release(env,rid)
        trace=await terminal(env,rid)
        assert trace['status']=='completed',trace
        before=sql_snapshot(env,rid);billing=await detail(env,rid)
        saved=event_assertions(before,'completed')
        assert len(before['attempts'])==len(before['bills'])==1
        assert trace['attempts'][0]['actual_model']==MODELS[group]
        assert trace['attempts'][0]['usage'] is not None
        assert billing['attempts'][0]['state']=='settled'
        quote=billing['attempts'][0]['quote']
        if isinstance(quote,str):quote=json.loads(quote)
        assert quote['version']==PRICE_VERSION
        tail=[]
        async with env.client.stream('GET','/v1/llm/stream/'+rid+'/events',params={'after_seq':prefix[-1][0]}) as reply:
            assert reply.status_code==200
            async for item in frames(reply):
                if item[1]!='response.heartbeat':tail.append(item)
        assert prefix+tail==saved
        answer=''.join(value['delta'] for _,kind,value in saved if kind=='response.delta')
        assert 'A123' in answer and '充电器' in answer
        assert sql_snapshot(env,rid)==before
        assert await detail(env,rid)==billing
        assert provider_records(env,rid)==['stream_entered','stream_closed']
        assert (await env.client.get('/v1/llm/stream/'+rid+'/events',params={'after_seq':saved[-1][0]})).status_code==204
        await counts(env,{'active_requests':0,'agents':{}})
        save(env,rid,trace,billing,dict(group=group,operation='external_disconnect_resume',
            prefix_seq=prefix[-1][0],events=len(saved),completed_before_resubscription=True,
            proof='real client received delta before Gateway consumed upstream terminal; supplier completion time not asserted'))
        record_property('f13_request_id',rid)
    finally:release(env,rid)
