import json
from uuid import uuid4
from pathlib import Path

from alembic import command
from alembic.config import Config

import pytest
from fastapi.testclient import TestClient

from errors import ProviderCallError
from f4_cases import registry
from main import create_app
from persistence.database import StorageUnavailable
from postgres_support import write_config
from providers.mock import MockProvider
from settings import Settings


@pytest.fixture
def setup_probe(legacy_postgres_environment, tmp_path):
    server, template = legacy_postgres_environment
    name = server.create_database(template=template)
    directory = write_config(tmp_path, server, name, registry())
    provider = MockProvider(text='private-probe-response')
    def app():
        return create_app(settings=Settings(env='test', _env_file=None), provider=provider,
                          model_registry=registry(), config_dir=directory)
    try:
        yield app, provider, lambda: server.connect(name)
    finally:
        server.drop_database(name)


def test_probe_success_restart_and_rejections(setup_probe):
    app, provider, connect = setup_probe
    with TestClient(app()) as client:
        response = client.post('/v1/models/general-primary/health')
        assert response.status_code == 200
        rid = response.headers['x-request-id']
        rejected = client.post('/v1/models/missing/health')
        assert rejected.status_code == 400
    with TestClient(app()):
        with connect() as conn:
            assert conn.execute("""select r.operation,r.status,r.input_snapshot,a.status,a.input_snapshot
                from tb_llm_request r join tb_llm_attempt a on r.id=a.request_pk
                where r.request_id=%s""", (rid,)).fetchall() == [('provider_probe','completed',None,'completed',None)]
            assert conn.execute("""select r.status,count(a.attempt_no) from tb_llm_request r
                left join tb_llm_attempt a on r.id=a.request_pk where r.request_id=%s group by r.status""",
                (rejected.headers['x-request-id'],)).fetchall() == [('failed',0)]
    assert len(provider.calls) == 1


@pytest.mark.parametrize('point,committed', [(stage, committed) for stage in
    ('prepare', 'begin_attempt', 'finish_attempt', 'finish') for committed in (False, True)])
def test_probe_storage_fault(setup_probe, monkeypatch, caplog, point, committed):
    app_factory, provider, connect = setup_probe
    app = app_factory()
    with TestClient(app) as client:
        original = getattr(app.state.storage, point)
        async def fault(*args, **kwargs):
            if committed:
                await original(*args, **kwargs)
            raise StorageUnavailable(uncertain=committed)
        with monkeypatch.context() as m:
            m.setattr(app.state.storage, point, fault)
            response = client.post('/v1/models/general-primary/health')
            assert response.status_code == 503
            assert response.json()['code'] == 'storage_unavailable'
            rid = response.headers['x-request-id']
            assert rid in caplog.text and f'stage={point} ' in caplog.text
    after_call = point.startswith('finish')
    assert len(provider.calls) == int(after_call)
    with connect() as conn:
        assert conn.execute('select status from tb_llm_request where request_id=%s',(rid,)).fetchone() == (
            'completed' if point == 'finish' and committed else 'failed',)
        assert conn.execute('select count(*) from tb_llm_attempt').fetchone()[0] == int(after_call or (point == 'begin_attempt' and committed))

        if point == 'finish' and committed:
            timing = json.loads(conn.execute('select timing_snapshot from tb_llm_request where request_id=%s',
                                             (rid,)).fetchone()[0])
            assert timing['error_stage'] is None
            assert timing['delivery_error_code'] == 'storage_unavailable'


def test_probe_upstream_timeout(setup_probe):
    app, provider, connect = setup_probe
    provider.error = ProviderCallError(code='provider_timeout', status_code=504)
    with TestClient(app()) as client:
        response = client.post('/v1/models/general-primary/health')
        assert response.status_code == 504
        assert response.json()['code'] == 'provider_timeout'
        with connect() as conn:
            assert conn.execute("""select r.status,r.error_code,a.status,a.error_code,a.total_tokens
                from tb_llm_request r join tb_llm_attempt a on a.request_pk=r.id where r.request_id=%s""",
                (response.headers['x-request-id'],)).fetchone() == (
                    'failed','provider_timeout','failed','provider_timeout',None)
    assert len(provider.calls) == 1


def test_f7_requests_survive_upgrade_and_restart(legacy_postgres_environment, tmp_path):
    server, _ = legacy_postgres_environment
    name = server.create_database()
    try:
        root = Path(__file__).resolve().parents[2]
        directory = write_config(tmp_path, server, name, registry())
        config = Config(str(root/'alembic.ini'))
        config.set_main_option('script_location', str(root/'migrations'))
        config.attributes['config_dir'] = directory
        command.upgrade(config, 'f7_001')
        ids = [uuid4(), uuid4()]
        with server.connect(name) as conn:
            for rid, operation, seq in zip(ids, ['completion','streaming'], [None,0], strict=True):
                pk = conn.execute("""insert into tb_llm_request(request_id,operation,last_event_seq,status,created_at)
                    values(%s,%s,%s,'completed',now()) returning id""", (rid,operation,seq)).fetchone()[0]
                conn.execute("""insert into tb_llm_attempt(request_pk,attempt_no,model,provider,adapter,api_protocol,
                    configured_model,status,started_at) values(%s,1,'model','mock','mock','chat_completions',
                    'mock','completed',now())""",(pk,))
        command.upgrade(config,'head')
        provider = MockProvider()
        def app():
            return create_app(settings=Settings(env='test', _env_file=None), provider=provider,
                              model_registry=registry(), config_dir=directory)
        with TestClient(app()) as client:
            response = client.post('/v1/models/general-primary/health')
            assert response.status_code == 200
            probe_id = response.headers['x-request-id']
        with TestClient(app()) as client:
            for rid in [*ids, probe_id]:
                result = client.get(f'/v1/traces/{rid}')
                if rid in ids:
                    assert result.status_code == 404  # F7 rows have no authenticated owner
                else:
                    assert result.status_code == 200
                    assert result.json()['data']['status'] == 'completed'
            with server.connect(name) as conn:
                assert conn.execute("""select a.provider_duration_ms from tb_llm_request r
                    join tb_llm_attempt a on r.id=a.request_pk where r.request_id=any(%s)""",(ids,)).fetchall() == [(None,),(None,)]
        assert len(provider.calls) == 1
    finally:
        server.drop_database(name)


@pytest.mark.parametrize('committed', [False, True])
def test_probe_upstream_failure_and_terminal_save_failure(setup_probe, monkeypatch, committed):
    app_factory, provider, connect = setup_probe
    app = app_factory()
    provider.error = ProviderCallError(code='provider_timeout', status_code=504)
    original = app.state.storage.finish
    async def failed_save(*args, **kwargs):
        if committed:
            await original(*args, **kwargs)
        raise StorageUnavailable(uncertain=committed)
    with TestClient(app) as client:
        with monkeypatch.context() as m:
            m.setattr(app.state.storage, 'finish', failed_save)
            response = client.post('/v1/models/general-primary/health')
            assert response.status_code == 503
            assert response.json()['code'] == 'storage_unavailable'
            rid = response.headers['x-request-id']
    with connect() as conn:
        assert conn.execute("""select r.error_code,a.error_code from tb_llm_request r
            join tb_llm_attempt a on a.request_pk=r.id where r.request_id=%s""",(rid,)).fetchone() == (
                'provider_timeout' if committed else 'storage_unavailable', 'provider_timeout')
    with TestClient(app_factory()) as client:
        diagnostic = client.get('/v1/traces/'+rid).json()['data']
        assert diagnostic['error_stage'] == ('provider' if committed else 'finish')
        assert diagnostic['delivery_error_code'] == ('storage_unavailable' if committed else None)
    assert len(provider.calls) == 1


def test_probe_invalid_body_records_admission_stage(setup_probe):
    app, provider, connect = setup_probe
    with TestClient(app()) as client:
        rejected = client.post('/v1/models/general-primary/health', json={'invalid':'private'})
        assert rejected.status_code == 422
        with connect() as conn:
            snapshot, = conn.execute('select timing_snapshot from tb_llm_request where request_id=%s',
                                     (rejected.headers['x-request-id'],)).fetchone()
            assert json.loads(snapshot)['error_stage'] == 'admission'
    assert not provider.calls


@pytest.mark.parametrize('stage', ['prepare','begin_attempt','finish_attempt','finish'])
def test_deadline_expires_during_probe_database_write(setup_probe, monkeypatch, stage):
    import asyncio
    from services import gateway
    app_factory, provider, connect = setup_probe
    app = app_factory()
    # Shorten only this controlled storage-fault experiment. F3 wire tests use 10s.
    monkeypatch.setattr(gateway, 'PROVIDER_PROBE_TIMEOUT_SECONDS', 1)
    with TestClient(app) as client:
        async def blocked(*args, **kwargs):
            await asyncio.Event().wait()
        with monkeypatch.context() as m:
            m.setattr(app.state.storage, stage, blocked)
            response = client.post('/v1/models/general-primary/health')
            assert response.status_code == 503
            assert response.json()['code'] == 'storage_unavailable'
            rid = response.headers['x-request-id']
    with connect() as conn:
        code, snapshot = conn.execute('select error_code,timing_snapshot from tb_llm_request where request_id=%s',
                                      (rid,)).fetchone()
        assert code == 'storage_unavailable'
        assert json.loads(snapshot)['error_stage'] == stage
    assert len(provider.calls) == int(stage in {'finish_attempt','finish'})


@pytest.mark.parametrize('stage', [None, 'provider', 'finish'])
def test_reconciler_keeps_existing_owner_even_when_error_codes_collide(setup_probe, stage):
    from services.trace import AttemptTrace, RequestTrace
    factory, provider, connect = setup_probe
    rid = uuid4()
    # An already committed terminal from an earlier owner. Historical timing
    # may lack a failure stage; an identical code cannot authenticate the owner.
    snapshot = {'recovery_stop_reason': 'already_recorded', 'recovery_wait_ms': 25.5}
    if stage is not None:
        snapshot['error_stage'] = stage
    with connect() as conn:
        pk = conn.execute("""insert into tb_llm_request(request_id,operation,status,error_code,timing_snapshot,
            created_at,ended_at,agent_id) values(%s,'provider_probe','failed','storage_unavailable',%s,now(),now(),'legacy-test')
            returning id""", (rid,json.dumps(snapshot))).fetchone()[0]
        conn.execute("""insert into tb_llm_attempt(request_pk,attempt_no,status,provider,adapter,model,
            configured_model,api_protocol,started_at,cleanup_error_code,latency_ms)
            values(%s,1,'failed','mock','mock','logical','mock','responses',now(),'stream_cleanup_timeout',17)""",(pk,))
    app = factory()
    stale = RequestTrace(str(rid), operation='provider_probe', error_code='storage_unavailable',
                         error_stage='finish', recovery_wait_ms=0)
    with TestClient(app) as client:
        # Shutdown drains the real reconciliation queue.
        rejected_attempt = AttemptTrace(str(rid), 1, 'logical', latency_ms=85)
        client.portal.call(app.state.defer_persistence, stale, rejected_attempt)
    with TestClient(factory()) as client:
        result = client.get(f'/v1/traces/{rid}').json()['data']
        assert result['error_stage'] == stage
        assert result['delivery_error_code'] == 'storage_unavailable'
        assert result['error_code'] == 'storage_unavailable' and len(result['attempts']) == 1
        assert result['attempts'][0]['cleanup_error_code'] == 'stream_cleanup_timeout'
        assert result['attempts'][0]['latency_ms'] == 17
        assert result['recovery']['stop_reason'] == 'already_recorded'
        assert result['recovery']['wait_ms'] == 25.5
    assert not provider.calls
