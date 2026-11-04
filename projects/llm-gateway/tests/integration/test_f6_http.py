"""F6 A/L: observed HTTP state plus independent PostgreSQL side effects."""
import asyncio
from uuid import uuid4

import pytest

from f6_cases import (server, case, application, ControlledProvider, create, cancel_url,
                      poll, observe, assert_events, until, body)
from providers.mock import MockProvider


async def test_f6_pending_cancel_without_subscription_has_zero_provider_calls(server, case):
    name, config = case
    provider = MockProvider()
    async with application(config, provider) as (app, client):
        rid = await create(client)
        await poll(client, rid)
        row, attempts, events = assert_events(server, name, rid)
        assert attempts == [] and provider.stream_calls == []
        assert [e['event_type'] for e in events] == ['response.canceling', 'response.canceled']
        assert (await client.get(f'/v1/llm/stream/{rid}/events')).status_code == 200
        assert provider.stream_calls == []
        assert len(app.state.traces.requests) == 1


@pytest.mark.parametrize('content', [b'{}', b'null', b'text', b'{bad'])
async def test_f6_rejects_every_nonempty_body(server, case, content):
    name, config = case
    provider = MockProvider()
    async with application(config, provider) as (_, client):
        rid = await create(client)
        response = await client.post(cancel_url(rid), content=content)
        assert response.status_code == 422
        assert response.json()['code'] == 'invalid_request'
        assert observe(server, name, rid)[0]['status'] == 'pending'
        assert provider.stream_calls == []


@pytest.mark.parametrize('suffix', ['?x=1', '?x=1&x=2', '?after_seq=0'])
async def test_f6_rejects_queries_without_side_effects(server, case, suffix):
    name, config = case
    async with application(config, MockProvider()) as (_, client):
        rid = await create(client)
        response = await client.post(cancel_url(rid)+suffix)
        assert response.status_code == 422 and response.json()['code'] == 'invalid_request'
        assert observe(server, name, rid)[0]['status'] == 'pending'


async def test_f6_missing_bad_uuid_ordinary_and_rejected_are_not_cancelable(server, case):
    _, config = case
    async with application(config, MockProvider()) as (_, client):
        ordinary = (await client.post('/v1/llm', json=body())).headers['x-request-id']
        rejected = (await client.post('/v1/llm/stream', json=body(model='missing'))).headers['x-request-id']
        for rid in [str(uuid4()), ordinary, rejected]:
            response = await client.post(cancel_url(rid))
            assert response.status_code == 404 and response.json()['code'] == 'not_found'
        assert (await client.post(cancel_url('invalid'))).status_code == 422


@pytest.mark.parametrize('first', [False, True])
async def test_f6_running_cancel_before_or_after_delta_cleans_once(server, case, first):
    name, config = case
    provider = ControlledProvider(first=first)
    provider.close_release.clear()
    async with application(config, provider) as (_, client):
        rid = await create(client)
        subscriber = asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
        await asyncio.wait_for(provider.entered.wait(), 3)
        if first:
            await until(lambda: len(observe(server, name, rid)[2]) == 1)
        response = await client.post(cancel_url(rid))
        assert response.status_code == 202 and response.json()['data']['status'] == 'canceling'
        await asyncio.wait_for(provider.closing.wait(), 3)
        for _ in range(4):
            assert (await client.post(cancel_url(rid))).json()['data']['status'] == 'canceling'
        provider.close_release.set()
        await poll(client, rid)
        assert (await subscriber).status_code == 200
        row, attempts, events = assert_events(server, name, rid)
        assert len(provider.stream_calls) == len(attempts) == provider.stream_close_calls == 1
        assert len([e for e in events if e['event_type'] == 'response.delta']) == int(first)
        assert provider.close_calls == 0
        assert attempts[0]['status'] == 'canceled'
        assert attempts[0]['input_tokens'] is None


async def test_f6_openapi_models_json_for_all_cancel_statuses(server, case):
    _, config = case
    async with application(config, MockProvider()) as (_, client):
        responses = (await client.get('/openapi.json')).json()['paths']['/v1/llm/stream/{request_id}/cancel']['post']['responses']
        for code in ['200', '202', '404', '422', '500', '503']:
            assert 'application/json' in responses[code]['content']
            assert 'text/event-stream' not in responses[code]['content']


async def test_f6_storage_and_internal_errors_are_uniform_without_false_terminal(server, case, monkeypatch):
    from persistence.database import StorageUnavailable
    name, config = case
    provider = MockProvider()
    async with application(config, provider) as (app, client):
        rid = await create(client)
        original = app.state.storage.get_stream
        for error, status, code in [(StorageUnavailable(), 503, 'storage_unavailable'),
                                     (RuntimeError('secret private detail'), 500, 'internal_error')]:
            async def broken(*args, **kwargs):
                raise error
            monkeypatch.setattr(app.state.storage, 'get_stream', broken)
            response = await client.post(cancel_url(rid))
            assert response.status_code == status
            assert response.json()['code'] == code
            assert response.json()['success'] is False and response.json()['data'] is None
            assert 'secret private detail' not in response.text
            assert (await client.get('/healthz')).status_code == 200
            assert observe(server, name, rid)[0]['status'] == 'pending'
        monkeypatch.setattr(app.state.storage, 'get_stream', original)
        assert provider.stream_calls == []


async def test_f6_closing_manager_rejects_new_cancel_but_preserves_existing_terminal(server, case):
    name, config = case
    provider = MockProvider()
    async with application(config, provider) as (app, client):
        completed = await create(client)
        await client.get(f'/v1/llm/stream/{completed}/events')
        pending = await create(client)
        app.state.streaming_manager.closing = True
        try:
            response = await client.post(cancel_url(pending))
            assert response.status_code == 503
            assert response.json()['code'] == 'execution_interrupted'
            assert observe(server, name, pending)[0]['status'] == 'pending'
            await poll(client, completed, 'completed')
        finally:
            app.state.streaming_manager.closing = False


async def test_f6_cancel_http_task_disconnect_does_not_own_accepted_operation(server, case, monkeypatch):
    name, config = case
    provider = ControlledProvider()
    async with application(config, provider) as (app, client):
        rid = await create(client)
        subscriber = asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
        await asyncio.wait_for(provider.entered.wait(), 3)
        committed, release = asyncio.Event(), asyncio.Event()
        original = app.state.storage.accept_cancel
        async def accepted(*args, **kwargs):
            result = await original(*args, **kwargs)
            committed.set(); await release.wait()
            return result
        monkeypatch.setattr(app.state.storage, 'accept_cancel', accepted)
        cancellation = asyncio.create_task(client.post(cancel_url(rid)))
        await asyncio.wait_for(committed.wait(), 3)
        assert observe(server, name, rid)[0]['status'] == 'canceling'
        cancellation.cancel()
        await asyncio.gather(cancellation, return_exceptions=True)
        release.set()
        await asyncio.wait_for(provider.closing.wait(), 3)
        await poll(client, rid)
        await subscriber
        assert_events(server, name, rid)
        assert len(provider.stream_calls) == provider.stream_close_calls == 1


async def test_f6_pending_canceling_is_committed_before_terminal_and_visible_to_pollers(server, case, monkeypatch):
    name, config = case
    provider = MockProvider()
    async with application(config, provider) as (app, client):
        saving, release = asyncio.Event(), asyncio.Event()
        original = app.state.storage.finish_cancel
        async def delayed(*args, **kwargs):
            saving.set(); await release.wait()
            return await original(*args, **kwargs)
        monkeypatch.setattr(app.state.storage, 'finish_cancel', delayed)
        rid = await create(client)
        first = await client.post(cancel_url(rid))
        assert first.status_code == 202
        await asyncio.wait_for(saving.wait(), 3)
        row, attempts, events = observe(server, name, rid)
        assert row['status'] == 'canceling'
        assert [e['event_type'] for e in events] == ['response.canceling']
        assert attempts == [] and provider.stream_calls == []
        for _ in range(3):
            assert (await client.post(cancel_url(rid))).json()['data']['status'] == 'canceling'
        release.set()
        await poll(client, rid)
        assert_events(server, name, rid)


@pytest.mark.parametrize('phase', ['before_transaction', 'waiting_for_row_lock'])
async def test_f6_disconnected_cancel_caller_cannot_abandon_preaccept_owner(server, case, monkeypatch, phase):
    name, config = case
    provider = MockProvider()
    async with application(config, provider) as (app, client):
        rid = await create(client)
        entered, release = asyncio.Event(), asyncio.Event()
        original = app.state.storage.accept_cancel
        async def accept(*args, **kwargs):
            entered.set()
            if phase == 'before_transaction':
                await release.wait()
            return await original(*args, **kwargs)
        monkeypatch.setattr(app.state.storage, 'accept_cancel', accept)
        with server.connect(name) as blocker:
            if phase == 'waiting_for_row_lock':
                blocker.execute('BEGIN')
                blocker.execute('SELECT id FROM tb_llm_request WHERE request_id=%s FOR UPDATE', (rid,))
            post = asyncio.create_task(client.post(cancel_url(rid)))
            await asyncio.wait_for(entered.wait(), 3)
            if phase == 'waiting_for_row_lock':
                def blocked_on_lock():
                    with server.connect(name) as witness:
                        return witness.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'").fetchone()[0] > 0
                await until(blocked_on_lock)
            assert observe(server, name, rid)[0]['status'] == 'pending'
            post.cancel()
            await asyncio.gather(post, return_exceptions=True)
            release.set()
            if phase == 'waiting_for_row_lock':
                blocker.execute('COMMIT')
        # No second cancel request is needed to drive acceptance or cleanup.
        await until(lambda: observe(server, name, rid)[0]['status'] == 'canceled')
        assert_events(server, name, rid)
        assert provider.stream_calls == []
        await poll(client, rid)
