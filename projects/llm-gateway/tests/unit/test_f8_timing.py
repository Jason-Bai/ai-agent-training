import asyncio
import json

import pytest

from contracts import StreamRequest
from persistence.store import attempt_values, timing
from providers.base import ProviderStreamEvent
from services.models import RecoveryPolicy
from test_f7_budget_and_repair import body
from test_f7_ordinary_edges import SequenceProvider, connection, ok, setup, text_request


async def test_f8_dispatch_excludes_registration_and_validation(monkeypatch):
    clock = [100.0]

    async def generate():
        clock[0] += 2
        return ok()

    class Store:
        async def prepare(self, *args, **options):
            pass

        async def begin_attempt(self, *args, **options):
            clock[0] += 10

        async def finish_attempt(self, *args, **options):
            return 'running'

    service, _, trace = setup(SequenceProvider(generate))
    service.storage = Store()
    monkeypatch.setattr('services.gateway.monotonic', lambda: clock[0])
    monkeypatch.setattr('services.gateway.validate_result', lambda result: clock.__setitem__(0, clock[0] + 3))
    _, attempt = await service._complete(text_request(), trace)
    assert attempt.latency_ms == 15000
    assert attempt_values(trace, attempt).get('provider_duration_ms') == 2000
    assert json.loads(timing(trace))['recovery_wait_ms'] == 0


@pytest.mark.parametrize('failed', [False, True])
async def test_f8_stream_dispatch_to_terminal_excludes_admission_and_final_commit(monkeypatch, failed):
    clock = [0.0]

    class Stream:
        async def stream(self, *args, **options):
            clock[0] += 2
            yield ProviderStreamEvent(delta='A')
            clock[0] += 3
            if failed:
                yield ProviderStreamEvent(status='failed', error_code='provider_connection_error')
            else:
                yield ProviderStreamEvent(status='completed', upstream_terminal_received=True)

    service, traces, trace = setup(Stream())
    execution = service.prepare_stream(StreamRequest.model_validate(body()), trace)
    monkeypatch.setattr('services.streaming.monotonic', lambda: clock[0])

    async def admit(start):
        clock[0] += 8
        await start()

    async def send(event, payload):
        if event in ('response.completed', 'response.failed'):
            clock[0] += 17

    async def start():
        pass

    execution.provider_admission = admit
    await execution.run(start, send, asyncio.Event())
    assert len(traces.attempts) == 1
    assert attempt_values(trace, traces.attempts[0]).get('provider_duration_ms') == 5000
    metrics = json.loads(timing(trace))
    assert metrics['generation_duration_ms'] == (None if failed else 3000)
    assert metrics['recovery_stop_reason'] == ('content_committed' if failed else None)


async def test_f8_retry_wait_and_skipped_candidate():
    service, traces, trace = setup(SequenceProvider(connection(), ok()),
        changes={'general-backup': {'max_output_tokens':64, 'default_output_tokens':64}},
        policy=RecoveryPolicy(base_delay_seconds=0.02, max_delay_seconds=0.02))
    await service._complete(text_request(), trace)
    assert len(traces.attempts) == 2
    metrics = json.loads(timing(trace))
    assert metrics.get('recovery_wait_ms', 0) > 0
    assert metrics['skipped_candidates'] == [{'model': 'general-backup', 'code': 'invalid_output_budget'}]


async def test_f8_cancel_during_wait_observes_elapsed_not_scheduled(monkeypatch):
    service, traces, trace = setup(SequenceProvider(connection(), ok()),
        policy=RecoveryPolicy(base_delay_seconds=5, max_delay_seconds=5))
    started = asyncio.Event()
    original = asyncio.sleep

    async def wait(seconds):
        started.set()
        await original(seconds)

    monkeypatch.setattr('services.gateway.asyncio.sleep', wait)
    task = asyncio.create_task(service._complete(text_request(), trace))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(traces.attempts) == 1
    measured = json.loads(timing(trace)).get('recovery_wait_ms')
    assert measured is not None and 0 <= measured < 5000

async def test_f8_observed_failure_stage_and_successful_fallback():
    service, _, trace = setup(SequenceProvider(connection(), connection()), SequenceProvider(ok()))
    await service._complete(text_request(), trace)
    assert json.loads(timing(trace)).get('error_stage') is None
    from errors import ProviderCallError
    service, _, trace = setup(SequenceProvider(ProviderCallError(code='provider_auth_error')))
    with pytest.raises(ProviderCallError):
        await service._complete(text_request(), trace)
    assert json.loads(timing(trace)).get('error_stage') == 'provider'

async def test_f8_terminal_with_final_delta_stops_provider_clock_before_output(monkeypatch):
    clock = [1.0]

    class Stream:
        async def stream(self, *args, **options):
            clock[0] += 2
            yield ProviderStreamEvent(delta='final', status='completed', upstream_terminal_received=True)

    service, traces, trace = setup(Stream())
    execution = service.prepare_stream(StreamRequest.model_validate(body()), trace)
    monkeypatch.setattr('services.streaming.monotonic', lambda: clock[0])

    async def start():
        pass

    async def send(event, payload):
        if event == 'response.delta':
            clock[0] += 10

    await execution.run(start, send, asyncio.Event())
    assert traces.attempts[0].provider_duration_ms == 2000

async def test_f8_stream_db_failure_correlates_request_and_attempt(caplog):
    from persistence.database import StorageUnavailable

    class Store:
        async def prepare(self, *args, **kwargs):
            pass

        async def begin_attempt(self, *args, **kwargs):
            raise StorageUnavailable()

        async def finish(self, *args, **kwargs):
            return 'failed'

        async def finish_attempt(self, *args, **kwargs):
            return 'running'

    service, traces, trace = setup(SequenceProvider())
    service.storage = Store()
    service.defer = lambda *args: None
    execution = service.prepare_stream(StreamRequest.model_validate(body()), trace)
    await execution.run_background()
    assert traces.attempts[0].provider_duration_ms is None
    assert f'request_id={trace.request_id} stage=begin_attempt attempt_no=1' in caplog.text
