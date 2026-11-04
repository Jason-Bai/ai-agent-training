import asyncio
import json

from errors import ProviderCallError
from f6_cases import case, observe, poll, server
from f7_cases import application, body, registry, SequenceProvider
from providers.base import ProviderResult
from providers.mock import MockProvider
from services.models import RecoveryPolicy
from services.trace import RequestTrace


async def test_f8_active_retry_sees_first_attempt_time_and_elapsed_backoff(server, case):
    name, config = case
    observations = []
    primary = SequenceProvider([ProviderCallError(code='provider_connection_error'), ProviderResult(text='ok')])
    models = registry({'general-backup': {'max_output_tokens': 32, 'default_output_tokens': 32}},
                      policy=RecoveryPolicy(base_delay_seconds=.02, max_delay_seconds=.02))

    def at_second_call():
        if len(primary.calls) == 2:
            with server.connect(name) as conn:
                observations.append(conn.execute(
                    'select r.status, r.timing_snapshot, a.status, a.provider_duration_ms '
                    'from tb_llm_request r join tb_llm_attempt a on r.id=a.request_pk '
                    'where a.attempt_no=1').fetchone())

    primary.before = at_second_call
    async with application(config, primary, models=models) as (_, client):
        response = await client.post('/v1/llm', json=body(max_output_tokens=128))
        assert response.status_code == 200
        request, attempts, _ = observe(server, name, response.headers['X-Request-ID'])
        assert len(attempts) == len(primary.calls) == 2
        assert len(observations) == 1
        status, snapshot, attempt_status, duration = observations[0]
        assert (status, attempt_status) == ('running', 'failed')
        assert duration == attempts[0]['provider_duration_ms'] and duration > 0
        active, final = json.loads(snapshot), json.loads(request['timing_snapshot'])
        assert active['skipped_candidates'] == [{'model': 'general-backup', 'code': 'invalid_output_budget'}]
        assert 0 < active['recovery_wait_ms'] == final['recovery_wait_ms']
        assert final['error_stage'] is None


async def test_f8_interrupted_stream_backoff_writes_partial_wait(server, case, monkeypatch):
    name, config = case
    waiting = asyncio.Event()
    original = RequestTrace.wait_for_recovery

    async def observed(self, seconds):
        waiting.set()
        await original(self, seconds)

    monkeypatch.setattr(RequestTrace, 'wait_for_recovery', observed)
    provider = MockProvider(error=ProviderCallError(code='provider_rate_limited', retry_after_seconds=5))
    async with application(config, provider) as (_, client):
        rid = (await client.post('/v1/llm/stream', json=body())).json()['data']['request_id']
        subscriber = asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
        try:
            await asyncio.wait_for(waiting.wait(), 3)
            await poll(client, rid)
            await subscriber
        finally:
            subscriber.cancel()
            await asyncio.gather(subscriber, return_exceptions=True)
        request, attempts, events = observe(server, name, rid)
        assert request['status'] == 'canceled'
        assert len(provider.stream_calls) == len(attempts) == 1
        assert attempts[0]['status'] == 'failed' and attempts[0]['provider_duration_ms'] > 0
        assert [e['event_type'] for e in events] == ['response.canceling', 'response.canceled']
        assert 0 < json.loads(request['timing_snapshot'])['recovery_wait_ms'] < 5000
