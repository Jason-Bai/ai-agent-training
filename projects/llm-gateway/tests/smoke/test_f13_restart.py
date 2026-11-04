"""C07: owned OS-worker crash recovery at both automatic-routing phases.

Only the typed Provider is scripted. Real HTTP, routing, persistence, recovery,
and process death are exercised; no real supplier or secret is involved.
"""
import asyncio
from decimal import Decimal
import json

import httpx
import pytest
import pytest_asyncio
import yaml

from f10_support import Environment, CredentialHeaders
from f12_cases import request
from f13_cases import registry
from postgres_support import PostgresServer, write_config, migrate


BOOTSTRAP = '''
import asyncio, json, os
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from contracts import Usage
from main import create_app as build
from providers.base import ProviderResult, ProviderStreamEvent
from services.classification import CLASSIFIER_TEMPLATE
from settings import Settings
ROOT=Path(__file__).parent
request_id=ContextVar('restart_request',default=None)
def record(value):
    with (ROOT/'calls.jsonl').open('a') as output:
        output.write(json.dumps(value)+'\\n')
        output.flush()
        os.fsync(output.fileno())
class RestartProvider:
    async def invoke(self,model,messages,transport,options):
        phase='classification' if messages[0].content==CLASSIFIER_TEMPLATE.system_template else 'generation'
        rid=request_id.get()
        assert rid is not None
        if phase=='classification':
            material=json.loads(messages[-1].content)
            assert material['candidate_groups'] and material['task_material']['messages']
            assert options['response_schema']==material['output_contract']['schema']
        record(dict(request_id=rid,phase=phase,pid=os.getpid(),transport=transport))
        gate=ROOT/'hold-phase'
        if gate.exists() and gate.read_text()==phase:
            staging=ROOT/'held.tmp'
            staging.write_text(json.dumps(dict(request_id=rid,phase=phase,pid=os.getpid())))
            staging.replace(ROOT/'held.json')
            await asyncio.Event().wait()
        text='{"group":"general"}' if phase=='classification' else '订单A123的商品是充电器。'
        return ProviderResult(text=text,usage=Usage(input_tokens=10,output_tokens=8,total_tokens=18))
    async def complete(self,model,messages,**options):
        return await self.invoke(model,messages,'complete',options)
    async def stream(self,model,messages,**options):
        result=await self.invoke(model,messages,'stream',options)
        yield ProviderStreamEvent(delta=result.text)
        yield ProviderStreamEvent(status='completed',usage=result.usage,upstream_terminal_received=True)
    async def aclose(self):pass
def create_app():
    app=build(settings=Settings(env='test',_env_file=None),config_dir=ROOT/'config',provider=RestartProvider())
    begin=app.state.storage.begin_attempt
    async def observed_begin(trace,attempt,**options):
        await begin(trace,attempt,**options)
        request_id.set(trace.request_id)
    app.state.storage.begin_attempt=observed_begin
    original=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(app):
        async with original(app):
            async def observe():
                while True:
                    value=app.state.concurrency_limiter.snapshot()
                    staging=ROOT/'permits.tmp';staging.write_text(json.dumps(value))
                    staging.replace(ROOT/'permits.json')
                    await asyncio.sleep(.01)
            task=asyncio.create_task(observe())
            try:yield
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True)
    app.router.lifespan_context=lifespan
    return app
'''


@pytest_asyncio.fixture
async def process(tmp_path):
    pg = await asyncio.to_thread(lambda: PostgresServer().start())
    name = pg.create_database(); env = Environment(pg,name,tmp_path,backend='mock')
    try:
        definition = registry(); write_config(env.config,pg,name,definition)
        path = env.config/'gateway.yaml'; document = yaml.safe_load(path.read_text())
        document['auto_routes'] = {key:value.model_dump(mode='json',exclude_none=True)
                                   for key,value in definition.auto_routes.items()}
        path.write_text(yaml.safe_dump(document))
        (tmp_path/'f10_bootstrap.py').write_text(BOOTSTRAP)
        await asyncio.to_thread(migrate,env.config)
        await env.cli('create-agent','a','F13 owned process recovery')
        token = await env.cli('issue','a')
        env.headers = CredentialHeaders(Authorization='Bearer '+token)
        await env.cli('deposit','a','10000','--operation-id','f13-restart-initial')
        await env.start()
        yield env
    finally:
        await env.stop()
        if env.listener is not None: env.listener.close()
        await asyncio.to_thread(pg.drop_database,name)
        await asyncio.to_thread(pg.close)
        # PostgresServer.close removes the owned volume; its generated local
        # password file also belongs to this fixture and must not be retained.
        pg.password_path.unlink(missing_ok=True)


async def wait_file(path):
    async with asyncio.timeout(6):
        while not path.exists(): await asyncio.sleep(.01)
    return json.loads(path.read_text())


def calls(env):
    path = env.directory/'calls.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def pending(env):
    reply = await env.client.post('/v1/llm/stream',json=request(
        streaming=True,model='auto-general').model_dump(exclude_none=True))
    assert reply.status_code == 202,reply.text
    return reply.json()['data']['request_id']


async def trace(env,rid):
    reply = await env.client.get('/v1/traces/'+rid)
    assert reply.status_code == 200,reply.text
    return reply.json()['data']


async def billing(env,rid):
    reply = await env.client.get('/v1/billing/requests/'+rid)
    assert reply.status_code == 200,reply.text
    return reply.json()['data']


def rows(env,rid):
    with env.pg.connect(env.name) as sql:
        pk,status,metadata = sql.execute('SELECT id,status,timing_snapshot FROM tb_llm_request WHERE request_id=%s',(rid,)).fetchone()
        attempts = sql.execute('SELECT attempt_no,status,error_code,input_tokens,output_tokens,total_tokens FROM tb_llm_attempt WHERE request_pk=%s ORDER BY attempt_no',(pk,)).fetchall()
        events = sql.execute('SELECT seq,event_type,payload FROM tb_llm_stream_event WHERE request_pk=%s ORDER BY seq',(pk,)).fetchall()
        charges = sql.execute('SELECT attempt_no,state,agent_charge FROM tb_billing_attempt WHERE request_pk=%s ORDER BY attempt_no',(pk,)).fetchall()
    return dict(status=status,metadata=json.loads(metadata),attempts=attempts,events=events,charges=charges)


async def no_permits(env):
    async with asyncio.timeout(3):
        while True:
            value = await wait_file(env.directory/'permits.json')
            if value == {'active_requests':0,'agents':{}}: return
            await asyncio.sleep(.01)


def assert_unique_terminal(saved,kind):
    assert [event[0] for event in saved['events']] == list(range(1,len(saved['events'])+1))
    assert [event[1] for event in saved['events'] if event[1] in {
        'response.completed','response.failed','response.canceled'}] == ['response.'+kind]


@pytest.mark.parametrize('phase',['classification','generation'])
async def test_real_worker_kill_recovers_original_auto_request_without_reexecution(process,phase):
    env = process; (env.directory/'hold-phase').write_text(phase)
    rid = await pending(env); first_pid = env.process.pid
    url = '/v1/llm/stream/'+rid+'/events'
    reader = asyncio.create_task(env.client.get(url))
    try:
        held = await wait_file(env.directory/'held.json')
        assert held == dict(request_id=rid,phase=phase,pid=first_pid)
        old_calls = calls(env)
        expected = ['classification'] if phase == 'classification' else ['classification','generation']
        assert [item['phase'] for item in old_calls] == expected
        assert all(item['request_id'] == rid and item['pid'] == first_pid for item in old_calls)
        before = rows(env,rid)
        assert before['status'] == 'running' and len(before['attempts']) == len(expected)
        assert before['attempts'][-1][3:] == (None,None,None)
        env.process.kill()
        await asyncio.to_thread(env.process.wait,timeout=5)
        assert env.process.returncode != 0
        try:
            await asyncio.wait_for(reader,5)
        except httpx.TransportError:
            pass  # The interrupted client is not an alternate failure signal.
        await env.stop()
        await env.start(); assert env.process.pid != first_pid
        recovered = await trace(env,rid); saved = rows(env,rid)
        assert recovered['status'] == 'failed' and recovered['error_code'] == 'execution_interrupted'
        assert [item['purpose'] for item in recovered['attempts']] == expected
        assert recovered['attempts'][-1]['usage'] is None
        assert saved['attempts'][-1][1:3] == ('failed','execution_interrupted')
        assert saved['attempts'][-1][3:] == (None,None,None)
        if phase == 'generation':
            assert saved['attempts'][0][1] == 'completed'
            assert recovered['attempts'][0]['usage'] == {'input_tokens':10,'output_tokens':8,'total_tokens':18}
            assert recovered['auto_routing']['resolved_group'] == 'general'
        assert_unique_terminal(saved,'failed')
        assert len(saved['charges']) == len(expected)
        assert all(state == 'settled' and cost == 0 for _,state,cost in saved['charges'])
        bill = await billing(env,rid); assert Decimal(bill['charged']) == 0
        replay = await env.client.get(url)
        assert replay.status_code == 200 and 'execution_interrupted' in replay.text
        assert 'response.completed' not in replay.text and 'response.delta' not in replay.text
        assert (await env.client.get(url,params={'after_seq':saved['events'][-1][0]})).status_code == 204
        assert calls(env) == old_calls and rows(env,rid) == saved and await billing(env,rid) == bill
        await no_permits(env)
        (env.directory/'restart-proof.json').write_text(json.dumps(dict(
            request_id=rid,old_pid=first_pid,new_pid=env.process.pid,phase=phase,
            calls=old_calls,trace=recovered,billing=bill),ensure_ascii=False))
    finally:
        if not reader.done(): reader.cancel()
        await asyncio.gather(reader,return_exceptions=True)


async def test_pending_survives_worker_restart_and_first_subscriber_executes_two_phases_once(process):
    env = process; rid = await pending(env); first_pid = env.process.pid
    before = rows(env,rid)
    assert before['status'] == 'pending' and not before['attempts'] and not before['charges'] and not calls(env)
    await no_permits(env)
    await env.stop(); await env.start(); assert env.process.pid != first_pid
    assert (await trace(env,rid))['status'] == 'pending' and rows(env,rid) == before and not calls(env)
    url = '/v1/llm/stream/'+rid+'/events'
    reply = await env.client.get(url)
    assert reply.status_code == 200 and 'response.completed' in reply.text,reply.text
    data = await trace(env,rid); saved = rows(env,rid); bill = await billing(env,rid)
    assert data['status'] == 'completed' and data['auto_routing']['resolved_group'] == 'general'
    assert [item['purpose'] for item in data['attempts']] == ['classification','generation']
    assert [item['phase'] for item in calls(env)] == ['classification','generation']
    assert all(item['pid'] == env.process.pid and item['request_id'] == rid for item in calls(env))
    assert_unique_terminal(saved,'completed')
    assert len(saved['attempts']) == len(saved['charges']) == 2
    assert Decimal(bill['charged']) == 0
    old_calls = calls(env)
    assert (await env.client.get(url,params={'after_seq':0})).text == reply.text
    assert (await env.client.get(url,params={'after_seq':saved['events'][-1][0]})).status_code == 204
    assert calls(env) == old_calls and rows(env,rid) == saved and await billing(env,rid) == bill
    await no_permits(env)
