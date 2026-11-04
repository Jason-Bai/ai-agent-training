"""F6 F/R: real transactions with commit acknowledgement loss and delayed owners."""
import asyncio
from contextvars import ContextVar

import pytest
from sqlalchemy.ext.asyncio import AsyncSessionTransaction

from f6_cases import (server, case, application, ControlledProvider, create, cancel_url,
                      poll, observe, assert_events, until)
from providers.mock import MockProvider
from providers.base import ProviderStreamEvent
from contracts import Usage
from persistence.database import StorageUnavailable


def acknowledgement_loss(app, monkeypatch, method, committed):
    marked = ContextVar('f6_transaction', default=False)
    original_method = getattr(app.state.storage, method)
    original_exit = AsyncSessionTransaction.__aexit__
    receipt = {'count': 0}
    async def exit_transaction(transaction, *args):
        if not marked.get() or receipt['count']:
            return await original_exit(transaction, *args)
        receipt['count'] += 1
        if committed:
            await original_exit(transaction, *args)
        else:
            await transaction.rollback()
        raise ConnectionError('controlled private acknowledgement loss')
    async def operation(*args, **kwargs):
        token = marked.set(True)
        try:
            return await original_method(*args, **kwargs)
        finally:
            marked.reset(token)
    monkeypatch.setattr(AsyncSessionTransaction, '__aexit__', exit_transaction)
    monkeypatch.setattr(app.state.storage, method, operation)
    return receipt


async def test_f6_cleanup_exception_is_failed_not_canceled(server, case):
    name, config = case
    provider = ControlledProvider(close_error=True)
    async with application(config, provider) as (_, client):
        rid = await create(client)
        subscriber = asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
        await asyncio.wait_for(provider.entered.wait(), 3)
        response = await poll(client, rid, 'failed')
        assert 'private cleanup exception' not in response.text
        delivered = await subscriber
        assert 'private cleanup exception' not in delivered.text
        row, attempts, events = assert_events(server, name, rid, 'failed')
        assert row['error_code'] == 'cancel_cleanup_failed'
        assert attempts[0]['cleanup_error_code'] == 'stream_cleanup_error'
        assert provider.stream_close_calls == 1


@pytest.mark.parametrize('committed', [True, False])
async def test_f6_accept_ack_loss_observes_actual_commit_before_stopping(server, case, monkeypatch, committed):
    name, config = case
    provider = ControlledProvider(first=True)
    async with application(config, provider) as (app, client):
        rid = await create(client)
        subscriber = asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
        await asyncio.wait_for(provider.entered.wait(), 3)
        await until(lambda: len(observe(server, name, rid)[2]) == 1)
        receipt = acknowledgement_loss(app, monkeypatch, 'accept_cancel', committed)
        response = await client.post(cancel_url(rid))
        assert response.status_code == 503 and response.json()['code'] == 'storage_unavailable'
        if committed:
            await poll(client, rid)
            assert_events(server, name, rid)
        else:
            # No second cancellation request: rolled-back intent must not stop generation.
            assert observe(server, name, rid)[0]['status'] == 'running'
            provider.release.set()
            await until(lambda: observe(server, name, rid)[0]['status'] == 'completed')
            assert not any(e['event_type'] == 'response.canceling' for e in observe(server, name, rid)[2])
        await subscriber
        assert receipt['count'] == 1 and len(provider.stream_calls) == 1


@pytest.mark.parametrize('committed', [True, False])
@pytest.mark.parametrize('cleanup_failed', [True, False])
async def test_f6_terminal_ack_loss_never_duplicates_terminal(server, case, monkeypatch, committed, cleanup_failed):
    name, config = case
    provider = ControlledProvider(close_error=cleanup_failed)
    async with application(config, provider) as (app, client):
        receipt = acknowledgement_loss(app, monkeypatch, 'finish_cancel', committed)
        rid = await create(client)
        subscriber = asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
        await asyncio.wait_for(provider.entered.wait(), 3)
        expected = 'failed' if cleanup_failed else 'canceled'
        await poll(client, rid, expected)
        await subscriber
        assert_events(server, name, rid, expected)
        assert receipt['count'] == 1 and provider.stream_close_calls == 1
        assert len(provider.stream_calls) == 1


async def test_f6_deferred_cancel_result_preserves_usage_against_reconcile(server, case, monkeypatch):
    name, config = case
    provider = MockProvider(stream_events=[ProviderStreamEvent(delta='订单 A123'),
        ProviderStreamEvent(status='completed', upstream_terminal_received=True,
                            usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5))])
    async with application(config, provider) as (app, client):
        completed, release_completion = asyncio.Event(), asyncio.Event()
        original_finish = app.state.storage.finish
        async def delay_completion(trace, *args, **kwargs):
            if kwargs.get('error_code') is None:
                completed.set(); await release_completion.wait()
            return await original_finish(trace, *args, **kwargs)
        monkeypatch.setattr(app.state.storage, 'finish', delay_completion)
        original_cancel = app.state.storage.finish_cancel
        save_enabled = asyncio.Event()
        attempted = asyncio.Event()
        async def delay_cancel(*args, **kwargs):
            attempted.set()
            if not save_enabled.is_set():
                raise StorageUnavailable()
            return await original_cancel(*args, **kwargs)
        monkeypatch.setattr(app.state.storage, 'finish_cancel', delay_cancel)
        rid = await create(client); url = f'/v1/llm/stream/{rid}/events'
        subscriber = asyncio.create_task(client.get(url))
        await asyncio.wait_for(completed.wait(), 3)
        response = await client.post(cancel_url(rid))
        assert response.status_code == 202
        await asyncio.wait_for(attempted.wait(), 3)
        # New subscriber and periodic orphan reconciliation must not steal queued result.
        second = asyncio.create_task(client.get(url))
        await app.state.streaming_manager.reconcile()
        assert observe(server, name, rid)[0]['status'] == 'canceling'
        save_enabled.set(); release_completion.set()
        await poll(client, rid)
        await subscriber; await second
        row, attempts, _ = assert_events(server, name, rid)
        assert (attempts[0]['input_tokens'], attempts[0]['output_tokens'], attempts[0]['total_tokens']) == (3, 2, 5)
        assert row['error_code'] is None
        assert provider.stream_close_calls == 1


async def test_f6_cancel_task_registration_failure_retains_owner_for_reconcile(server, case, monkeypatch):
    name, config = case
    provider = MockProvider()
    async with application(config, provider) as (app, client):
        rid = await create(client)
        original = asyncio.create_task
        rejected = []
        def create_task(coroutine, *args, **kwargs):
            if coroutine.cr_code.co_name == '_cancel_operation' and not rejected:
                rejected.append(coroutine)
                raise RuntimeError('controlled registration failure')
            return original(coroutine, *args, **kwargs)
        monkeypatch.setattr(asyncio, 'create_task', create_task)
        response = await client.post(cancel_url(rid))
        assert response.status_code == 500
        assert response.json()['code'] == 'internal_error'
        assert rejected and rejected[0].cr_frame is None  # failed registration closes coroutine
        assert observe(server, name, rid)[0]['status'] == 'pending'
        await app.state.streaming_manager.reconcile()
        await until(lambda: observe(server, name, rid)[0]['status'] == 'canceled')
        await poll(client, rid)
        assert_events(server, name, rid)
        assert provider.stream_calls == []


async def test_f6_programmer_error_after_acceptance_keeps_cancellation_owner(server, case, monkeypatch):
    name, config = case
    provider = ControlledProvider()
    async with application(config, provider) as (app, client):
        rid = await create(client)
        subscriber = asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
        await asyncio.wait_for(provider.entered.wait(), 3)
        original = app.state.storage.finish_cancel
        failed = asyncio.Event()
        calls = []
        async def finish(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                failed.set()
                raise RuntimeError('private one-off programming fault')
            return await original(*args, **kwargs)
        monkeypatch.setattr(app.state.storage, 'finish_cancel', finish)
        response = await client.post(cancel_url(rid))
        assert response.status_code == 202
        await asyncio.wait_for(failed.wait(), 3)
        # The failure occurred after acceptance and local stop, before final save.
        await until(lambda: app.state.streaming_manager.cancellations[rid].task.done())
        assert observe(server, name, rid)[0]['status'] == 'canceling'
        await app.state.streaming_manager.reconcile()
        await poll(client, rid)
        await subscriber
        assert_events(server, name, rid)
        assert len(provider.stream_calls) == provider.stream_close_calls == 1
        assert len(calls) == 2


async def test_f6_http_cleanup_timeout_returns_failed_without_false_canceled(server, case):
    name, config = case
    provider = ControlledProvider()
    provider.close_release.clear()
    async with application(config, provider) as (_, client):
        rid = await create(client)
        subscriber = asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
        await asyncio.wait_for(provider.entered.wait(), 3)
        response = await poll(client, rid, 'failed')
        assert response.status_code == 200
        await subscriber
        row, attempts, events = assert_events(server, name, rid, 'failed')
        assert row['error_code'] == 'cancel_cleanup_failed'
        assert attempts[0]['cleanup_error_code'] == 'stream_cleanup_timeout'
        assert len(provider.stream_calls) == provider.stream_close_calls == 1
        provider.close_release.set()


async def test_f6_diagnostics_retry_does_not_rewrite_terminal_or_known_usage(server, case, monkeypatch):
    name, config = case
    provider = MockProvider(stream_events=[ProviderStreamEvent(delta='订单 A123'),
        ProviderStreamEvent(status='completed', upstream_terminal_received=True,
                            usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5))])
    async with application(config, provider) as (app, client):
        upstream_done, release = asyncio.Event(), asyncio.Event()
        original_finish = app.state.storage.finish
        async def finish(trace, *args, **kwargs):
            if kwargs.get('error_code') is None:
                upstream_done.set(); await release.wait()
            return await original_finish(trace, *args, **kwargs)
        monkeypatch.setattr(app.state.storage, 'finish', finish)
        original_diagnostics = app.state.storage.update_diagnostics
        failed, saved = asyncio.Event(), asyncio.Event()
        writes = []
        async def diagnostics(*args, **kwargs):
            writes.append(1)
            if len(writes) == 1:
                failed.set()
                raise StorageUnavailable()
            result = await original_diagnostics(*args, **kwargs)
            saved.set()
            return result
        monkeypatch.setattr(app.state.storage, 'update_diagnostics', diagnostics)
        rid = await create(client)
        subscriber = asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
        await asyncio.wait_for(upstream_done.wait(), 3)
        await poll(client, rid)
        release.set()
        await subscriber
        await asyncio.wait_for(failed.wait(), 3)
        before = observe(server, name, rid)
        assert before[0]['status'] == 'canceled'
        await asyncio.wait_for(saved.wait(), 3)
        after = observe(server, name, rid)
        assert after[0]['status'] == 'canceled' and after[0]['error_code'] is None
        assert [(e['seq'],e['event_type'],e['payload']) for e in before[2]] == [(e['seq'],e['event_type'],e['payload']) for e in after[2]]
        assert (after[1][0]['input_tokens'], after[1][0]['output_tokens'], after[1][0]['total_tokens']) == (3, 2, 5)
        assert len(provider.stream_calls) == provider.stream_close_calls == 1
