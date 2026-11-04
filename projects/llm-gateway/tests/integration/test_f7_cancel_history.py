"""Cancel the request during backoff without rewriting ended Attempts."""
import asyncio
from f6_cases import server,case,observe,until,poll
from f7_cases import application,body
from providers.mock import MockProvider
from errors import ProviderCallError


async def test_f7_cancel_backoff_keeps_failed_attempt_history(server,case):
    name,config=case
    p=MockProvider(error=ProviderCallError(code='provider_rate_limited',retry_after_seconds=5.0))
    async with application(config,p,MockProvider()) as (app,client):
        rid=(await client.post('/v1/llm/stream',json=body())).json()['data']['request_id']
        sub=asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
        await until(lambda: bool(observe(server,name,rid)[1]) and observe(server,name,rid)[1][0]['status']=='failed')
        attempt=app.state.traces.attempts[-1]
        latency=attempt.latency_ms
        await poll(client,rid)
        await sub
        row,attempts,_=observe(server,name,rid)
        assert row['status']=='canceled' and len(p.stream_calls)==1
        assert attempts[0]['status']==attempt.status=='failed'
        assert attempts[0]['error_code']==attempt.error_code=='provider_rate_limited'
        assert attempt.latency_ms==latency
