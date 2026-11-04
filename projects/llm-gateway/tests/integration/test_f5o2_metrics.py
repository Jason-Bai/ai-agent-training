"""The first successful send across active subscribers owns original TTFT."""
import asyncio
import json
from time import monotonic

import httpx

from test_f5o2_http import server, case, make_app, body, observe
from test_f5o2_delivery import invoke
from providers.base import ProviderStreamEvent
from providers.mock import MockProvider


async def test_two_active_subscribers_record_only_earliest_actual_text_send(server,case):
    name,config=case
    provider_release=asyncio.Event();slow_entered=asyncio.Event();slow_release=asyncio.Event()
    fast_sent=asyncio.Event();timestamps={}
    async def gate():
        await provider_release.wait()
        timestamps['remote_terminal']=monotonic()
        return ProviderStreamEvent(status='completed',upstream_terminal_received=True)
    provider=MockProvider(stream_events=[ProviderStreamEvent(delta='订单 A123'),gate])
    app=make_app(config,provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            rid=(await client.post('/v1/llm/stream',json=body())).json()['data']['request_id']
            url=f'/v1/llm/stream/{rid}/events'
            async def slow_send(message):
                if b'event: response.delta' in message.get('body',b''):
                    slow_entered.set()
                    await slow_release.wait()
                    timestamps['slow_send']=monotonic()
            async def fast_send(message):
                if b'event: response.delta' in message.get('body',b''):
                    timestamps['fast_send']=monotonic()
                    fast_sent.set()
            slow=asyncio.create_task(invoke(app,'GET',url,slow_send))
            fast=None
            try:
                await asyncio.wait_for(slow_entered.wait(),2)
                trace=app.state.traces.requests[rid]
                assert trace.upstream_first_text_at is not None and trace.first_text_sent_at is None
                assert trace.upstream_terminal_at is None
                fast=asyncio.create_task(invoke(app,'GET',url,fast_send))
                await asyncio.wait_for(fast_sent.wait(),2)
                await asyncio.sleep(0)
                first=trace.first_text_sent_at
                assert first is not None and first >= timestamps['fast_send']
                assert trace.stream_started_at <= trace.upstream_first_text_at <= first
                slow_release.set()
                while 'slow_send' not in timestamps:
                    await asyncio.sleep(0)
                assert timestamps['slow_send'] > first
                await asyncio.sleep(0)
                assert trace.first_text_sent_at==first
                provider_release.set()
                await asyncio.wait_for(asyncio.gather(slow,fast),2)
                assert trace.first_text_sent_at==first
                assert trace.upstream_terminal_at >= timestamps['remote_terminal'] > first
                assert trace.ttft_ms==(first-trace.stream_started_at)*1000
                assert trace.generation_duration_ms==(trace.upstream_terminal_at-trace.upstream_first_text_at)*1000
                assert len(provider.stream_calls)==1
                await asyncio.sleep(1.1)
                row,attempts,events=observe(server,name,rid)
                timing=json.loads(row['timing_snapshot'])
                assert timing['ttft_ms']==trace.ttft_ms
                assert timing['generation_duration_ms']==trace.generation_duration_ms
                assert row['status']=='completed' and len(attempts)==1
            finally:
                slow_release.set();provider_release.set()
                for task in [slow,fast]:
                    if task is not None and not task.done():task.cancel()
                await asyncio.gather(*(t for t in [slow,fast] if t is not None),return_exceptions=True)
