"""Real TCP, owned Gateway processes and disposable Compose PostgreSQL evidence."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import signal
import subprocess
import sys
import time
from uuid import uuid4

import httpx
from psycopg.rows import dict_row
from legacy_identity import HEADERS
import pytest
import yaml

from f4_cases import registry
from postgres_support import PostgresServer, write_config, migrate


BOOTSTRAP = '''
import asyncio
import json
from pathlib import Path
from main import create_app as build_app
from providers.mock import MockProvider
from providers.base import ProviderResult, ProviderStreamEvent
import providers.factory as factory
from settings import Settings
from f4_cases import registry
from sqlalchemy import text

ROOT = Path(__file__).parent

def record(kind, label=''):
    with (ROOT/'observations.jsonl').open('a') as file:
        file.write(json.dumps([kind, label])+'\\n')

async def gate(label):
    record('waiting', label)
    while not (ROOT/('release-'+label)).exists():
        await asyncio.sleep(.01)

class ObservedProvider(MockProvider):
    async def complete(self, model, messages, **kwargs):
        label = messages[-1].content
        record('complete', label)
        await gate(label)
        return ProviderResult(text=label)

    async def stream(self, model, messages, **kwargs):
        label = messages[-1].content
        record('stream', label)
        try:
            yield ProviderStreamEvent(delta=label)
            await gate(label)
            if label == 'outage':
                yield ProviderStreamEvent(delta='second')
            yield ProviderStreamEvent(status='completed', upstream_terminal_received=True)
        finally:
            record('stream_closed', label)

    async def aclose(self):
        record('provider_closed')
        await super().aclose()

def create_app():
    factory.create_provider = lambda *args, **kwargs: ObservedProvider()
    app = build_app(settings=Settings(env='test', _env_file=None),
                    model_registry=registry(), config_dir=ROOT/'config')
    original = app.state.storage.begin_attempt
    async def begin(trace, attempt, **kwargs):
        await original(trace, attempt, **kwargs)
        if (ROOT/'block-attempt').exists():
            record('attempt_committed', trace.request_id)
            await asyncio.Event().wait()
    app.state.storage.begin_attempt = begin
    recover_row = app.state.storage._recover_row
    async def recover(session, row):
        result = await recover_row(session, row)
        if (ROOT/'rollback-recovery').exists():
            (ROOT/'rollback-recovery').unlink()
            record('recovery_rollback')
            await session.execute(text('SELECT 1/0'))
        return result
    app.state.storage._recover_row = recover
    return app
'''


def wait_for(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    pytest.fail('Timed out waiting for explicit process/database observation')


def observations(directory):
    path = directory / 'observations.jsonl'
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    return [json.loads(line) for line in lines if line.endswith(']')]


def body(label):
    return {'model': 'general-primary', 'prompt': {'name': 'basic_text', 'version': '1.0.0'},
            'messages': [{'role': 'user', 'content': label}], 'timeout_seconds': 30}


def events(response):
    frame = []
    for line in response.iter_lines():
        if line:
            frame.append(line)
        elif frame:
            name = next(line[7:] for line in frame if line.startswith('event: '))
            data = json.loads(next(line[6:] for line in frame if line.startswith('data: ')))
            yield name, data
            frame = []
    assert not frame


def observe(server, name, request_id):
    with server.connect(name, row_factory=dict_row) as conn:
        row = conn.execute('SELECT * FROM tb_llm_request WHERE request_id=%s', (request_id,)).fetchone()
        assert row is not None
        attempts = conn.execute('SELECT * FROM tb_llm_attempt WHERE request_pk=%s', (row['id'],)).fetchall()
        saved = conn.execute('SELECT * FROM tb_llm_stream_event WHERE request_pk=%s ORDER BY seq',
                             (row['id'],)).fetchall()
    return row, attempts, saved


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
    write_config(tmp_path / 'config', process_pg, name, registry())
    migrate(tmp_path / 'config')
    (tmp_path / 'f5o1_bootstrap.py').write_text(BOOTSTRAP)
    try:
        yield process_pg, name, tmp_path
    finally:
        process_pg.drop_database(name)


@contextmanager
def gateway(directory, *, ready=True):
    # Own the inherited listening FD and child PID; never discover or kill by port/name.
    project = Path(__file__).resolve().parents[2]
    with socket.socket() as listener, (directory / ('gateway-' + uuid4().hex + '.log')).open('w+') as log:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        process = subprocess.Popen([
            sys.executable, '-m', 'uvicorn', 'f5o1_bootstrap:create_app', '--factory',
            '--workers', '1', '--timeout-graceful-shutdown', '1', '--fd', str(listener.fileno()), '--log-level', 'warning'],
            cwd=directory, env={'PATH': os.defpath, 'PYTHONPATH': os.pathsep.join(
                [str(directory), str(project / 'src'), str(project / 'tests')])},
            pass_fds=(listener.fileno(),), stdout=log, stderr=subprocess.STDOUT)
        listener.close()
        with httpx.Client(headers=HEADERS, base_url=f'http://127.0.0.1:{port}', timeout=8, trust_env=False) as client:
            try:
                if ready:
                    def health():
                        if process.poll() is not None:
                            log.seek(0)
                            pytest.fail(log.read())
                        try:
                            return client.get('/healthz').status_code == 200
                        except httpx.TransportError:
                            return False
                    wait_for(health)
                yield process, client
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                        pytest.fail('Owned Gateway did not shut down within ten seconds')


def create_stream(client,payload):
    response=client.post('/v1/llm/stream',json=payload)
    assert response.status_code==202,response.text
    return f"/v1/llm/stream/{response.json()['data']['request_id']}/events"


def test_tcp_delta_is_committed_while_provider_waits(process_case):
    server, name, directory = process_case
    with gateway(directory) as (_, client):
        with client.stream('GET', create_stream(client,body('tcp-gated'))) as response:
            assert response.status_code == 200
            request_id = response.headers['x-request-id']
            received = events(response)
            assert next(received) == ('response.delta', {'request_id': request_id, 'delta': 'tcp-gated'})
            wait_for(lambda: ['waiting', 'tcp-gated'] in observations(directory))
            row, attempts, saved = observe(server, name, request_id)
            assert row['status'] == attempts[0]['status'] == 'running'
            assert [e['event_type'] for e in saved] == ['response.delta']
            assert json.loads(saved[0]['payload'])['delta'] == 'tcp-gated'
            assert ['stream_closed', 'tcp-gated'] not in observations(directory)
            (directory / 'release-tcp-gated').touch()
            assert list(received)[-1] == ('response.completed', {'request_id': request_id, 'usage': None})
        row, attempts, saved = observe(server, name, request_id)
        assert row['status'] == attempts[0]['status'] == 'completed'
        assert [e['seq'] for e in saved] == [1, 2]


@pytest.mark.parametrize('streaming', [True, False])
def test_sigkill_recovers_running_without_recalling_provider(process_case, streaming):
    server, name, directory = process_case
    with gateway(directory) as (process, client):
        if streaming:
            with client.stream('GET', create_stream(client,body('killed'))) as response:
                request_id = response.headers['x-request-id']
                received = events(response)
                assert next(received)[0] == 'response.delta'
                assert observe(server, name, request_id)[0]['status'] == 'running'
                process.kill()
                assert process.wait(timeout=5) < 0
        else:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(client.post, '/v1/llm', json=body('killed'))
                wait_for(lambda: ['waiting', 'killed'] in observations(directory))
                with server.connect(name) as conn:
                    request_id, status = conn.execute('SELECT request_id,status FROM tb_llm_request').fetchone()
                assert status == 'running'
                process.kill()
                assert process.wait(timeout=5) < 0
                with pytest.raises(httpx.TransportError):
                    pending.result(timeout=5)
    for restart in range(2):
        with gateway(directory) as (_, client):
            row, attempts, saved = observe(server, name, request_id)
            assert row['status'] == attempts[0]['status'] == 'failed'
            assert row['error_code'] == attempts[0]['error_code'] == 'execution_interrupted'
            assert attempts[0]['input_tokens'] is None
            assert [e['event_type'] for e in saved] == (['response.delta', 'response.failed'] if streaming else [])
            assert row['last_event_seq'] == (2 if streaming else None)
            automatic_calls = [item for item in observations(directory) if item[0] in {'stream', 'complete'}]
            assert automatic_calls == [['stream' if streaming else 'complete', 'killed']]
            if restart == 1:
                (directory / 'release-after-restart').touch()
                response = client.post('/v1/llm', json=body('after-restart'))
                assert response.status_code == 200
                assert observe(server, name, response.headers['x-request-id'])[0]['status'] == 'completed'
    calls = [item for item in observations(directory) if item[0] in {'stream', 'complete'}]
    assert calls == [['stream' if streaming else 'complete', 'killed'], ['complete', 'after-restart']]


def test_attempt_committed_before_provider_call_survives_kill(process_case):
    server, name, directory = process_case
    (directory / 'block-attempt').touch()
    with gateway(directory) as (process, client), ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, '/v1/llm', json=body('not-called'))
        marker = wait_for(lambda: next((item for item in observations(directory)
                                      if item[0] == 'attempt_committed'), None))
        request_id = marker[1]
        row, attempts, _ = observe(server, name, request_id)
        assert row['status'] == attempts[0]['status'] == 'running'
        assert not any(item[0] in {'stream', 'complete'} for item in observations(directory))
        process.kill()
        process.wait(timeout=5)
        with pytest.raises(httpx.TransportError):
            pending.result(timeout=5)
    (directory / 'block-attempt').unlink()
    with gateway(directory):
        row, attempts, _ = observe(server, name, request_id)
        assert row['status'] == attempts[0]['status'] == 'failed'
        assert row['error_code'] == attempts[0]['error_code'] == 'execution_interrupted'
        assert attempts[0]['actual_model'] is attempts[0]['input_tokens'] is None
    assert not any(item[0] in {'stream', 'complete'} for item in observations(directory))


def seed_states(server, name):
    ids = {}
    with server.connect(name) as conn:
        for status in ['pending', 'canceling', 'completed', 'failed', 'canceled']:
            request_id = uuid4()
            pk = conn.execute('''INSERT INTO tb_llm_request
                (request_id,operation,status,created_at,input_snapshot,last_event_seq,error_code)
                VALUES (%s,'streaming',%s,now(),'{}',%s,%s) RETURNING id''',
                (request_id, status, 1 if status in {'completed','failed','canceled'} else 0,
                 'seed_error' if status in {'failed','canceled'} else None)).fetchone()[0]
            if status != 'pending':
                conn.execute('''INSERT INTO tb_llm_attempt
                    (request_pk,attempt_no,model,provider,adapter,api_protocol,configured_model,status,started_at)
                    VALUES (%s,1,'general-primary','mock-main','mock','chat_completions','mock',%s,now())''',
                    (pk, 'running' if status == 'canceling' else 'completed' if status == 'completed' else 'failed'))
            if status in {'completed', 'failed', 'canceled'}:
                conn.execute('''INSERT INTO tb_llm_stream_event(request_pk,seq,event_type,payload,created_at)
                    VALUES (%s,1,%s,%s,now())''',
                    (pk, 'response.completed' if status == 'completed' else 'response.failed', '{"seed":true}'))
            ids[status] = request_id
    return ids


def test_startup_recovery_rolls_back_then_is_idempotent(process_case):
    server, name, directory = process_case
    ids = seed_states(server, name)
    originals = {status: observe(server, name, request_id) for status, request_id in ids.items()}
    (directory / 'rollback-recovery').touch()
    with gateway(directory, ready=False) as (process, _):
        assert process.wait(timeout=12) != 0
    assert ['recovery_rollback', ''] in observations(directory)
    assert observe(server, name, ids['canceling']) == originals['canceling']
    for _ in range(2):
        with gateway(directory):
            for status, request_id in ids.items():
                row, attempts, saved = observe(server, name, request_id)
                if status == 'canceling':
                    assert row['status'] == attempts[0]['status'] == 'failed'
                    assert row['error_code'] == 'execution_interrupted'
                    assert [e['event_type'] for e in saved] == ['response.failed']
                    assert row['last_event_seq'] == 1
                else:
                    assert (row, attempts, saved) == originals[status]
    assert not any(item[0] in {'stream', 'complete'} for item in observations(directory))


def test_normal_shutdown_closes_pool_and_owned_provider(process_case):
    server, name, directory = process_case
    (directory / 'release-normal').touch()
    with gateway(directory) as (process, client):
        response = client.post('/v1/llm', json=body('normal'))
        assert response.status_code == 200
        assert observe(server, name, response.headers['x-request-id'])[0]['status'] == 'completed'
        with server.connect(name) as conn:
            # Observer is excluded; no request transaction remains open after HTTP completion.
            assert conn.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=%s "
                                "AND pid<>pg_backend_pid() AND xact_start IS NOT NULL", (name,)).fetchone()[0] == 0
        assert not any(item[0] == 'provider_closed' for item in observations(directory))
    assert process.returncode == -signal.SIGTERM
    assert sum(item[0] == 'provider_closed' for item in observations(directory)) == 2
    with server.connect() as conn:
        assert conn.execute('SELECT count(*) FROM pg_stat_activity WHERE datname=%s', (name,)).fetchone()[0] == 0


def test_compose_down_without_volume_flag_preserves_owned_data():
    server = PostgresServer()
    # Exercise the product Compose file, overriding only owned test identities.
    project = Path(__file__).resolve().parents[2]
    document = yaml.safe_load((project / 'compose.yaml').read_text())
    service = document['services']['postgres']
    service['environment']['POSTGRES_USER'] = server.username
    service['environment']['POSTGRES_DB'] = 'gateway_test'
    service['ports'] = ['127.0.0.1::5432']
    service['healthcheck']['test'] = ['CMD-SHELL', 'pg_isready -U gateway_test -d gateway_test']
    document['secrets']['postgres_password']['file'] = str(server.password_path)
    server.compose_path.write_text(yaml.safe_dump(document))
    server.start()
    try:
        assert server.project.startswith('llmgw-test-')
        marker = uuid4().hex
        with server.connect() as conn:
            conn.execute('CREATE TABLE owned_sentinel (value text NOT NULL)')
            conn.execute('INSERT INTO owned_sentinel VALUES (%s)', (marker,))
        volumes = subprocess.run(['docker', 'volume', 'ls', '-q', '--filter',
                                  'label=com.docker.compose.project=' + server.project],
                                 capture_output=True, text=True, check=False)
        assert volumes.returncode == 0 and volumes.stdout.strip()
        owned_volumes = volumes.stdout.splitlines()
        container = server.compose('ps', '-q', 'postgres')
        inspected = subprocess.run(['docker', 'inspect', container.stdout.strip()],
                                   capture_output=True, text=True, check=True)
        mounts = json.loads(inspected.stdout)[0]['Mounts']
        database_mount = [mount for mount in mounts if mount['Destination'] == '/var/lib/postgresql']
        assert len(database_mount) == 1
        assert database_mount[0]['Type'] == 'volume'
        assert database_mount[0]['Name'] in owned_volumes
        result = server.compose('down', '--remove-orphans')
        assert result.returncode == 0, result.stderr
        server.start()
        with server.connect() as conn:
            assert conn.execute('SELECT value FROM owned_sentinel').fetchall() == [(marker,)]
        # Cleanup guard refuses development database names without issuing SQL.
        with pytest.raises(ValueError, match='non-test'):
            server.drop_database('developer_database')
    finally:
        server.close()


@pytest.mark.parametrize('shutdown_while_offline', [False, True])
def test_owned_database_outage_closes_upstream_and_reconciles_only_stopped_execution(tmp_path,
                                                                                  shutdown_while_offline):
    # Destructive outage belongs to this instance, never the shared process_pg fixture.
    server = PostgresServer().start()
    # Docker may reassign an ephemeral published port when a stopped container
    # restarts. Pin the allocated owned port so the running Gateway keeps its DSN.
    document = yaml.safe_load(server.compose_path.read_text())
    document['services']['postgres']['ports'] = [f'127.0.0.1:{server.port}:5432']
    server.compose_path.write_text(yaml.safe_dump(document))
    try:
        name = server.create_database()
        write_config(tmp_path / 'config', server, name, registry())
        migrate(tmp_path / 'config')
        (tmp_path / 'f5o1_bootstrap.py').write_text(BOOTSTRAP)
        with gateway(tmp_path) as (process, client):
            with client.stream('GET', create_stream(client,body('outage'))) as response_a:
                id_a = response_a.headers['x-request-id']
                events_a = events(response_a)
                assert next(events_a)[0] == 'response.delta'
                with client.stream('GET', create_stream(client,body('active-b'))) as response_b:
                    id_b = response_b.headers['x-request-id']
                    events_b = events(response_b)
                    assert next(events_b)[0] == 'response.delta'
                    assert observe(server, name, id_a)[0]['status'] == 'running'
                    assert observe(server, name, id_b)[0]['status'] == 'running'
                    stopped = server.compose('stop', 'postgres')
                    assert stopped.returncode == 0, stopped.stderr
                    (tmp_path / 'release-outage').touch()
                    wait_for(lambda: ['stream_closed', 'outage'] in observations(tmp_path))
                    assert ['stream_closed', 'active-b'] not in observations(tmp_path)
                    if shutdown_while_offline:
                        # End only B's subscription; application shutdown owns its generation.
                        response_b.close()
                        assert ['stream_closed','active-b'] not in observations(tmp_path)
                        process.terminate()
                        assert process.wait(timeout=10) == -signal.SIGTERM
                        wait_for(lambda: ['stream_closed', 'active-b'] in observations(tmp_path))
                    else:
                        server.start()
                        row, attempts, saved = wait_for(lambda: terminal_observation(server, name, id_a))
                        assert row['status'] == attempts[0]['status'] == 'failed'
                        assert row['error_code'] == 'storage_unavailable'
                        assert [e['event_type'] for e in saved] == ['response.delta', 'response.failed']
                        assert observe(server, name, id_b)[0]['status'] == 'running'
                        (tmp_path / 'release-active-b').touch()
                        # PG outage ends this reader; reconnect to the same generation.
                        response_b.close()
                        resumed = client.get(f'/v1/llm/stream/{id_b}/events?after_seq=1')
                        assert list(events(resumed))[-1][0] == 'response.completed'
                        assert observe(server, name, id_b)[0]['status'] == 'completed'
            if not shutdown_while_offline:
                with server.connect(name) as conn:
                    assert conn.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=%s "
                                        "AND pid<>pg_backend_pid() AND state='idle in transaction'",
                                        (name,)).fetchone()[0] == 0
        calls = [item for item in observations(tmp_path) if item[0] == 'stream']
        assert calls == [['stream', 'outage'], ['stream', 'active-b']]
        assert sum(item[0] == 'provider_closed' for item in observations(tmp_path)) == 2
        if shutdown_while_offline:
            server.start()
            with server.connect() as conn:
                assert conn.execute('SELECT count(*) FROM pg_stat_activity WHERE datname=%s',
                                    (name,)).fetchone()[0] == 0
    finally:
        server.close()


def terminal_observation(server, name, request_id):
    result = observe(server, name, request_id)
    return result if result[0]['status'] in {'failed', 'completed'} else None
