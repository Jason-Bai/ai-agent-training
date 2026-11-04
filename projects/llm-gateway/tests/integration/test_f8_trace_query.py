"""Read-side contract checked against PostgreSQL independently of TraceStore."""
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event

from persistence.database import StorageUnavailable
from providers.mock import MockProvider
from test_f5o2_http import body, case, make_app, observe, server


def get(client, rid):
    response = client.get(f'/v1/traces/{rid}')
    assert response.status_code == 200, response.text
    assert set(response.json()) == {'success', 'code', 'message', 'data'}
    assert response.json()['data']['request_id'] == rid
    return response.json()['data']


def test_query_readonly_and_storage_errors(case, server, monkeypatch, caplog):
    name, config = case
    provider = MockProvider()
    app = make_app(config, provider)
    with TestClient(app) as client:
        rid = client.post('/v1/llm/stream', json=body()).headers['x-request-id']
        before = observe(server, name, rid)
        for _ in range(2):
            data = get(client, rid)
            assert data['status'] == 'pending'
            assert data['attempts'] == []
            assert data['usage'] == {'known_subtotal': None, 'unknown_attempts': []}
        assert observe(server, name, rid) == before
        assert not provider.calls and not provider.stream_calls
        for key, status, code in [('bad', 422, 'invalid_request'), (uuid4(), 404, 'not_found')]:
            response = client.get(f'/v1/traces/{key}')
            assert (response.status_code, response.json()['code']) == (status, code)

        async def fail_query(*args, **kwargs):
            raise StorageUnavailable()

        monkeypatch.setattr('api.routes.read_trace', fail_query)
        response = client.get(f'/v1/traces/{rid}')
        assert (response.status_code, response.json()['code']) == (503, 'storage_unavailable')
        assert f'request_id={rid} stage=trace_query code=storage_unavailable' in caplog.text


@pytest.mark.parametrize('counts', [[None, 85], [85, 85], [None, None], [0]])
def test_ordered_usage_and_sensitive_projection(case, server, counts):
    name, config = case
    marker = 'PRIVATE-DIAGNOSTIC-CONTENT'
    with TestClient(make_app(config)) as client:
        rid = client.post('/v1/llm/stream', json=body(messages=[{'role': 'user','content': marker}])).headers['x-request-id']
        row, _, _ = observe(server, name, rid)
        with server.connect(name) as conn:
            conn.execute("UPDATE tb_llm_request SET error_message=%s WHERE id=%s", (marker, row['id']))
            for i in reversed(range(len(counts))):
                count = counts[i]
                inp = None if count is None else min(60, count)
                out = None if count is None else count-inp
                conn.execute("""INSERT INTO tb_llm_attempt
                    (request_pk, attempt_no, model, provider, adapter, api_protocol, configured_model, status,
                     started_at, input_snapshot, input_tokens, output_tokens, total_tokens)
                    VALUES (%s,%s,'primary','provider','mock','chat_completions','model','running',
                            now(),%s,%s,%s,%s)""", (row['id'], i+1, marker, inp, out, count))
        data = get(client, rid)
        assert marker not in str(data)
        assert [a['attempt_no'] for a in data['attempts']] == list(range(1,len(counts)+1))
        assert all('input_snapshot' not in a for a in data['attempts'])
        assert 'input_snapshot' not in data and 'error_message' not in data
        assert data['usage']['unknown_attempts'] == [i for i,c in enumerate(counts,1) if c is None]
        known = [c for c in counts if c is not None]
        expected = {'input_tokens':sum(min(60,c) for c in known),'output_tokens':sum(max(0,c-60) for c in known),
                    'total_tokens':sum(known)} if known else None
        assert data['usage']['known_subtotal'] == expected


def test_reader_keeps_one_snapshot_across_writer_commit(case, server):
    name, config = case
    app = make_app(config)
    with TestClient(app) as client:
        rid = client.post('/v1/llm/stream', json=body()).headers['x-request-id']
        engine = app.state.storage.database.engine.sync_engine
        committed = False

        def advance_after_request_read(conn, cursor, statement, parameters, context, many):
            nonlocal committed
            if not committed and 'FROM tb_llm_request' in statement and 'timing_snapshot' in statement:
                committed = True
                with server.connect(name) as writer:
                    row = writer.execute("UPDATE tb_llm_request SET status='completed', last_event_seq=1 WHERE request_id=%s RETURNING id",(rid,)).fetchone()
                    writer.execute("""INSERT INTO tb_llm_attempt (request_pk,attempt_no,model,provider,adapter,
                       api_protocol,configured_model,status,started_at) VALUES (%s,1,'m','p','mock','chat_completions','m','completed',now())""",row)
        event.listen(engine, 'after_cursor_execute', advance_after_request_read)
        try:
            data = get(client, rid)
        finally:
            event.remove(engine, 'after_cursor_execute', advance_after_request_read)
        assert committed and data['status'] == 'pending' and data['attempts'] == []
        later = get(client, rid)
        assert later['status'] == 'completed' and len(later['attempts']) == 1


def test_arbitrary_provider_end_metadata_is_not_published(case):
    from providers.base import ProviderResult
    marker = 'private provider traceback and content'
    result = ProviderResult(text='answer', actual_model='model', finish_reason=marker,
                            upstream_status=marker, upstream_finish_reason=marker)
    with TestClient(make_app(case[1], MockProvider(result=result))) as client:
        rid = client.post('/v1/llm', json=body()).headers['x-request-id']
        data = get(client, rid)
        assert marker not in str(data)
        assert data['attempts'][0]['status'] == 'failed'


def test_legacy_skip_extraction_accepts_nul_inside_saved_user_message(case, server):
    with TestClient(make_app(case[1])) as client:
        response = client.post('/v1/llm/stream', json=body(messages=[{'role':'user', 'content':'embedded\x00nul'}]))
        assert response.status_code == 202
        rid = response.headers['x-request-id']
        with server.connect(case[0]) as conn:
            conn.execute('UPDATE tb_llm_request SET timing_snapshot=NULL WHERE request_id=%s', (rid,))
        assert get(client, rid)['recovery']['skipped_candidates'] == []


def test_new_pending_query_reads_metadata_without_input_snapshot(case, server):
    app = make_app(case[1])
    with TestClient(app) as client:
        rid = client.post('/v1/llm/stream', json=body()).headers['x-request-id']
        engine = app.state.storage.database.engine.sync_engine
        statements = []
        def capture(conn, cursor, statement, parameters, context, many):
            statements.append(statement)
        event.listen(engine, 'before_cursor_execute', capture)
        try:
            for _ in range(2):
                assert get(client, rid)['recovery']['skipped_candidates'] == []
        finally:
            event.remove(engine, 'before_cursor_execute', capture)
        assert not any('input_snapshot' in stmt for stmt in statements)


@pytest.mark.parametrize(('point', 'stage'), [
    ('create_pending', 'create_pending'), ('claim_stream', 'claim_stream'), ('accept_cancel', 'accept_cancel'),
])
def test_stream_lifecycle_storage_failure_logged(case, server, point, stage, monkeypatch, caplog):
    provider = MockProvider()
    app = make_app(case[1], provider)
    async def fail(*args, **kwargs):
        raise StorageUnavailable()

    with TestClient(app) as client, monkeypatch.context() as patch:
        if point != 'create_pending':
            rid = client.post('/v1/llm/stream', json=body()).headers['x-request-id']
        patch.setattr(app.state.storage, point, fail)
        if point == 'create_pending':
            response = client.post('/v1/llm/stream', json=body())
            rid = response.headers['x-request-id']
        elif point == 'claim_stream':
            response = client.get(f'/v1/llm/stream/{rid}/events')
        else:
            response = client.post(f'/v1/llm/stream/{rid}/cancel')
        assert (response.status_code, response.json()['code']) == (503, 'storage_unavailable')
        assert not provider.stream_calls
        assert f'request_id={rid} stage={stage} attempt_no=None' in caplog.text
