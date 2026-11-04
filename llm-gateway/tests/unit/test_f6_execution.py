"""Cancellation signals and bounded execution cleanup without HTTP buffering."""
import asyncio
from uuid import uuid4

import pytest

from contracts import Message
from f4_cases import registry
from providers.mock import MockProvider
from services.streaming import PreparedStream, StreamingExecution
from services.trace import TraceStore


class RecordingStorage:
    def __init__(self):
        self.finished = []

    async def begin_attempt(self, *args, **kwargs):
        pass

    async def append_event(self, *args, **kwargs):
        pass

    async def finish(self, *args, **kwargs):
        self.finished.append(kwargs)
        return 'canceling'


def execution(provider=None):
    models = registry()
    model = models.endpoints['general-primary']
    traces = TraceStore()
    trace = traces.start_request(str(uuid4()), operation='streaming')
    value = StreamingExecution(PreparedStream('general-primary', model, models.providers[model.provider],
        [Message(role='user', content='订单 A123')], 16, 30), provider or MockProvider(delay=30), trace, traces,
        storage=RecordingStorage(), defer=lambda *args: None)
    value.prepared_storage = True
    value.deadline = asyncio.get_running_loop().time() + 30
    return value


async def test_cancel_signal_does_not_finalize_as_internal_failure():
    value = execution()
    task = asyncio.create_task(value.run_background())
    try:
        async with asyncio.timeout(2):
            while not value.provider.stream_calls:
                await asyncio.sleep(0)
        value.cancel_requested = True
        value.trace._cancel_requested = True
        value.trace.status = 'canceling'
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert value.trace.status == 'canceling'
        assert value.trace.error_code is None
        assert not getattr(value.trace, '_persisted_terminal', False)
        assert value.storage.finished == []
        assert value.provider.stream_close_calls == 1
        assert value.provider.close_calls == 0
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cancel_before_run_never_starts_attempt_or_provider():
    value = execution()
    value.cancel_requested = True
    value.trace._cancel_requested = True
    value.trace.status = 'canceling'
    task = asyncio.create_task(value.run_background())
    try:
        done, _ = await asyncio.wait({task}, timeout=.1)
        assert done, 'An accepted cancellation must stop before starting execution'
        await task
        assert value.attempt is None
        assert value.provider.stream_calls == []
        assert value.trace.status == 'canceling'
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cleanup_retains_task_that_does_not_acknowledge_cancellation(monkeypatch):
    import services.streaming as streaming
    monkeypatch.setattr(streaming, 'STREAM_CLEANUP_TIMEOUT_SECONDS', .03)
    value = execution()
    started, release = asyncio.Event(), asyncio.Event()

    async def resistant():
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    worker = asyncio.create_task(resistant())
    await started.wait()
    cleanup = asyncio.create_task(value._cleanup(worker, None))
    try:
        done, _ = await asyncio.wait({cleanup}, timeout=.3)
        assert done, 'Cleanup must not wait indefinitely for a cancellation-resistant task'
        await cleanup
        assert value.trace.cleanup_error_code == 'stream_cleanup_timeout'
        assert not worker.done()
        assert worker in value.cleanup_tasks
    finally:
        release.set()
        await asyncio.wait_for(worker, 1)
        await asyncio.wait_for(cleanup, 1)


async def test_second_interruption_does_not_extend_cancel_cleanup_budget(monkeypatch):
    import services.streaming as streaming
    monkeypatch.setattr(streaming, 'STREAM_CLEANUP_TIMEOUT_SECONDS', .03)
    entered, closing = asyncio.Event(), asyncio.Event()

    class Provider(MockProvider):
        async def stream(self, *args, **kwargs):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield
            finally:
                self.stream_close_calls += 1
                closing.set()
                await asyncio.Event().wait()

    provider = Provider()
    value = execution(provider)
    task = asyncio.create_task(value.run_background())
    await entered.wait()
    value.cancel_requested = True
    value.trace._cancel_requested = True
    value.trace.status = 'canceling'
    task.cancel()
    await closing.wait()
    began = asyncio.get_running_loop().time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, .3)
    assert asyncio.get_running_loop().time() - began < .2
    assert provider.stream_close_calls == 1
    assert value.trace.cleanup_error_code == 'stream_cleanup_timeout'
    assert value.trace.status == 'canceling'
    assert value.storage.finished == []


async def test_canceling_subscription_heartbeats_do_not_change_event_cursor(monkeypatch):
    import services.subscriptions as module
    from utils.json_codec import dumps
    clock = [0.0]
    monkeypatch.setattr(module, 'monotonic', lambda: clock[0])
    sent, queries = [], []
    started = asyncio.Event()
    terminal = False

    class Store:
        async def read_events(self, rid, after_seq, limit):
            queries.append(after_seq)
            await asyncio.sleep(0)
            events = [{'seq': 1, 'event_type': 'response.canceling', 'payload': dumps({'request_id': rid})}]
            if terminal:
                events.append({'seq': 2, 'event_type': 'response.canceled', 'payload': dumps({'request_id': rid})})
            return {'status': 'canceled' if terminal else 'canceling', 'last_event_seq': len(events)}, [e for e in events if e['seq'] > after_seq]

    async def start():
        started.set()

    async def emit(name, payload, seq=None):
        nonlocal terminal
        sent.append((name, seq))
        if name == 'response.heartbeat':
            terminal = True

    subject = module.EventSubscription(Store(), 'A123', 0, poll_seconds=.001)
    task = asyncio.create_task(subject.run(start, emit, asyncio.Event()))
    try:
        await started.wait()
        async with asyncio.timeout(.5):
            while not sent:
                await asyncio.sleep(.001)
        clock[0] = 14.99
        await asyncio.sleep(.005)
        assert sent == [('response.canceling', 1)]
        clock[0] = 15
        await asyncio.wait_for(task, .5)
        assert sent == [('response.canceling', 1), ('response.heartbeat', None), ('response.canceled', 2)]
        assert subject.after_seq == 2 and set(queries) <= {0, 1}
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_owned_postgres_port_is_stable_and_outside_client_ephemeral_range(tmp_path, monkeypatch):
    import postgres_support
    import yaml
    monkeypatch.setattr(postgres_support.tempfile, 'mkdtemp', lambda **kwargs: str(tmp_path))
    server = postgres_support.PostgresServer()
    assert isinstance(server.port, int) and 20000 <= server.port < 30000
    document = yaml.safe_load(server.compose_path.read_text())
    assert document['services']['postgres']['ports'] == [f'127.0.0.1:{server.port}:5432']


async def test_late_cleanup_cannot_overwrite_acknowledged_cancellation_terminal():
    value = execution()
    entered, release = asyncio.Event(), asyncio.Event()
    original = value._cleanup

    async def held(worker, disconnect):
        await original(worker, disconnect)
        entered.set()
        await release.wait()

    value._cleanup = held
    task = asyncio.create_task(value.run_background())
    try:
        async with asyncio.timeout(1):
            while not value.provider.stream_calls:
                await asyncio.sleep(0)
        value.cancel_requested = True
        value.trace._cancel_requested = True
        value.trace.status = 'canceling'
        task.cancel()
        await asyncio.wait_for(entered.wait(), 1)
        # The coordinator's bounded stop already recorded failed. This models
        # its acknowledged terminal, not a fake successful cleanup.
        value.trace._persisted_terminal = True
        value.trace.status, value.trace.error_code = 'failed', 'cancel_cleanup_failed'
        value.attempt.status, value.attempt.error_code = 'failed', 'cancel_cleanup_failed'
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert value.trace.status == value.attempt.status == 'failed'
        assert value.trace.error_code == value.attempt.error_code == 'cancel_cleanup_failed'
        assert value.storage.finished == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
