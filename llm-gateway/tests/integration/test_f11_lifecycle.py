"""F11 ownership under recovery, cancellation, storage and task-registration races."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest

from f6_cases import server, case, body, observe, frames, until, poll
from f4_cases import registry
from legacy_identity import HEADERS, provision
from main import create_app
from providers.mock import MockProvider
from errors import ProviderCallError
from persistence.database import StorageUnavailable
from settings import Settings


def models():
    from services.models import ConcurrencyPolicy, RecoveryPolicy
    return replace(registry({'general-primary': {'fallback_models': ['general-backup']}}),
        concurrency=ConcurrencyPolicy(max_requests=1, max_requests_per_agent=1),
        recovery=RecoveryPolicy(base_delay_seconds=0.0, max_delay_seconds=0.0))


@asynccontextmanager
async def application(config, provider):
    provision(config)
    app = create_app(settings=Settings(env='test', _env_file=None), config_dir=config,
                     model_registry=models(), provider=provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                     base_url='http://test', headers=HEADERS) as client:
            yield app, client


def occupied(app):
    return app.state.concurrency_limiter.snapshot()['active_requests']


async def pending(client):
    response = await client.post('/v1/llm/stream', json=body())
    assert response.status_code == 202, response.text
    return response.json()['data']['request_id']


class RetryOrdinary(MockProvider):
    async def complete(self, model, messages, **options):
        if not self.calls:
            self.calls.append((model, messages))
            raise ProviderCallError(code='provider_rate_limited', retry_after_seconds=.01)
        return await super().complete(model, messages, **options)


async def test_ordinary_retry_survives_full_capacity_without_readmission(server, case, monkeypatch):
    from services.trace import RequestTrace
    name, config = case
    entered, release = asyncio.Event(), asyncio.Event()
    original = RequestTrace.wait_for_recovery

    async def wait(trace, seconds):
        entered.set()
        await release.wait()
        await original(trace, seconds)

    monkeypatch.setattr(RequestTrace, 'wait_for_recovery', wait)
    provider = RetryOrdinary()
    async with application(config, provider) as (app, client):
        first = asyncio.create_task(client.post('/v1/llm', json=body()))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            rejected = await client.post('/v1/llm', json=body())
            assert rejected.status_code == 429, rejected.text
            assert rejected.json()['code'] == 'concurrency_limited'
            assert len(provider.calls) == 1 and occupied(app) == 1
            rejected_id = rejected.headers['x-request-id']
            row, attempts, _ = observe(server, name, rejected_id)
            assert row['error_code'] == 'concurrency_limited' and attempts == []
            release.set()
            result = await first
            assert result.status_code == 200, result.text
            row, attempts, _ = observe(server, name, result.headers['x-request-id'])
            assert row['status'] == 'completed' and len(attempts) == 2
            assert len(provider.calls) == 2 and occupied(app) == 0
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)


async def test_stream_retry_holds_one_request_and_pending_rejection_is_read_only(server, case, monkeypatch):
    from f7_stream_cases import ScriptedStream, success, failure
    from services.trace import RequestTrace
    name, config = case
    entered, release = asyncio.Event(), asyncio.Event()
    original = RequestTrace.wait_for_recovery

    async def wait(trace, seconds):
        entered.set()
        await release.wait()
        await original(trace, seconds)

    monkeypatch.setattr(RequestTrace, 'wait_for_recovery', wait)
    provider = ScriptedStream([[failure('provider_rate_limited', retry_after_seconds=.01)], success()])
    async with application(config, provider) as (app, client):
        rid, other = await pending(client), await pending(client)
        before = observe(server, name, other)
        first = asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            rejected = await client.get(f'/v1/llm/stream/{other}/events')
            assert rejected.status_code == 429, rejected.text
            assert observe(server, name, other) == before
            assert occupied(app) == 1 and len(provider.stream_calls) == 1
            release.set()
            response = await first
            assert frames(response.text)[-1][1] == 'response.completed'
            await until(lambda: occupied(app) == 0)
            assert len(provider.stream_calls) == 2
            await poll(client, other)
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)


@pytest.mark.parametrize('point', ['claim-before', 'claim-after', 'create-task', 'before-first-bytecode'])
async def test_claim_or_task_failure_never_leaks_admission(server, case, monkeypatch, point):
    name, config = case
    provider = MockProvider()
    async with application(config, provider) as (app, client):
        rid = await pending(client)
        manager = app.state.streaming_manager
        original_claim = app.state.storage.claim_stream
        original_task = asyncio.create_task

        async def claim(*args, **kwargs):
            if point == 'claim-after':
                await original_claim(*args, **kwargs)
            raise StorageUnavailable()

        def create_task(coroutine, *args, **kwargs):
            if getattr(coroutine, 'cr_code', None) is not None and coroutine.cr_code.co_name == '_generate':
                if point == 'create-task':
                    raise RuntimeError('controlled task registration failure')
                task = original_task(coroutine, *args, **kwargs)
                task.cancel()
                return task
            return original_task(coroutine, *args, **kwargs)

        with monkeypatch.context() as patch:
            if point.startswith('claim-'):
                patch.setattr(app.state.storage, 'claim_stream', claim)
            else:
                patch.setattr(asyncio, 'create_task', create_task)
            try:
                await manager.subscribe(rid, 0)
            except (StorageUnavailable, RuntimeError):
                pass
        await until(lambda: occupied(app) == 0)
        assert provider.stream_calls == []
        assert rid not in manager.tasks
        # A later normal request may take the entire configured capacity.
        response = await client.post('/v1/llm', json=body())
        assert response.status_code == 200, response.text
        await manager.reconcile()
        await until(lambda: observe(server, name, rid)[0]['status'] in {'pending', 'failed'})
        assert observe(server, name, rid)[0]['status'] in {'pending', 'failed'}


@pytest.mark.parametrize('stop', ['cancel', 'timeout'])
async def test_active_cleanup_holds_capacity_until_actual_exit(server, case, monkeypatch, stop):
    import services.streaming as streaming
    from f7_stream_cases import ClosingFailureProvider
    # The iterator deliberately ignores task cancellation during its close.
    name, config = case
    provider = ClosingFailureProvider('swallow')
    monkeypatch.setattr(streaming, 'STREAM_CLEANUP_TIMEOUT_SECONDS', .03)
    async with application(config, provider) as (app, client):
        rid = await pending(client)
        manager = app.state.streaming_manager
        try:
            await manager.subscribe(rid, 0)
            await asyncio.wait_for(provider.closing.wait(), 3)
            if stop == 'cancel':
                await client.post(f'/v1/llm/stream/{rid}/cancel')
            await until(lambda: rid not in manager.tasks)
            # Cancellation can leave both the attempt-close and outer cleanup
            # waiting on this deliberately non-cooperative iterator.
            assert provider.active_closes > 0 and occupied(app) == 1
            rejected = await client.post('/v1/llm', json=body())
            assert rejected.status_code == 429, rejected.text
            provider.release.set()
            await until(lambda: provider.active_closes == 0 and occupied(app) == 0)
            assert (await client.post('/v1/llm', json=body())).status_code == 200
        finally:
            provider.release.set()
            await until(lambda: not manager.cleanup_tasks)


@pytest.mark.parametrize('method', ['prepare', 'begin_attempt', 'finish'])
async def test_storage_failure_releases_stopped_request(server, case, monkeypatch, method):
    name, config = case
    provider = MockProvider()
    async with application(config, provider) as (app, client):
        async def fail(*args, **kwargs):
            raise StorageUnavailable()
        with monkeypatch.context() as patch:
            patch.setattr(app.state.storage, method, fail)
            response = await client.post('/v1/llm', json=body())
            assert response.status_code == 503, response.text
        assert occupied(app) == 0
        assert len(provider.calls) == int(method == 'finish')
        assert (await client.post('/v1/llm', json=body())).status_code == 200
