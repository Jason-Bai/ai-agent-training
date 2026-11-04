"""Observe the real reconciliation through existing app callbacks, not internals."""
import asyncio

import pytest

from f13o1_support import eventually, trace, wiring
from persistence.database import StorageUnavailable


async def test_shutdown_flushes_stopped_request_once(wiring):
    item = trace()
    wiring.app.state.defer_persistence(item)
    manager = wiring.app.state.streaming_manager
    assert manager.awaiting_persistence(item.request_id)
    async with wiring.app.router.lifespan_context(wiring.app):
        pass
    storage = wiring.app.state.storage
    assert [call[0] for call in storage.calls] == ['finish', 'diagnostics']
    assert item.status == 'failed' and item._persisted_terminal
    assert not manager.awaiting_persistence(item.request_id)


@pytest.mark.parametrize('kind', ['generation', 'request', 'cancel'])
async def test_active_and_canceling_request_cannot_be_failed_by_compensation(wiring, kind):
    app = wiring.app
    item = trace(_cancel_requested=kind == 'cancel')
    manager = app.state.streaming_manager
    if kind == 'generation':
        manager.active.add(item.request_id)
    elif kind == 'request':
        middleware = next(m for m in app.user_middleware if m.cls.__name__ == 'RequestContextMiddleware')
        middleware.kwargs['active'].add(item.request_id)
    app.state.defer_persistence(item)
    async with app.router.lifespan_context(app):
        await eventually(lambda: manager.ticks >= 1)
    assert app.state.storage.calls == []
    assert item.status == 'running'
    assert manager.awaiting_persistence(item.request_id)


async def test_unacknowledged_post_preserves_already_running_stream(wiring):
    app = wiring.app
    item = trace(operation='streaming', _stream_creation=True)
    app.state.storage.row = {'status': 'running', 'error_code': None}
    app.state.defer_persistence(item)
    async with app.router.lifespan_context(app):
        pass
    assert app.state.storage.calls == [('get_stream', item.request_id)]
    assert item.status == 'running'


async def test_old_post_does_not_replace_stopped_generation(wiring):
    app = wiring.app
    generation = trace(operation='streaming', _admitted=True)
    app.state.defer_persistence(generation)
    app.state.defer_persistence(trace(operation='streaming', _stream_creation=True))
    async with app.router.lifespan_context(app):
        pass
    assert generation.status == 'failed'
    assert app.state.storage.calls[0][2]['event'][0] == 'response.failed'


async def test_new_pending_entry_is_not_removed_by_previous_flush(wiring):
    app = wiring.app
    old, new = trace(), trace(_cancel_requested=True)
    async def replace():
        app.state.defer_persistence(new)
    app.state.storage.on_finish = replace
    app.state.defer_persistence(old)
    async with app.router.lifespan_context(app):
        await eventually(lambda: old.status == 'failed')
    assert app.state.streaming_manager.awaiting_persistence(new.request_id)
    assert new.status == 'running'


async def test_persisted_terminal_only_updates_diagnostics_and_app_queues_are_independent(wiring):
    app, other = wiring.app, wiring.make_app()
    item = trace(status='completed', _persisted_terminal=True)
    app.state.defer_persistence(item)
    assert not other.state.streaming_manager.awaiting_persistence(item.request_id)
    async with app.router.lifespan_context(app):
        pass
    assert app.state.storage.calls == [('diagnostics', item.request_id)]
    assert item.status == 'completed'


async def test_storage_failure_keeps_entry_until_later_recovery(wiring):
    app = wiring.app
    item = trace()
    app.state.storage.failure = StorageUnavailable()
    app.state.defer_persistence(item)
    async with app.router.lifespan_context(app):
        await eventually(lambda: bool(app.state.storage.calls))
        assert app.state.streaming_manager.awaiting_persistence(item.request_id)
        app.state.storage.failure = None
    assert item.status == 'failed'
    assert not app.state.streaming_manager.awaiting_persistence(item.request_id)


async def test_unexpected_compensation_failure_is_safe_and_retryable(wiring, caplog):
    app = wiring.app
    item = trace()
    app.state.storage.failure = RuntimeError('private upstream body')
    app.state.defer_persistence(item)
    async with app.router.lifespan_context(app):
        await eventually(lambda: bool(app.state.storage.calls))
        assert app.state.streaming_manager.awaiting_persistence(item.request_id)
        app.state.storage.failure = None
    assert item.status == 'failed'
    assert item.request_id in caplog.text
    assert 'private upstream body' not in caplog.text


async def test_cancellation_is_not_swallowed_when_storage_wraps_it(wiring):
    app = wiring.app
    item = trace()
    entered = asyncio.Event()

    async def interrupted():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise StorageUnavailable() from None

    app.state.storage.on_finish = interrupted
    app.state.defer_persistence(item)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(entered.wait(), 1)
        # Let shutdown retry successfully after canceling the in-flight flush.
        app.state.storage.on_finish = None
    assert item.status == 'failed'
    assert not app.state.streaming_manager.awaiting_persistence(item.request_id)
    assert len([call for call in app.state.storage.calls if call[0] == 'finish']) == 2
