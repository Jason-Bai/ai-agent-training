"""F7 independent TCP clients, request-owned recovery and disposable PG faults."""
from concurrent.futures import ThreadPoolExecutor
import json
import signal
import time
import pytest
import yaml
from f7_cases import registry
from postgres_support import PostgresServer, write_config, migrate
from test_f5o2_process import gateway, create, events, observations, observe, wait_for

BOOTSTRAP = '''
import asyncio
import json
from pathlib import Path
from main import create_app as build_app
from providers.mock import MockProvider
from providers.base import ProviderStreamEvent
from contracts import Usage
from services.models import load_registry
from settings import Settings
ROOT=Path(__file__).parent

def record(kind,label=''):
    with (ROOT/'observations.jsonl').open('a') as file:
        file.write(json.dumps([kind,label])+'\\n')

async def gate(label):
    record('waiting',label)
    while not (ROOT/('release-'+label)).exists(): await asyncio.sleep(.01)

class ObservedProvider(MockProvider):
    def __init__(self):
        super().__init__(); self.counts={}
    async def stream(self,model,messages,**kwargs):
        label=messages[-1].content
        self.counts[label]=self.counts.get(label,0)+1
        number=self.counts[label]
        record('stream',label)
        try:
            if label=='slow-generation': await asyncio.sleep(11)
            if number==1 and label in {'heartbeat','kill-wait'}:
                yield ProviderStreamEvent(status='failed',error_code='provider_rate_limited',retry_after_seconds=16.0)
                return
            if number==1 and label in {'resume','outage'}:
                yield ProviderStreamEvent(status='failed',error_code='provider_connection_error',
                    usage=Usage(input_tokens=3,output_tokens=2,total_tokens=5))
                return
            yield ProviderStreamEvent(delta=label)
            if label=='resume':
                await gate(label)
                yield ProviderStreamEvent(delta='-remaining')
            yield ProviderStreamEvent(status='completed',upstream_terminal_received=True,
                usage=Usage(input_tokens=2,output_tokens=2,total_tokens=4))
        finally: record('stream_closed',label)

def create_app():
    models=load_registry(ROOT/'config')
    app=build_app(settings=Settings(env='test',_env_file=None),model_registry=models,
        config_dir=ROOT/'config',provider=ObservedProvider())
    original=app.state.storage.finish_attempt
    async def finish(trace,attempt,**kwargs):
        result=await original(trace,attempt,**kwargs)
        if (ROOT/'block-between').exists() and attempt.number==1:
            record('attempt_saved',trace.request_id)
            await gate('between')
        return result
    app.state.storage.finish_attempt=finish
    return app
'''

@pytest.fixture(scope='module')
def process_pg():
    server=PostgresServer().start()
    try: yield server
    finally: server.close()

@pytest.fixture
def process_case(process_pg,tmp_path):
    name=process_pg.create_database()
    models=registry()
    config=write_config(tmp_path/'config',process_pg,name,models)
    document=yaml.safe_load((config/'gateway.yaml').read_text())
    document['recovery']=models.recovery.model_dump()
    (config/'gateway.yaml').write_text(yaml.safe_dump(document))
    migrate(config)
    (tmp_path/'f5o2_bootstrap.py').write_text(BOOTSTRAP)
    try: yield process_pg,name,tmp_path
    finally: process_pg.drop_database(name)


def test_f7_actual_fifteen_second_heartbeat_precedes_retry_result(process_case):
    server,name,directory=process_case
    with gateway(directory) as (_,client):
        rid,url=create(client,'heartbeat')
        started=time.monotonic()
        with client.stream('GET',url,timeout=25) as response:
            received=events(response)
            heartbeat=next(received)
            elapsed=time.monotonic()-started
            assert heartbeat==(None,'response.heartbeat',{'request_id':rid})
            assert 14 <= elapsed < 20
            assert [o for o in observations(directory) if o==['stream','heartbeat']]==[['stream','heartbeat']]
            assert observe(server,name,rid)[0]['status']=='running'
            rest=list(received)
        assert [e[1] for e in rest]==['response.delta','response.completed']
        row,attempts,saved=observe(server,name,rid)
        assert row['status']=='completed' and len(attempts)==2
        assert [e['seq'] for e in saved]==[1,2]
        assert len([o for o in observations(directory) if o==['stream','heartbeat']])==2


def test_f7_disconnect_and_resume_keeps_one_recovery_chain(process_case):
    server,name,directory=process_case
    with gateway(directory) as (_,client):
        rid,url=create(client,'resume')
        with client.stream('GET',url) as response:
            first=next(events(response))
            assert first==(1,'response.delta',{'request_id':rid,'delta':'resume'})
        assert len([o for o in observations(directory) if o==['stream','resume']])==2
        assert len(observe(server,name,rid)[1])==2
        (directory/'release-resume').touch()
        rest=list(events(client.get(url+'?after_seq=1')))
        assert [e[:2] for e in rest]==[(2,'response.delta'),(3,'response.completed')]
        assert rest[0][2]['delta']=='-remaining'
        assert len([o for o in observations(directory) if o==['stream','resume']])==2
        assert observe(server,name,rid)[0]['status']=='completed'


def test_f7_sigkill_during_retry_wait_does_not_recall_after_restart(process_case):
    server,name,directory=process_case
    with gateway(directory) as (process,client),ThreadPoolExecutor(max_workers=1) as pool:
        pending,_=create(client,'pending')
        rid,url=create(client,'kill-wait')
        future=pool.submit(client.get,url)
        wait_for(lambda:len(observe(server,name,rid)[1])==1 and ['stream_closed','kill-wait'] in observations(directory))
        process.kill();assert process.wait(timeout=5)==-signal.SIGKILL
        try: future.result(timeout=5)
        except Exception: pass
    with gateway(directory) as (_,client):
        row,attempts,saved=observe(server,name,rid)
        assert row['status']=='failed' and row['error_code']=='execution_interrupted'
        assert len(attempts)==1 and attempts[0]['status']=='failed'
        assert [e['event_type'] for e in saved]==['response.failed']
        assert observe(server,name,pending)[0]['status']=='pending'
        assert list(events(client.get(url)))[0][2]['code']=='execution_interrupted'
    assert len([o for o in observations(directory) if o==['stream','kill-wait']])==1


def test_f7_pending_freezes_recovery_policy_across_config_change(process_case):
    server,name,directory=process_case
    with gateway(directory) as (_,client): rid,url=create(client,'pending-config')
    config=directory/'config/gateway.yaml'
    document=yaml.safe_load(config.read_text())
    document['recovery'].update(max_attempts=1,max_attempts_per_endpoint=1)
    config.write_text(yaml.safe_dump(document))
    with gateway(directory) as (_,client):
        result=list(events(client.get(url)))
        assert len(result)==1 and result[0][2]['code']=='execution_configuration_changed'
        row,attempts,saved=observe(server,name,rid)
        assert row['status']=='failed' and attempts==[]
    assert not any(o[0]=='stream' for o in observations(directory))


def test_f7_database_outage_between_attempts_never_reissues_provider(process_case):
    # This module runs serially; only this fixture's randomly owned server stops.
    server,name,directory=process_case
    (directory/'block-between').touch()
    try:
        with gateway(directory) as (_,client),ThreadPoolExecutor(max_workers=1) as pool:
            rid,url=create(client,'outage')
            future=pool.submit(client.get,url)
            wait_for(lambda:['attempt_saved',rid] in observations(directory))
            row,attempts,_=observe(server,name,rid)
            assert row['status']=='running' and attempts[0]['status']=='failed'
            assert server.compose('stop','postgres').returncode==0
            (directory/'release-between').touch()
            wait_for(lambda:len([o for o in observations(directory) if o==['stream_closed','outage']])==1)
            # Wait until the response sees storage loss, rather than restarting
            # PG soon enough to let the next Attempt legitimately start.
            try: future.result(timeout=8)
            except Exception: pass
            server.start()
            row,attempts,saved=wait_for(lambda: terminal(server,name,rid))
            assert row['status']=='failed' and row['error_code']=='storage_unavailable'
            assert len([o for o in observations(directory) if o==['stream','outage']])==1
            assert len(attempts)==1 and attempts[0]['error_code']=='provider_connection_error'
    finally:
        if not server.started: server.start()


def terminal(server,name,rid):
    result=observe(server,name,rid)
    return result if result[0]['status'] in {'completed','failed','canceled'} else None


def test_f7_normal_generation_can_exceed_ten_seconds(process_case):
    server,name,directory=process_case
    with gateway(directory) as (_,client):
        rid,url=create(client,'slow-generation')
        started=time.monotonic()
        with client.stream('GET',url,timeout=20) as response:
            items=list(events(response))
        assert time.monotonic()-started>=10
        assert [item[1] for item in items]==['response.delta','response.completed']
        row,attempts,saved=observe(server,name,rid)
        assert row['status']=='completed' and len(attempts)==1
        assert len([o for o in observations(directory) if o==['stream','slow-generation']])==1
