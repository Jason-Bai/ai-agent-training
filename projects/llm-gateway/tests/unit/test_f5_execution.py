"""F5 E/T: deterministic upstream and real awaited send boundaries."""
import asyncio
from dataclasses import replace

import pytest

from providers.mock import MockProvider
from services.gateway import GatewayService
from f3_cases import make_app


def test_stream_preflight_entry_exists():
    assert hasattr(GatewayService, 'prepare_stream'), 'F5 needs preflight before SSE starts'


def event(status='running', delta='', **kwargs):
    from providers.base import ProviderStreamEvent
    return ProviderStreamEvent(status=status, delta=delta,
                               upstream_terminal_received=status == 'completed', **kwargs)


class Scripted(MockProvider):
    def __init__(self, script, *, close_error=False, close_wait=False):
        super().__init__()
        self.script = script
        self.reads = self.opened = self.closed = 0
        self.close_error, self.close_wait = close_error, close_wait

    async def stream(self, *args, **kwargs):
        self.opened += 1
        try:
            for item in self.script:
                self.reads += 1
                if isinstance(item, asyncio.Event):
                    await item.wait()
                elif isinstance(item, Exception):
                    raise item
                elif callable(item):
                    yield item()
                else:
                    yield item
        finally:
            self.closed += 1
            if self.close_wait:
                await asyncio.Event().wait()
            if self.close_error:
                raise RuntimeError('PRIVATE_CLOSE_ERROR')


def execution(provider, timeout=30):
    from contracts import StreamRequest
    app = make_app(primary=provider)
    state = app.state
    trace = state.traces.start_request('unit-request', operation='streaming')
    gateway = GatewayService(state.providers, state.traces, state.prompt_registry,
                             state.model_registry, state.token_budget, limiter=state.concurrency_limiter)
    request = StreamRequest.model_validate({
        'model': 'general-primary', 'prompt': {'name': 'basic_text', 'version': '1.0.0'},
        'messages': [{'role': 'user', 'content': 'A123'}],
    })
    result = gateway.prepare_stream(request, trace)
    # A private prepared execution is shortened, never a public request field.
    result.prepared = replace(result.prepared, timeout_seconds=timeout)
    return result, trace, state.traces


async def run(script, *, timeout=30, send=None, start=None, closed=None, provider=None):
    provider = provider or Scripted(script)
    executor, trace, store = execution(provider, timeout)
    output = []

    async def begin():
        if start:
            await start()

    async def emit(name, data):
        if send:
            await send(name, data)
        output.append((name, data))

    await executor.run(begin, emit, closed or asyncio.Event())
    return output, trace, store, provider


@pytest.mark.parametrize('parts', [['订单', ' ', 'A123', '\n'], ['回答'], ['', '回答']])
async def test_exact_deltas_and_completed(parts):
    output, trace, store, provider = await run([*(event(delta=s) for s in parts), event('completed')])
    assert [d['delta'] for n, d in output if n == 'response.delta'] == [s for s in parts if s]
    assert output[-1] == ('response.completed', {'request_id': 'unit-request', 'usage': None})
    assert trace.status == store.attempts[0].status == 'completed'
    assert trace.ttft_ms is not None and trace.generation_duration_ms is not None
    assert provider.closed == 1 and provider.close_calls == 0


@pytest.mark.parametrize('parts', [[], [''], [' '], ['\n', '\t']])
async def test_blank_normal_end_is_failed(parts):
    output, trace, store, provider = await run([*(event(delta=s) for s in parts), event('completed')])
    assert output[-1][0] == 'response.failed'
    assert output[-1][1]['code'] == trace.error_code == 'provider_invalid_response'
    assert store.attempts[0].status == 'failed'
    assert (trace.ttft_ms is None) == (not any(parts))


@pytest.mark.parametrize('script,code', [([], 'provider_invalid_response'),
    ([lambda: event(delta='部分')], 'provider_invalid_response'),
    ([RuntimeError('PRIVATE_INTERNAL_ERROR')], 'internal_error'),
    ([OSError('PRIVATE_PROVIDER_PROGRAM_ERROR')], 'internal_error')])
async def test_eof_and_unexpected_error(script, code):
    output, trace, store, provider = await run(script)
    assert output[-1][1]['code'] == trace.error_code == code
    assert trace.status == store.attempts[0].status == 'failed'
    assert trace.generation_duration_ms is None
    assert provider.closed == 1
    assert 'PRIVATE_' not in repr(output) + repr(trace) + repr(store.attempts)


async def test_last_delta_with_failed_terminal_and_no_further_reads():
    terminal = event('failed', '最后一段', error_code='output_truncated')
    output, trace, store, provider = await run([terminal, event(delta='禁止发送')])
    assert [n for n, _ in output] == ['response.delta', 'response.failed']
    assert output[0][1]['delta'] == '最后一段'
    assert trace.error_code == store.attempts[0].provider_error_code == 'output_truncated'
    assert provider.reads == 1 and provider.closed == 1
    assert trace.generation_duration_ms is None  # synthetic failed is not remote completion


@pytest.mark.parametrize('blocked', ['start', 'response.delta', 'response.heartbeat',
                                    'response.completed', 'response.failed'])
async def test_total_deadline_covers_actual_send(blocked, monkeypatch):
    import services.streaming as streaming
    monkeypatch.setattr(streaming, 'STREAM_HEARTBEAT_SECONDS', 0.005)
    pending = asyncio.Event()
    script = [pending] if blocked == 'response.heartbeat' else [event(delta='正文'), event('completed')]
    if blocked == 'response.failed':
        script = [event('failed', error_code='provider_auth_error')]

    async def start():
        if blocked == 'start':
            await pending.wait()

    async def send(name, data):
        if name == blocked:
            await pending.wait()

    output, trace, store, provider = await run(script, timeout=0.025, start=start, send=send)
    assert trace.status == 'failed' and trace.error_code == 'request_timeout'
    assert len(store.attempts) == (0 if blocked == 'start' else 1)
    if store.attempts:
        assert store.attempts[0].status == 'failed'
    if blocked == 'response.failed':
        assert store.attempts[0].provider_error_code == 'provider_auth_error'
    assert sum(n in {'response.failed', 'response.completed'} for n, _ in output) <= 1


async def test_heartbeats_keep_same_pending_read_and_do_not_reset_deadline(monkeypatch):
    import services.streaming as streaming
    monkeypatch.setattr(streaming, 'STREAM_HEARTBEAT_SECONDS', 0.005)
    provider = Scripted([asyncio.Event()])
    output, trace, _, _ = await run([], timeout=0.026, provider=provider)
    assert sum(n == 'response.heartbeat' for n, _ in output) >= 2
    assert provider.reads == 1 and provider.closed == 1
    assert trace.ttft_ms is None and trace.error_code == 'request_timeout'


async def test_send_backpressure_does_not_prefetch():
    entered, release = asyncio.Event(), asyncio.Event()
    provider = Scripted([event(delta='一'), event(delta='二'), event('completed')])

    async def send(name, data):
        if name == 'response.delta' and data['delta'] == '一':
            entered.set()
            await release.wait()

    task = asyncio.create_task(run([], send=send, provider=provider))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert provider.reads == 1
        release.set()
        output, trace, _, _ = await asyncio.wait_for(task, 1)
        assert trace.status == 'completed' and provider.reads == 3
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('before_text', [True, False])
async def test_disconnect_cleans_only_own_stream(before_text):
    closed = asyncio.Event()
    reached = asyncio.Event()
    provider = Scripted([asyncio.Event()] if before_text else [event(delta='正文'), asyncio.Event()])

    async def begin():
        if before_text:
            asyncio.get_running_loop().call_later(0.005, closed.set)

    async def send(name, data):
        if name == 'response.delta':
            reached.set()
            closed.set()

    output, trace, store, _ = await run([], provider=provider, start=begin, send=send, closed=closed)
    assert trace.status == store.attempts[0].status == 'failed'
    assert trace.error_code == 'client_disconnected'
    assert provider.closed == 1 and provider.close_calls == 0
    assert not any(n in {'response.completed', 'response.failed'} for n, _ in output)


async def test_generation_time_uses_upstream_times_not_sent_times(monkeypatch):
    import services.streaming as streaming
    now = [0.0]
    monkeypatch.setattr(streaming, 'monotonic', lambda: now[0])

    def first():
        now[0] = 2
        return event(delta='正文')

    def last():
        now[0] = 7
        return event('completed')

    async def send(name, data):
        now[0] = 3 if name == 'response.delta' else 8

    output, trace, _, _ = await run([first, last], send=send)
    assert trace.ttft_ms == 3000 and trace.generation_duration_ms == 5000
    assert trace.stream_duration_ms == 8000


async def test_cleanup_error_does_not_replace_success_or_original_failure():
    for terminal, expected in [(event('completed'), None),
                               (event('failed', error_code='provider_auth_error'), 'provider_auth_error')]:
        provider = Scripted([event(delta='正文'), terminal], close_error=True)
        _, trace, _, _ = await run([], provider=provider)
        assert trace.error_code == expected
        assert trace.cleanup_error_code == 'stream_cleanup_error'
        assert provider.closed == 1


@pytest.mark.parametrize('bad', [True, '1', -1, 1.5])
async def test_constructed_nested_usage_is_revalidated(bad):
    from contracts import Usage
    from providers.base import ProviderStreamEvent
    usage = Usage.model_construct(input_tokens=bad, output_tokens=0, total_tokens=bad)
    terminal = ProviderStreamEvent.model_construct(status='completed', usage=usage,
                                                   upstream_terminal_received=True)
    output, trace, _, _ = await run([event(delta='正文'), terminal])
    assert trace.error_code == 'provider_invalid_response'
    assert output[-1][1]['code'] == 'provider_invalid_response'
    assert trace.upstream_terminal_at is None


async def test_task_cancelled_during_cleanup_still_finalizes_completed_trace(monkeypatch):
    import services.streaming as streaming
    monkeypatch.setattr(streaming, 'STREAM_CLEANUP_TIMEOUT_SECONDS', 0.03)
    provider = Scripted([event(delta='正文'), event('completed')], close_wait=True)
    executor, trace, store = execution(provider)

    async def start():
        pass

    async def send(name, data):
        pass

    task = asyncio.create_task(executor.run(start, send, asyncio.Event()))
    async with asyncio.timeout(1):
        while not provider.closed:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert trace.status == store.attempts[0].status == 'completed'
    assert trace.error_code is None and trace.stream_ended_at is not None
    assert trace.cleanup_error_code == 'stream_cleanup_timeout'
    assert provider.closed == 1


async def test_cancel_during_failure_notification_cannot_leave_running_trace():
    entered = asyncio.Event()
    provider = Scripted([event('failed', error_code='provider_auth_error')])
    executor, trace, store = execution(provider)

    async def start():
        pass

    async def send(name, data):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(executor.run(start, send, asyncio.Event()))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert trace.status == store.attempts[0].status == 'failed'
    assert trace.error_code == 'internal_error'
    assert store.attempts[0].provider_error_code == 'provider_auth_error'
    assert trace.stream_ended_at is not None and provider.closed == 1


@pytest.mark.parametrize('bad', [
    {'status': 'other'}, {'delta': 1}, {'status': 'running', 'error_code': 'provider_error'},
    {'status': 'completed', 'upstream_terminal_received': False},
    {'status': 'completed', 'upstream_terminal_received': True, 'error_code': 'provider_error'},
    {'status': 'failed'}, {'status': 'failed', 'error_code': 'unknown'},
    {'upstream_terminal_received': 'true'},
])
async def test_illegal_internal_event_is_rejected_before_its_delta(bad):
    from providers.base import ProviderStreamEvent
    fields = {'delta': 'PRIVATE_MUST_NOT_BE_EMITTED', **bad}
    raw = ProviderStreamEvent.model_construct(**fields)
    output, trace, _, provider = await run([raw])
    assert [name for name, _ in output] == ['response.failed']
    assert trace.error_code == 'provider_invalid_response' and trace.ttft_ms is None
    assert trace.upstream_first_text_at is None and provider.closed == 1


@pytest.mark.parametrize('exception,code', [(OSError('PRIVATE_WIRE_ERROR'), 'client_disconnected'),
                                          (RuntimeError('PRIVATE_PROGRAM_ERROR'), 'internal_error')])
async def test_terminal_send_exception_never_starts_second_terminal(exception, code):
    attempts = []

    async def send(name, data):
        if name == 'response.completed':
            attempts.append(name)
            raise exception
        assert name != 'response.failed'

    _, trace, store, provider = await run([event(delta='正文'), event('completed')], send=send)
    assert attempts == ['response.completed'] and trace.error_code == code
    assert store.attempts[0].status == 'failed' and provider.closed == 1
    assert trace.upstream_terminal_at is not None


async def test_remote_failure_keeps_generation_endpoint_and_exact_usage():
    from providers.base import ProviderStreamEvent
    from contracts import Usage
    terminal = ProviderStreamEvent(status='failed', error_code='output_truncated',
        usage=Usage(input_tokens=0, output_tokens=0, total_tokens=0), upstream_terminal_received=True)
    output, trace, store, _ = await run([event(delta='正文'), terminal])
    assert trace.error_code == 'output_truncated'
    assert trace.generation_duration_ms is not None
    assert store.attempts[0].usage.model_dump() == {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}


@pytest.mark.parametrize('terminal', ['completed', 'failed'])
async def test_hanging_close_has_one_shared_grace_and_no_orphan_tasks(terminal, monkeypatch):
    import services.streaming as streaming
    monkeypatch.setattr(streaming, 'STREAM_CLEANUP_TIMEOUT_SECONDS', 0.01)
    fields = {'error_code': 'provider_auth_error'} if terminal == 'failed' else {}
    provider = Scripted([event(delta='正文'), event(terminal, **fields)], close_wait=True)
    before = asyncio.all_tasks()
    _, trace, _, _ = await run([], provider=provider)
    assert trace.cleanup_error_code == 'stream_cleanup_timeout'
    assert trace.error_code == ('provider_auth_error' if terminal == 'failed' else None)
    assert provider.closed == 1 and provider.close_calls == 0
    assert not (asyncio.all_tasks() - before)


def test_default_heartbeat_boundary_uses_last_successful_send(monkeypatch):
    import constants
    import services.streaming as streaming
    assert constants.STREAM_HEARTBEAT_SECONDS == 15
    assert constants.STREAM_CLEANUP_TIMEOUT_SECONDS == 1
    executor, _, _ = execution(Scripted([]))
    executor.last_sent_at = 10
    now = [24.999]
    monkeypatch.setattr(streaming, 'monotonic', lambda: now[0])
    assert executor._heartbeat_delay() == pytest.approx(0.001)
    now[0] = 25
    assert executor._heartbeat_delay() == 0
    now[0] = 25.001
    assert executor._heartbeat_delay() == 0


async def test_disconnect_after_completed_send_keeps_success():
    closed = asyncio.Event()

    async def send(name, data):
        if name == 'response.completed':
            closed.set()

    output, trace, _, provider = await run([event(delta='正文'), event('completed')], send=send, closed=closed)
    assert trace.status == 'completed' and trace.error_code is None
    assert output[-1][0] == 'response.completed' and provider.closed == 1


async def test_close_error_from_cancelled_pending_read_is_recorded():
    provider = Scripted([event(delta='正文'), asyncio.Event()], close_error=True)
    _, trace, _, _ = await run([], provider=provider, timeout=0.01)
    assert trace.error_code == 'request_timeout'
    assert trace.cleanup_error_code == 'stream_cleanup_error'
    assert provider.closed == 1


async def test_continuously_ready_empty_events_cannot_starve_idle_heartbeat(monkeypatch):
    import services.streaming as streaming
    monkeypatch.setattr(streaming, 'STREAM_HEARTBEAT_SECONDS', 0.001)
    output, trace, _, _ = await run([*[event() for _ in range(2000)], event(delta='正文'), event('completed')])
    assert any(name == 'response.heartbeat' for name, _ in output)
    assert trace.status == 'completed'


async def test_due_deadline_wins_over_failure_ready_in_same_event_loop_turn():
    import time

    def delayed_failure():
        # Controlled synchronous work prevents the loop's timeout callback from
        # running until after both the event and the deadline are observable.
        time.sleep(0.02)
        return event('failed', error_code='provider_timeout')

    output, trace, _, _ = await run([delayed_failure], timeout=0.01)
    assert trace.error_code == 'request_timeout'


async def test_no_provider_attempt_after_headers_return_beyond_deadline():
    import time
    async def start():
        time.sleep(0.02)
    output,trace,store,provider=await run([event(delta='正文'),event('completed')],timeout=0.01,start=start)
    assert trace.error_code=='request_timeout'
    assert trace.attempts==len(store.attempts)==provider.opened==0
