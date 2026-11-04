"""F6 C: deterministic commit winners, repeated cancellation and request isolation."""
import asyncio

import pytest

from f6_cases import (server, case, application, ControlledProvider, create, cancel_url,
                      poll, observe, assert_events, until, body)
from providers.mock import MockProvider
from providers.base import ProviderStreamEvent


async def test_f6_twenty_cancelers_share_one_cleanup(server, case):
    name, config = case
    provider = ControlledProvider(first=True)
    provider.close_release.clear()
    async with application(config, provider) as (_, client):
        rid = await create(client)
        subscriber = asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
        await asyncio.wait_for(provider.entered.wait(), 3)
        await until(lambda: len(observe(server, name, rid)[2]) == 1)
        results = await asyncio.gather(*(client.post(cancel_url(rid)) for _ in range(20)))
        assert all(r.status_code == 202 and r.json()['data']['status'] == 'canceling' for r in results)
        await asyncio.wait_for(provider.closing.wait(), 3)
        assert provider.stream_close_calls == 1
        provider.close_release.set()
        await poll(client, rid)
        await subscriber
        assert_events(server, name, rid)
        assert len(provider.stream_calls) == 1


async def test_f6_cancel_before_twenty_first_subscriptions_never_starts_provider(server, case):
    name, config = case
    provider = MockProvider()
    async with application(config, provider) as (_, client):
        rid = await create(client)
        await poll(client, rid)
        results = await asyncio.gather(*(client.get(f'/v1/llm/stream/{rid}/events') for _ in range(20)))
        assert all(r.status_code == 200 for r in results)
        assert provider.stream_calls == []
        assert_events(server, name, rid)


@pytest.mark.parametrize('winner', ['cancel', 'completed'])
async def test_f6_completed_and_cancel_follow_database_commit_winner(server, case, monkeypatch, winner):
    name, config = case
    provider = MockProvider()
    async with application(config, provider) as (app, client):
        entered, release = asyncio.Event(), asyncio.Event()
        original = app.state.storage.finish
        async def finish(trace, *args, **kwargs):
            if kwargs.get('error_code') is None:
                entered.set()
                await release.wait()
            return await original(trace, *args, **kwargs)
        monkeypatch.setattr(app.state.storage, 'finish', finish)
        rid = await create(client)
        subscriber = asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
        await asyncio.wait_for(entered.wait(), 3)
        if winner == 'cancel':
            await poll(client, rid)
            release.set()
        else:
            release.set()
            await until(lambda: observe(server, name, rid)[0]['status'] == 'completed')
            await poll(client, rid, 'completed')
        await subscriber
        row, attempts, events = observe(server, name, rid)
        assert row['status'] == ('canceled' if winner == 'cancel' else 'completed')
        assert [e['event_type'] for e in events].count('response.canceling') == int(winner == 'cancel')
        assert len(attempts) == len(provider.stream_calls) == 1
        assert sum(e['event_type'] in {'response.completed','response.failed','response.canceled'} for e in events) == 1


@pytest.mark.parametrize('winner', ['cancel', 'failed'])
async def test_f6_failed_and_cancel_follow_database_commit_winner(server, case, monkeypatch, winner):
    name, config = case
    provider = MockProvider(stream_events=[ProviderStreamEvent(status='failed', error_code='provider_unavailable')])
    async with application(config, provider) as (app, client):
        entered, release = asyncio.Event(), asyncio.Event()
        original = app.state.storage.finish
        async def finish(trace, *args, **kwargs):
            if kwargs.get('error_code') == 'provider_unavailable':
                entered.set(); await release.wait()
            return await original(trace, *args, **kwargs)
        monkeypatch.setattr(app.state.storage, 'finish', finish)
        rid = await create(client)
        subscriber = asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
        await asyncio.wait_for(entered.wait(), 3)
        if winner == 'cancel':
            await poll(client, rid); release.set()
        else:
            release.set()
            await until(lambda: observe(server, name, rid)[0]['status'] == 'failed')
            await poll(client, rid, 'failed')
        await subscriber
        row, attempts, events = observe(server, name, rid)
        assert row['status'] == ('canceled' if winner == 'cancel' else 'failed')
        assert len(attempts) == len(provider.stream_calls) == 1
        assert sum(e['event_type'] in {'response.completed','response.failed','response.canceled'} for e in events) == 1


async def test_f6_other_request_on_shared_provider_continues(server, case):
    name, config = case
    provider = ControlledProvider(first=True)
    async with application(config, provider) as (_, client):
        a, b = await create(client), await create(client)
        first = asyncio.create_task(client.get(f'/v1/llm/stream/{a}/events'))
        await asyncio.wait_for(provider.entered.wait(), 3)
        second = asyncio.create_task(client.get(f'/v1/llm/stream/{b}/events'))
        await until(lambda: len(provider.stream_calls) == 2)
        await poll(client, a)
        provider.release.set()
        await first; await second
        assert_events(server, name, a)
        row, attempts, events = observe(server, name, b)
        assert row['status'] == 'completed' and len(attempts) == 1
        assert not any(e['event_type'] == 'response.canceling' for e in events)
        assert provider.close_calls == 0


@pytest.mark.parametrize('stage', ['registered', 'execution', 'before_attempt', 'after_attempt'])
async def test_f6_cancel_at_generation_registration_and_attempt_boundaries(server, case, monkeypatch, stage):
    from services.streaming import StreamingExecution
    name, config = case
    provider = MockProvider()
    async with application(config, provider) as (app, client):
        entered, release = asyncio.Event(), asyncio.Event()
        manager = app.state.streaming_manager
        if stage == 'registered':
            original = manager._generate
            async def generate(*args, **kwargs):
                entered.set(); await release.wait()
                return await original(*args, **kwargs)
            monkeypatch.setattr(manager, '_generate', generate)
        elif stage == 'execution':
            original = StreamingExecution.run_background
            async def run(execution):
                entered.set(); await release.wait()
                return await original(execution)
            monkeypatch.setattr(StreamingExecution, 'run_background', run)
        else:
            original = app.state.storage.begin_attempt
            async def begin(*args, **kwargs):
                if stage == 'after_attempt':
                    await original(*args, **kwargs)
                entered.set(); await release.wait()
                if stage == 'before_attempt':
                    return await original(*args, **kwargs)
            monkeypatch.setattr(app.state.storage, 'begin_attempt', begin)
        rid = await create(client)
        # Direct application subscription registers the actual production owner.
        await manager.subscribe(rid, 0)
        await asyncio.wait_for(entered.wait(), 3)
        assert rid in manager.tasks
        assert (rid in manager.executions) == (stage != 'registered')
        assert len(observe(server, name, rid)[1]) == int(stage == 'after_attempt')
        assert provider.stream_calls == []
        await poll(client, rid)
        row, attempts, events = assert_events(server, name, rid)
        assert provider.stream_calls == []
        assert len(attempts) == int(stage == 'after_attempt')
        assert not attempts or attempts[0]['status'] == 'canceled'
        assert row['error_code'] is None
        assert app.state.traces.requests[rid].error_code is None


@pytest.mark.parametrize('winner', ['cancel', 'timeout'])
async def test_f6_request_timeout_and_cancel_have_one_database_terminal(server, case, monkeypatch, winner):
    name, config = case
    provider = ControlledProvider()
    async with application(config, provider) as (app, client):
        entering_timeout, release = asyncio.Event(), asyncio.Event()
        original = app.state.storage.finish
        async def timeout_finish(trace, *args, **kwargs):
            if kwargs.get('error_code') == 'request_timeout':
                entering_timeout.set(); await release.wait()
            return await original(trace, *args, **kwargs)
        monkeypatch.setattr(app.state.storage, 'finish', timeout_finish)
        response = await client.post('/v1/llm/stream', json=body(timeout_seconds=1))
        assert response.status_code == 202, response.text
        rid = response.json()['data']['request_id']
        subscriber = asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
        await asyncio.wait_for(provider.entered.wait(), 3)
        await asyncio.wait_for(entering_timeout.wait(), 4)
        if winner == 'cancel':
            await poll(client, rid)
            release.set()
        else:
            release.set()
            await until(lambda: observe(server, name, rid)[0]['status'] == 'failed')
            await poll(client, rid, 'failed')
        await subscriber
        row, attempts, events = observe(server, name, rid)
        assert row['status'] == ('canceled' if winner == 'cancel' else 'failed')
        assert row['error_code'] == (None if winner == 'cancel' else 'request_timeout')
        assert len(attempts) == len(provider.stream_calls) == provider.stream_close_calls == 1
        assert sum(e['event_type'] in {'response.canceled','response.failed','response.completed'} for e in events) == 1


async def test_f6_twenty_subscribers_registered_before_cancel_share_one_generation(server, case):
    name, config = case
    provider = ControlledProvider(first=True)
    async with application(config, provider) as (app, client):
        rid = await create(client); url = f'/v1/llm/stream/{rid}/events'
        subscriptions = [asyncio.create_task(client.get(url)) for _ in range(20)]
        await until(lambda: len(app.state.streaming_manager.subscriptions) == 20)
        await asyncio.wait_for(provider.entered.wait(), 3)
        assert len(provider.stream_calls) == 1
        await poll(client, rid)
        replies = await asyncio.gather(*subscriptions)
        assert all(response.status_code == 200 for response in replies)
        from f6_cases import frames
        assert all(frames(response.text) == frames(replies[0].text) for response in replies)
        assert len(provider.stream_calls) == provider.stream_close_calls == 1
        assert len(assert_events(server, name, rid)[1]) == 1


async def test_f6_blocked_old_cursor_does_not_delay_other_cursor_cancel_and_resume(server, case):
    from test_f5o2_delivery import invoke
    from f6_cases import frames
    name, config = case
    provider = ControlledProvider(first=True)
    async with application(config, provider) as (app, client):
        rid = await create(client); url = f'/v1/llm/stream/{rid}/events'
        blocked, disconnect = asyncio.Event(), asyncio.Event()
        delivered = []
        async def send(message):
            if b'event: response.delta' in message.get('body', b''):
                blocked.set()
                await asyncio.Event().wait()
            delivered.append(message)
        old = asyncio.create_task(invoke(app, 'GET', url, send, disconnect=disconnect))
        await asyncio.wait_for(blocked.wait(), 3)
        assert observe(server, name, rid)[2][0]['seq'] == 1
        healthy = asyncio.create_task(client.get(url+'?after_seq=1'))
        await poll(client, rid)
        result = await asyncio.wait_for(healthy, 3)
        assert [f[0] for f in frames(result.text)] == [2, 3]
        assert [f[1] for f in frames(result.text)] == ['response.canceling','response.canceled']
        assert not old.done()  # a real send operation, not a non-reading client
        disconnect.set()
        await asyncio.wait_for(old, 3)
        assert not any(b'event: response.delta' in m.get('body', b'') for m in delivered)
        resumed = await client.get(url+'?after_seq=0')
        items = frames(resumed.text)
        assert [f[0] for f in items] == [1, 2, 3]
        assert [f[1] for f in items] == ['response.delta','response.canceling','response.canceled']
        assert_events(server, name, rid)
        assert len(provider.stream_calls) == provider.stream_close_calls == 1
