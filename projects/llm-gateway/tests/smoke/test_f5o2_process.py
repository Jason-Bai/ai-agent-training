"""Owned TCP processes and real PostgreSQL: generation outlives subscribers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from uuid import uuid4

import httpx
from legacy_identity import HEADERS
import pytest
import yaml

from f4_cases import registry
from postgres_support import PostgresServer, write_config, migrate
from test_f5o1_process import BOOTSTRAP as BASE_BOOTSTRAP, observations, observe, wait_for, body, seed_states


BOOTSTRAP = BASE_BOOTSTRAP + '''
_original_create_app = create_app

def create_app():
    app = _original_create_app()
    original_append = app.state.storage.append_event
    async def append(trace, name, payload, **kwargs):
        if (ROOT/'block-commit').exists():
            record('before_commit', trace.request_id)
            await gate('commit')
        return await original_append(trace, name, payload, **kwargs)
    app.state.storage.append_event = append
    return app
'''


def events(response):
    fields = {}
    for line in response.iter_lines():
        if line:
            key, value = line.split(': ', 1)
            fields[key] = value
        elif fields:
            yield (int(fields['id']) if 'id' in fields else None,
                   fields['event'], json.loads(fields['data']))
            fields = {}
    assert not fields


@pytest.fixture(scope='module')
def process_pg():
    server = PostgresServer().start()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def process_case(process_pg, tmp_path):
    name = process_pg.create_database()
    write_config(tmp_path/'config', process_pg, name, registry())
    migrate(tmp_path/'config')
    (tmp_path/'f5o2_bootstrap.py').write_text(BOOTSTRAP)
    try:
        yield process_pg, name, tmp_path
    finally:
        process_pg.drop_database(name)


@contextmanager
def gateway(directory):
    project = Path(__file__).resolve().parents[2]
    with socket.socket() as listener, (directory/('o2-'+uuid4().hex+'.log')).open('w+') as log:
        listener.bind(('127.0.0.1', 0)); listener.listen()
        port = listener.getsockname()[1]
        process = subprocess.Popen([
            sys.executable, '-m', 'uvicorn', 'f5o2_bootstrap:create_app', '--factory', '--workers','1',
            '--fd',str(listener.fileno()), '--timeout-graceful-shutdown','1','--log-level','warning'],
            cwd=directory, env={'PATH':os.defpath,'PYTHONPATH':os.pathsep.join(
                [str(directory),str(project/'src'),str(project/'tests')])},
            pass_fds=(listener.fileno(),),stdout=log,stderr=subprocess.STDOUT)
        listener.close()
        with httpx.Client(headers=HEADERS, base_url=f'http://127.0.0.1:{port}',timeout=10,trust_env=False) as client:
            try:
                def ready():
                    if process.poll() is not None:
                        log.seek(0); pytest.fail(log.read())
                    try: return client.get('/healthz').status_code==200
                    except httpx.TransportError: return False
                wait_for(ready)
                yield process,client
            finally:
                if process.poll() is None:
                    process.terminate()
                    try: process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill();process.wait(timeout=5)
                        pytest.fail('Owned O2 Gateway exceeded bounded shutdown')


def create(client,label):
    response=client.post('/v1/llm/stream',json=body(label))
    assert response.status_code==202,response.text
    rid=response.json()['data']['request_id']
    assert response.headers['x-request-id']==rid
    return rid, f'/v1/llm/stream/{rid}/events'


def terminal(server,name,rid):
    result=observe(server,name,rid)
    return result if result[0]['status'] in {'completed','failed','canceled'} else None


def test_commit_barrier_precedes_first_tcp_delta(process_case):
    server,name,directory=process_case
    (directory/'block-commit').touch()
    with gateway(directory) as (_,client),ThreadPoolExecutor(max_workers=1) as pool:
        rid,url=create(client,'commit-proof')
        assert observe(server,name,rid)[0]['status']=='pending'
        assert observations(directory)==[]
        with client.stream('GET',url) as response:
            assert response.status_code==200
            received=events(response); future=pool.submit(next,received)
            wait_for(lambda:['before_commit',rid] in observations(directory))
            assert observe(server,name,rid)[2]==[] and not future.done()
            (directory/'release-commit').touch()
            assert future.result(timeout=5)==(1,'response.delta',{'request_id':rid,'delta':'commit-proof'})
            row,attempts,saved=observe(server,name,rid)
            assert row['status']==attempts[0]['status']=='running'
            assert [e['seq'] for e in saved]==[1]
            (directory/'release-commit-proof').touch()
            assert list(received)[-1][1]=='response.completed'


def test_disconnect_continues_and_resume_uses_saved_sequence(process_case):
    server,name,directory=process_case
    with gateway(directory) as (_,client):
        rid,url=create(client,'continue')
        with client.stream('GET',url) as response:
            assert response.headers['x-request-id']==rid
            first=next(events(response));assert first[0:2]==(1,'response.delta')
        assert ['stream_closed','continue'] not in observations(directory)
        (directory/'release-continue').touch()
        row,attempts,saved=wait_for(lambda:terminal(server,name,rid))
        assert row['status']==attempts[0]['status']=='completed'
        assert [e['seq'] for e in saved]==[1,2]
        replay=client.get(url+'?after_seq=1')
        assert list(events(replay))==[(2,'response.completed',{'request_id':rid,'usage':None})]
        assert client.get(url+'?after_seq=2').status_code==204
        assert list(events(client.get(url)))[0]==first  # lost client ACK permits duplicate delivery
        assert [o for o in observations(directory) if o[0]=='stream']==[['stream','continue']]


def test_twenty_subscribers_share_one_generation_and_other_request_isolated(process_case):
    server,name,directory=process_case
    with gateway(directory) as (_,client),ThreadPoolExecutor(max_workers=21) as pool:
        rid,url=create(client,'shared');other,other_url=create(client,'other')
        futures=[pool.submit(client.get,url) for _ in range(20)]
        other_future=pool.submit(client.get,other_url)
        wait_for(lambda:len(observe(server,name,rid)[2])==1)
        wait_for(lambda:len(observe(server,name,other)[2])==1)
        (directory/'release-other').touch()
        assert list(events(other_future.result(timeout=8)))[-1][1]=='response.completed'
        assert observe(server,name,rid)[0]['status']=='running'
        (directory/'release-shared').touch()
        responses=[f.result(timeout=8) for f in futures]
        assert all(r.status_code==200 for r in responses)
        assert all(list(events(r))==list(events(responses[0])) for r in responses)
        assert len(observe(server,name,rid)[1])==1
        assert sorted(o for o in observations(directory) if o[0]=='stream')==[['stream','other'],['stream','shared']]


def test_sigkill_two_restarts_preserve_pending_and_all_terminal_states(process_case):
    server,name,directory=process_case
    ids=seed_states(server,name)
    originals={s:observe(server,name,rid) for s,rid in ids.items()}
    with gateway(directory) as (process,client):
        pending,pending_url=create(client,'pending-valid')
        rid,url=create(client,'kill-running')
        with client.stream('GET',url) as response:
            assert next(events(response))[0]==1
            process.kill();assert process.wait(timeout=5)==-signal.SIGKILL
    for _ in range(2):
        with gateway(directory) as (_,client):
            row,attempts,saved=observe(server,name,rid)
            assert row['status']==attempts[0]['status']=='failed'
            assert row['error_code']=='execution_interrupted'
            assert [e['event_type'] for e in saved]==['response.delta','response.failed']
            assert observe(server,name,pending)[0]['status']=='pending'
            for status,seed_id in ids.items():
                observed=observe(server,name,seed_id)
                if status=='canceling':
                    assert observed[0]['status']=='failed' and len(observed[2])==1
                else:
                    assert observed==originals[status]
            assert list(events(client.get(url+'?after_seq=1')))[0][2]['code']=='execution_interrupted'
    assert [o for o in observations(directory) if o[0]=='stream']==[['stream','kill-running']]


@pytest.mark.parametrize('subscribed',[False,True])
def test_shutdown_stops_owned_background_without_turning_pending_failed(process_case,subscribed):
    server,name,directory=process_case
    with gateway(directory) as (process,client):
        pending,_=create(client,'pending-shutdown')
        rid,url=create(client,'shutdown-owned')
        if subscribed:
            with client.stream('GET',url) as response:
                assert next(events(response))[0]==1
                process.terminate();process.wait(timeout=10)
        else:
            with client.stream('GET',url) as response:
                assert next(events(response))[0]==1
            process.terminate();process.wait(timeout=10)
    assert ['stream_closed','shutdown-owned'] in observations(directory)
    assert observe(server,name,pending)[0]['status']=='pending'
    assert observe(server,name,rid)[0]['status']=='failed'
    with server.connect() as conn:
        assert conn.execute('SELECT count(*) FROM pg_stat_activity WHERE datname=%s',(name,)).fetchone()[0]==0


@pytest.mark.parametrize('shutdown_offline',[False,True])
def test_database_outage_stopped_generation_does_not_fail_active_peer(tmp_path,shutdown_offline):
    # Exclusive infrastructure: never stop another test's or the developer's server.
    server=PostgresServer().start()
    document=yaml.safe_load(server.compose_path.read_text())
    document['services']['postgres']['ports']=[f'127.0.0.1:{server.port}:5432']
    server.compose_path.write_text(yaml.safe_dump(document))
    try:
        name=server.create_database();write_config(tmp_path/'config',server,name,registry());migrate(tmp_path/'config')
        (tmp_path/'f5o2_bootstrap.py').write_text(BOOTSTRAP)
        with gateway(tmp_path) as (process,client):
            a,url_a=create(client,'outage');b,url_b=create(client,'active-b')
            with client.stream('GET',url_a) as response:
                assert next(events(response))[0]==1
            with client.stream('GET',url_b) as response:
                assert next(events(response))[0]==1
            assert server.compose('stop','postgres').returncode==0
            (tmp_path/'release-outage').touch()
            wait_for(lambda:['stream_closed','outage'] in observations(tmp_path))
            assert ['stream_closed','active-b'] not in observations(tmp_path)
            if shutdown_offline:
                process.terminate();assert process.wait(timeout=10)==-signal.SIGTERM
                assert ['stream_closed','active-b'] in observations(tmp_path)
            server.start()
            if not shutdown_offline:
                assert wait_for(lambda:terminal(server,name,a))[0]['status']=='failed'
                assert observe(server,name,b)[0]['status']=='running'
                (tmp_path/'release-active-b').touch()
                assert wait_for(lambda:terminal(server,name,b))[0]['status']=='completed'
        if shutdown_offline:
            with gateway(tmp_path):
                assert observe(server,name,a)[0]['status']=='failed'
                assert observe(server,name,b)[0]['status']=='failed'
        assert sorted(o for o in observations(tmp_path) if o[0]=='stream')==[['stream','active-b'],['stream','outage']]
    finally:
        server.close()


def test_pending_survives_restart_and_only_explicit_subscription_starts_it(process_case):
    server,name,directory=process_case
    with gateway(directory) as (_,client):
        rid,url=create(client,'waiting-after-restart')
        original=observe(server,name,rid)
        assert original[0]['status']=='pending' and original[1:]==([],[])
    with gateway(directory) as (_,client):
        assert observe(server,name,rid)==original
        assert not any(o[0]=='stream' for o in observations(directory))
        (directory/'release-waiting-after-restart').touch()
        response=client.get(url)
        assert [item[1] for item in events(response)]==['response.delta','response.completed']
        row,attempts,saved=observe(server,name,rid)
        assert row['status']=='completed' and len(attempts)==1
        assert row['input_snapshot']==original[0]['input_snapshot']
        assert [e['seq'] for e in saved]==[1,2]
