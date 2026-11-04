"""F6 real TCP clients, owned worker processes and PostgreSQL observations."""
from concurrent.futures import ThreadPoolExecutor
import json
import signal
import socket

import httpx
import pytest
import yaml

from f4_cases import registry
from postgres_support import PostgresServer, write_config, migrate
from test_f5o2_process import (BOOTSTRAP as BASE, gateway, create, events,
                              observations, observe, wait_for)

BOOTSTRAP = BASE + '''
_base_factory = create_app
_original_stream = ObservedProvider.stream
async def stream_with_cleanup_gate(self, *args, **kwargs):
    iterator = _original_stream(self, *args, **kwargs)
    try:
        async for item in iterator:
            yield item
    finally:
        await iterator.aclose()
        if (ROOT/'block-provider-close').exists():
            record('provider_cleanup_waiting')
            await gate('provider-close')
ObservedProvider.stream = stream_with_cleanup_gate

def create_app():
    app = _base_factory()
    original = getattr(app.state.storage, 'finish_cancel', None)
    if original is not None:
        async def finish(*args, **kwargs):
            if (ROOT/'block-cancel-finish').exists():
                record('cancel_finish_waiting')
                await gate('cancel-finish')
            return await original(*args, **kwargs)
        app.state.storage.finish_cancel = finish
    class ReplyGate:
        def __init__(self, app): self.app = app
        async def __call__(self, scope, receive, send):
            async def output(message):
                if (scope.get('path','').endswith('/cancel')
                    and message['type']=='http.response.start'
                    and (ROOT/'block-cancel-reply').exists()):
                    record('cancel_reply_waiting')
                    await gate('cancel-reply')
                await send(message)
            await self.app(scope, receive, output)
    app.add_middleware(ReplyGate)
    return app
'''

@pytest.fixture(scope='module')
def process_pg():
    value = PostgresServer().start()
    try: yield value
    finally: value.close()

@pytest.fixture
def process_case(process_pg, tmp_path):
    name = process_pg.create_database()
    write_config(tmp_path/'config', process_pg, name, registry()); migrate(tmp_path/'config')
    (tmp_path/'f5o2_bootstrap.py').write_text(BOOTSTRAP)
    try: yield process_pg, name, tmp_path
    finally: process_pg.drop_database(name)


def cancel(client, rid):
    response = client.post(f'/v1/llm/stream/{rid}/cancel')
    assert response.status_code in (200, 202), response.text
    data = response.json()
    assert set(data) == {'success', 'code', 'data', 'message'}
    assert response.headers['x-request-id'] == rid
    assert data['success'] is True and data['code'] == 'ok'
    assert set(data['data']) == {'request_id', 'status'}
    assert data['data']['request_id'] == rid
    status = data['data']['status']
    assert response.status_code == (202 if status == 'canceling' else 200)
    return status


def done(client, rid):
    status = cancel(client, rid)
    return status if status in {'canceled', 'failed', 'completed'} else None


def assert_saved(server, name, rid, expected, prefix=()):
    row, attempts, saved = observe(server, name, rid)
    assert row['status'] == expected
    assert [e['seq'] for e in saved] == list(range(1, len(saved)+1))
    assert [e['event_type'] for e in saved] == [*prefix, 'response.canceling', 'response.'+expected]
    assert all(json.loads(e['payload'])['request_id'] == rid for e in saved)
    if attempts: assert attempts[0]['status'] == expected
    return row, attempts, saved


def test_pending_cancel_only_post_polling_never_starts_provider(process_case):
    server, name, directory = process_case
    with gateway(directory) as (_, client):
        rid, url = create(client, 'pending-cancel')
        assert wait_for(lambda: done(client, rid)) == 'canceled'
        _, attempts, _ = assert_saved(server, name, rid, 'canceled')
        assert attempts == []
        assert not any(o[0] == 'stream' for o in observations(directory))
        assert [item[1] for item in events(client.get(url))] == ['response.canceling','response.canceled']
        assert cancel(client, rid) == 'canceled'


def test_tcp_multiple_subscribers_disconnect_and_peer_remains_live(process_case):
    server, name, directory = process_case
    with gateway(directory) as (_, client), ThreadPoolExecutor(max_workers=2) as pool:
        rid, url = create(client, 'cancel-a'); other, other_url = create(client, 'peer-b')
        with client.stream('GET', url) as response:
            first = next(events(response)); assert first[1] == 'response.delta'
        subscriptions = [pool.submit(client.get, url+'?after_seq=1') for _ in range(2)]
        with client.stream('GET', other_url) as peer:
            assert next(events(peer))[1] == 'response.delta'
        assert wait_for(lambda: done(client, rid)) == 'canceled'
        for future in subscriptions:
            assert [e[1] for e in events(future.result(timeout=8))] == ['response.canceling','response.canceled']
        wait_for(lambda: ['stream_closed','cancel-a'] in observations(directory))
        assert ['provider_closed',''] not in observations(directory)
        assert observe(server, name, other)[0]['status'] == 'running'
        assert_saved(server, name, rid, 'canceled', ['response.delta'])
        assert [e[1] for e in events(client.get(url))] == ['response.delta','response.canceling','response.canceled']
        (directory/'release-peer-b').touch()
        assert list(events(client.get(other_url)))[-1][1] == 'response.completed'
        assert observe(server, name, other)[0]['status'] == 'completed'
        assert sorted(o for o in observations(directory) if o[0]=='stream') == [['stream','cancel-a'],['stream','peer-b']]


def test_cancel_http_connection_closed_after_acceptance_does_not_stop_cleanup(process_case):
    server, name, directory = process_case
    (directory/'block-cancel-reply').touch()
    with gateway(directory) as (_, client):
        rid, url = create(client, 'lost-cancel-client')
        with client.stream('GET', url) as response: assert next(events(response))[1]=='response.delta'
        with socket.create_connection((client.base_url.host, client.base_url.port), timeout=5) as connection:
            authorization = client.headers['Authorization']
            connection.sendall((f'POST /v1/llm/stream/{rid}/cancel HTTP/1.1\r\nHost: localhost\r\n'
                                f'Authorization: {authorization}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n').encode())
            wait_for(lambda: ['cancel_reply_waiting',''] in observations(directory))
        wait_for(lambda: observe(server,name,rid)[0]['status']=='canceled')
        wait_for(lambda: ['stream_closed','lost-cancel-client'] in observations(directory))
        (directory/'release-cancel-reply').touch()
        assert cancel(client,rid)=='canceled'
        assert_saved(server,name,rid,'canceled',['response.delta'])


@pytest.mark.parametrize('kill', [True, False])
@pytest.mark.parametrize('phase', ['cleanup', 'terminal'])
def test_exit_during_canceling_recovers_once_without_model_recall(process_case,kill,phase):
    server,name,directory=process_case
    (directory/'block-cancel-finish').touch()
    if phase=='cleanup': (directory/'block-provider-close').touch()
    with gateway(directory) as (process,client):
        rid,url=create(client,'interrupted-cancel')
        with client.stream('GET',url) as response: assert next(events(response))[1]=='response.delta'
        assert cancel(client,rid)=='canceling'
        wait_for(lambda: [('provider_cleanup_waiting' if phase=='cleanup' else 'cancel_finish_waiting'),''] in observations(directory))
        assert observe(server,name,rid)[0]['status']=='canceling'
        process.kill() if kill else process.terminate()
        assert process.wait(timeout=10) in (-signal.SIGKILL,-signal.SIGTERM)
    (directory/'release-cancel-finish').touch()
    for _ in range(2):
        with gateway(directory) as (_,client):
            assert cancel(client,rid)=='failed'
            row,_,_=assert_saved(server,name,rid,'failed',['response.delta'])
            assert row['error_code']=='execution_interrupted'
    assert [o for o in observations(directory) if o[0]=='stream']==[['stream','interrupted-cancel']]


@pytest.mark.parametrize('accepted', [False,True])
def test_owned_pg_outage_before_or_after_cancel_acceptance(tmp_path,accepted):
    # Infrastructure fault: execute serially with all other PG stop/restart suites.
    server=PostgresServer().start()
    document=yaml.safe_load(server.compose_path.read_text())
    document['services']['postgres']['ports']=[f'127.0.0.1:{server.port}:5432']
    server.compose_path.write_text(yaml.safe_dump(document))
    try:
        name=server.create_database();write_config(tmp_path/'config',server,name,registry());migrate(tmp_path/'config')
        (tmp_path/'f5o2_bootstrap.py').write_text(BOOTSTRAP)
        if accepted: (tmp_path/'block-cancel-finish').touch()
        with gateway(tmp_path) as (_,client):
            rid,url=create(client,'pg-cancel')
            with client.stream('GET',url) as response: assert next(events(response))[1]=='response.delta'
            if accepted:
                assert cancel(client,rid)=='canceling'
                wait_for(lambda:['cancel_finish_waiting',''] in observations(tmp_path))
                wait_for(lambda:['stream_closed','pg-cancel'] in observations(tmp_path))
            assert server.compose('stop','postgres').returncode==0
            if accepted: (tmp_path/'release-cancel-finish').touch()
            response=client.post(f'/v1/llm/stream/{rid}/cancel')
            assert response.status_code==503 and response.json()['code']=='storage_unavailable'
            assert client.get('/healthz').status_code==200
            server.start()
            assert wait_for(lambda:done(client,rid))=='canceled'
            assert_saved(server,name,rid,'canceled',['response.delta'])
            assert [o for o in observations(tmp_path) if o[0]=='stream']==[['stream','pg-cancel']]
    finally: server.close()
