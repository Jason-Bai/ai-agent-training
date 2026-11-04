from pricing_support import copy_pricing
"""Six explicitly authorized real calls; PostgreSQL remains test-owned."""
from datetime import datetime
from decimal import Decimal

import asyncio
import socket

import httpx2
from legacy_identity import HEADERS
import pytest
import pytest_asyncio
from psycopg.rows import dict_row
import uvicorn

from main import create_app
from postgres_support import PostgresServer, write_config, migrate
from services.models import DEFAULT_CONFIG_DIR
from settings import Settings
from test_real_providers import real_registry
from utils.json_codec import loads


pytestmark = pytest.mark.live


@pytest.fixture(scope='module')
def live_pg(request):
    if not request.config.getoption('--run-live'):
        pytest.fail('live fixture requires explicit --run-live')
    server = PostgresServer().start()
    try:
        yield server
    finally:
        server.close()


@pytest_asyncio.fixture
async def accepted_server(request, tmp_path, live_pg, monkeypatch):
    credentials = DEFAULT_CONFIG_DIR / 'credentials.yaml'
    if not credentials.is_file():
        pytest.fail('本机尚未配置 Provider Key；请本地配置，不要在聊天中发送')
    name = live_pg.create_database()
    directory = write_config(tmp_path / 'config', live_pg, name, real_registry())
    (directory / 'credentials.yaml').symlink_to(credentials)
    copy_pricing(directory, DEFAULT_CONFIG_DIR)
    await asyncio.to_thread(migrate, directory)
    app = create_app(settings=Settings(env='test', _env_file=None),
                     model_registry=real_registry(), config_dir=directory)
    from upstream_observation import install, snapshot_failures
    install(app,tmp_path,monkeypatch)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', lifespan='on'))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(15):
            while not server.started:
                if task.done():
                    await task
                    pytest.fail('验收服务未启动')
                await asyncio.sleep(.01)
        async with httpx2.AsyncClient(headers=HEADERS, base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=40) as client:
            yield app, client, live_pg, name
    finally:
        await snapshot_failures(app,tmp_path)
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()
        live_pg.drop_database(name)


@pytest.mark.parametrize('model,adapter,protocol', [
    ('general-primary', 'deepseek', 'chat_completions'),
    ('general-backup', 'openai_compatible', 'responses'),
])
@pytest.mark.parametrize('mode', ['text', 'structured', 'streaming'])
async def test_real_call_and_committed_records(accepted_server, model, adapter, protocol, mode, record_property, monkeypatch):
    from providers import base, streaming
    # Observe only upstream metering, before provider-specific normalization.
    # Never capture request headers, messages or response content.
    wire_usage = []
    actual_observer = base.observe_reported_metadata

    def observe(raw, observed, *, chat):
        if raw.get('usage') is not None:
            wire_usage.append(dict(raw['usage']))
        return actual_observer(raw, observed, chat=chat)

    monkeypatch.setattr(base, 'observe_reported_metadata', observe)
    monkeypatch.setattr(streaming, 'observe_reported_metadata', observe)
    app, client, pg, name = accepted_server
    payload = {'model': model, 'prompt': {'name': 'basic_text', 'version': '1.0.0'},
               'messages': [{'role': 'user', 'content': '用一句话解释 TTFT，不超过四十字。'}],
               'max_output_tokens': 128, 'timeout_seconds': 30}
    if mode == 'structured':
        payload['prompt'] = {'name': 'context_answer', 'version': '1.2.0',
                             'variables': {'context': '订单 A123 的商品是充电器。'}}
        payload['messages'][0]['content'] = '提取订单号和商品。'
        payload['response_schema'] = {'type': 'object', 'properties': {
            'order_id': {'type': 'string'}, 'product': {'type': 'string'}},
            'required': ['order_id', 'product'], 'additionalProperties': False}
    path = '/v1/llm/stream' if mode == 'streaming' else '/v1/llm'
    if mode == 'streaming':
        received = []
        sequences = []
        created = await client.post(path, json=payload)
        assert created.status_code == 202
        request_id = created.json()['data']['request_id']
        assert created.json()['data']['status'] == 'pending'
        assert created.headers['x-request-id'] == request_id
        async with client.stream('GET', path + '/' + request_id + '/events') as response:
            assert response.status_code == 200
            assert response.headers['x-request-id'] == request_id
            frame = []
            async for line in response.aiter_lines():
                if line:
                    frame.append(line)
                elif frame:
                    kind = next(line[7:] for line in frame if line.startswith('event: '))
                    value = loads(next(line[6:] for line in frame if line.startswith('data: ')))
                    ids = [line[4:] for line in frame if line.startswith('id: ')]
                    assert value['request_id'] == request_id
                    if kind == 'response.heartbeat':
                        assert ids == []
                    else:
                        assert len(ids) == 1
                        sequences.append(int(ids[0]))
                    received.append((kind, value))
                    frame = []
        assert not frame
        assert received[-1][0] == 'response.completed'
        assert sequences == list(range(1, len(sequences) + 1))
        assert any(kind == 'response.delta' and value['delta'].strip() for kind, value in received)
    else:
        response = await client.post(path, json=payload)
        assert response.status_code == 200, response.json().get('code')
        request_id = response.headers['x-request-id']
        result = response.json()['data']
        assert result['text'].strip()
        if mode == 'structured':
            assert result['parsed'] == {'order_id': 'A123', 'product': '充电器'}
    with pg.connect(name, row_factory=dict_row) as connection:
        record = connection.execute('SELECT * FROM tb_llm_request WHERE request_id=%s', (request_id,)).fetchone()
        attempts = connection.execute('SELECT * FROM tb_llm_attempt WHERE request_pk=%s', (record['id'],)).fetchall()
        events = connection.execute('SELECT * FROM tb_llm_stream_event WHERE request_pk=%s ORDER BY seq', (record['id'],)).fetchall()
    assert record['status'] == 'completed'
    assert len(attempts) == 1 and attempts[0]['status'] == 'completed'
    attempt = attempts[0]
    assert (attempt['adapter'], attempt['api_protocol']) == (adapter, protocol)
    assert attempt['actual_model'] and attempt['input_tokens'] is not None
    assert attempt['total_tokens'] == attempt['input_tokens'] + attempt['output_tokens']
    diagnostic = await client.get(f'/v1/traces/{request_id}')
    assert diagnostic.status_code == 200, diagnostic.text
    data = diagnostic.json()['data']
    assert (data['request_id'], data['operation'], data['status']) == (
        request_id, record['operation'], record['status'])
    detail, = data['attempts']
    assert (detail['attempt_no'], detail['api_protocol'], detail['actual_model']) == (
        attempt['attempt_no'], protocol, attempt['actual_model'])
    reported = {field: attempt[field] for field in ('input_tokens', 'output_tokens', 'total_tokens')}
    assert detail['usage'] == data['usage']['known_subtotal'] == reported
    assert data['usage']['unknown_attempts'] == []
    if mode == 'streaming':
        expected = [(kind, value) for kind, value in received if kind != 'response.heartbeat']
        assert [(event['event_type'], loads(event['payload'])) for event in events] == expected
        assert [event['seq'] for event in events] == sequences
    else:
        assert events == []
    snapshot = loads(record['input_snapshot'])
    assert snapshot['max_output_tokens'] == 128
    for key, value in {'request_id': request_id, 'mode': mode, 'adapter': adapter, 'protocol': protocol,
                       'actual_model': attempt['actual_model'], 'input_tokens': attempt['input_tokens'],
                       'output_tokens': attempt['output_tokens'], 'events': len(events)}.items():
        record_property(key, value)

    raw = wire_usage[-1]
    if protocol == 'chat_completions':
        billed_input, cached, billed_output = raw['prompt_tokens'], raw['prompt_cache_hit_tokens'], raw['completion_tokens']
        assert cached + raw['prompt_cache_miss_tokens'] == billed_input
    else:
        billed_input, cached, billed_output = raw['input_tokens'], raw['input_tokens_details']['cached_tokens'], raw['output_tokens']
    billing_url = f'/v1/billing/requests/{request_id}'
    accounting = (await client.get(billing_url)).json()['data']
    charge, = accounting['attempts']
    with pg.connect(name, row_factory=dict_row) as connection:
        ledger, = connection.execute('SELECT * FROM tb_billing_attempt WHERE request_pk=%s', (record['id'],)).fetchall()
    quote = ledger['quote']
    assert quote == charge['quote']
    assert (quote['supplier'], quote['model'], quote['currency']) == ('deepseek', 'deepseek-flash', 'CNY')
    assert charge['usage_source'] == 'real' and charge['cached_input_tokens'] == ledger['cached_input_tokens'] == cached
    assert (billed_input, billed_output) == (attempt['input_tokens'], attempt['output_tokens'])
    if charge['state'] == 'settled':
        expected = (cached * Decimal(quote['hit']) + (billed_input-cached) * Decimal(quote['miss'])
                    + billed_output * Decimal(quote['output'])) / 1_000_000
        assert ledger['agent_charge'] == ledger['supplier_cost'] == Decimal(charge['agent_charge']) == expected
    else:
        # Calendar coverage or a tariff boundary can prevent an automatic charge.
        assert charge['state'] == 'pending' and charge['agent_charge'] is None
        assert quote['period'] == 'unknown' or (
            quote['valid_until'] and attempt['ended_at'] >= datetime.fromisoformat(quote['valid_until']))
    if mode == 'streaming':
        before = len(wire_usage)
        replay = await client.get(f'{path}/{request_id}/events', params={'after_seq': sequences[-2]})
        assert replay.status_code == 200 and 'event: response.completed' in replay.text
        assert len(wire_usage) == before
        assert (await client.get(billing_url)).json()['data'] == accounting
    for key, value in {'cached_input_tokens': cached, 'billing_state': charge['state'], 'price_version': quote['version'],
                       'price_period': quote['period'], 'agent_charge': charge['agent_charge'],
                       'supplier_cost': charge['supplier_cost']}.items():
        record_property(key, value)
