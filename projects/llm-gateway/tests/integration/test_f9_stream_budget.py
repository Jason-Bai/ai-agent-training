from pricing_support import write_pricing
from registry_helpers import ModelRegistry
"""One funded account, many subscribers, and independently observed provider entries."""
from services.models import ConcurrencyPolicy
import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import text

from f6_cases import ControlledProvider, case, frames, server
from f7_cases import application, body, registry
from services.models import ProviderConfig
from test_f9_billing_queries import enroll


def paid_models(config):
    models = registry({'general-backup': {'structured_output_mode': 'none'}})
    providers = {key: ProviderConfig(adapter='deepseek',api_protocol='chat_completions',
                 base_url='http://127.0.0.1', credential_ref='test-only') for key in models.providers}
    write_pricing(config, """
bindings: {mock-main: test, mock-backup: test}
versions:
- supplier: test
  model: deepseek-flash
  version: test
  effective_from: '2000-01-01T00:00:00Z'
  peak: {hit: '0', miss: '0', output: '1000'}
  off_peak: {hit: '0', miss: '0', output: '1000'}
""")
    return ModelRegistry(models.endpoints,providers,model_groups=models.model_groups,recovery=models.recovery, concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))


@pytest.mark.parametrize('end',['cancel','finish'])
async def test_parallel_preflight_and_stream_lifecycle(case, server, end):
    _, config = case
    controlled = ControlledProvider(first=True)
    async with application(config,controlled,models=paid_models(config)) as (app,client):
        client.headers.update(await enroll(app,'orders','0.010'))
        payload = body(max_output_tokens=8)
        created = await client.post('/v1/llm/stream',json=payload)
        rid = created.json()['data']['request_id']
        url = f'/v1/llm/stream/{rid}/events'
        async def balance():
            return (await client.get('/v1/billing/summary')).json()['data']
        assert Decimal((await balance())['reserved']) == 0
        subscriptions = [asyncio.create_task(client.get(url)) for _ in range(2)]
        try:
            await asyncio.wait_for(controlled.entered.wait(),5)
            assert Decimal((await balance())['reserved']) == Decimal('.008')
            refused = await client.post('/v1/llm',json=payload)
            assert (refused.status_code,refused.json()['code']) == (402,'insufficient_budget')
            assert not controlled.calls
            another = await client.post('/v1/llm/stream',json=payload)
            denied = await client.get('/v1/llm/stream/'+another.json()['data']['request_id']+'/events')
            assert frames(denied.text)[-1][2]['code'] == 'insufficient_budget'
            assert len(controlled.stream_calls) == 1
            if end == 'cancel':
                result = await client.post(f'/v1/llm/stream/{rid}/cancel')
                assert result.status_code in (200,202)
        finally:
            controlled.release.set()
            replies = await asyncio.wait_for(asyncio.gather(*subscriptions),6)
        events = frames(replies[0].text)
        assert events[-1][1] == ('response.canceled' if end=='cancel' else 'response.completed')
        assert frames((await client.get(url,params={'after_seq':events[-1][0]-1})).text) == [events[-1]]
        totals = await balance()
        assert Decimal(totals['reserved']) == Decimal(totals['pending']) == Decimal('.008')
        assert totals['pending_attempts'] == len(controlled.stream_calls) == 1
        async with app.state.storage.database.transaction() as s:
            assert await s.scalar(text('SELECT count(*) FROM tb_billing_attempt')) == 1


async def test_pending_cancel_never_creates_a_hold(case,server):
    _, config = case
    provider = ControlledProvider()
    async with application(config,provider,models=paid_models(config)) as (app,client):
        client.headers.update(await enroll(app,'orders'))
        rid = (await client.post('/v1/llm/stream',json=body())).json()['data']['request_id']
        while (await client.post(f'/v1/llm/stream/{rid}/cancel')).status_code==202:
            await asyncio.sleep(.01)
        detail = (await client.get('/v1/billing/requests/'+rid)).json()['data']
        assert detail['status']=='canceled' and not detail['attempts'] and not provider.stream_calls
        assert Decimal(detail['charged']) == Decimal(detail['reserved']) == 0
