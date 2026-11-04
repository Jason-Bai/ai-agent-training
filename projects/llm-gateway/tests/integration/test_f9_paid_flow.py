from pricing_support import write_pricing
from registry_helpers import ModelConfig, ModelRegistry
"""Test-owned provider HTTP servers, real Gateway/SDK/PG, fixed fixture prices."""
from services.models import ConcurrencyPolicy
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import httpx
import pytest

from f3_cases import CountingFixture, MODEL_VALUES
from f6_cases import case, server
from f7_cases import body
from main import create_app
from persistence.billing import resolve
from services.models import ProviderConfig, RecoveryPolicy
from settings import Settings
from test_f9_billing_queries import enroll
from protocol.test_providers import server as primary_wire, server as backup_wire, SUCCESS

import yaml


@asynccontextmanager
async def account(config, primary_wire, backup_wire, rates, *, next_tariff=None):
    definitions = {
        'chat': ProviderConfig(adapter='deepseek', api_protocol='chat_completions',
                               base_url=primary_wire.url, credential_ref='test'),
        'response': ProviderConfig(adapter='openai_compatible', api_protocol='responses',
                                   base_url=backup_wire.url, credential_ref='test'),
    }
    prices = dict(zip(('hit', 'miss', 'output'), rates))
    tariff = dict(supplier='fixture', model='deepseek-flash', version='test-v1',
                  effective_from='2020-01-01T00:00:00Z', peak=prices, off_peak=prices)
    write_pricing(config, {'bindings': dict.fromkeys(definitions, 'fixture'),
                                                      'versions': [tariff, tariff | next_tariff] if next_tariff else [tariff]})
    (config/'credentials.yaml').write_text('credentials: {test: {api_key: local-wire-fixture}}')
    models = {name: ModelConfig(**(MODEL_VALUES | {'provider': provider, 'fallback_models': fallback}))
              for name, provider, fallback in [('general-primary', 'chat', ('general-backup',)),
                                                ('general-backup', 'response', ())]}
    registry = ModelRegistry(models, definitions, recovery=RecoveryPolicy(max_attempts=2, max_attempts_per_endpoint=1), concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))
    app = create_app(settings=Settings(env='test', _env_file=None), model_registry=registry,
                     config_dir=config, token_budget=CountingFixture(250).as_budget())
    async with app.router.lifespan_context(app):
        headers = await enroll(app, 'agent-a123')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://test', headers=headers) as client:
            yield app, client


def responses_body(output):
    return json.dumps(dict(object='response', model='deepseek-flash', status='completed',
                           output=[dict(type='message', role='assistant', status='completed',
                                        content=[dict(type='output_text', text='A123')])],
                           usage=dict(input_tokens=200, output_tokens=output, total_tokens=200+output,
                                      input_tokens_details={'cached_tokens':150})))


@pytest.mark.parametrize('model', ['general-primary', 'general-backup'])
async def test_b01_fixed_prices_cross_the_provider_boundary(server, case, primary_wire, backup_wire, model):
    name, config = case
    primary_wire.body = SUCCESS | {'usage': dict(prompt_tokens=200, completion_tokens=20, total_tokens=220,
                                                  prompt_cache_hit_tokens=150, prompt_cache_miss_tokens=50)}
    backup_wire.raw = responses_body(20).encode()
    async with account(config, primary_wire, backup_wire, ('0.4', '2', '8')) as (_, client):
        result = await client.post('/v1/llm', json=body(model=model, max_output_tokens=100))
        assert result.status_code == 200, result.text
        assert len(primary_wire.calls) + len(backup_wire.calls) == 1
        rid = result.headers['x-request-id']
        data = (await client.get(f'/v1/billing/requests/{rid}')).json()['data']
        attempt, = data['attempts']
        assert attempt['cached_input_tokens'] == 150
        assert Decimal(attempt['reserved_amount']) == Decimal('.001300')
        assert Decimal(attempt['agent_charge']) == Decimal('.000320')
        summary = (await client.get('/v1/billing/summary')).json()['data']
        assert Decimal(summary['available']) == Decimal('.999680')
        with server.connect(name) as conn:
            saved, = conn.execute('SELECT reserved_amount, agent_charge, supplier_cost FROM tb_billing_attempt').fetchall()
        assert saved == (Decimal('.001300'), Decimal('.000320'), Decimal('.000320'))


@pytest.mark.parametrize('action,amount,available', [('settle','.008','.986'), ('zero',None,'.994'),
                                                     ('platform',None,'.994')])
async def test_b04_unknown_primary_known_backup_disposition(server, case, primary_wire, backup_wire,
                                                           action, amount, available):
    name, config = case
    primary_wire.status = 503  # rejected without trustworthy usage
    backup_wire.raw = responses_body(40).encode()
    async with account(config, primary_wire, backup_wire, ('20','20','50')) as (app, client):
        reply = await client.post('/v1/llm', json=body(max_output_tokens=100))
        assert reply.status_code == 200, reply.text
        assert len(primary_wire.calls) == len(backup_wire.calls) == 1
        rid = reply.headers['x-request-id']
        item, = (await client.get('/v1/billing/requests')).json()['data']['items']
        detail = (await client.get('/v1/billing/requests/'+rid)).json()['data']
        assert item['request_id'] == rid and item['billing_status'] == 'partially_pending'
        assert [a['state'] for a in detail['attempts']] == ['pending','settled']
        summary = (await client.get('/v1/billing/summary')).json()['data']
        assert [Decimal(summary[k]) for k in ['available','charged','pending']] == [
            Decimal('.984'), Decimal('.006'), Decimal('.010')]
        assert summary['usage']['real']['unknown_attempts'] == 1
        for _ in range(2):
            async with app.state.storage.database.transaction() as session:
                await resolve(session, rid, 1, 'disposition-A123', action, amount, 'fixture statement')
        with server.connect(name) as conn:
            row = conn.execute('SELECT agent_charge,supplier_cost FROM tb_billing_attempt WHERE attempt_no=1').fetchone()
        assert row == (Decimal(amount or '0'), None if action=='platform' else Decimal(amount or '0'))
        summary = (await client.get('/v1/billing/summary')).json()['data']
        assert Decimal(summary['available']) == Decimal(available)
        assert Decimal(summary['pending']) == 0


@pytest.mark.parametrize('final_report', [False, True])
async def test_r1_chat_usage_without_done_requires_final_report(server, case, primary_wire, backup_wire, final_report):
    name, config = case
    report = dict(prompt_tokens=10, completion_tokens=1, total_tokens=11,
                  prompt_cache_hit_tokens=7, prompt_cache_miss_tokens=3)
    chunk = dict(object='chat.completion.chunk', model='deepseek-flash', usage=report,
                 choices=[dict(index=0, delta={'content':'x'}, finish_reason='stop' if final_report else None)])
    primary_wire.raw = ('data: '+json.dumps(chunk)+'\n\n').encode()
    async with account(config, primary_wire, backup_wire, ('20','20','50')) as (_, client):
        result = await client.post('/v1/llm/stream', json=body(max_output_tokens=100))
        rid = result.headers['x-request-id']
        events = await client.get(f'/v1/llm/stream/{rid}/events')
        assert 'response.failed' in events.text
        detail = (await client.get(f'/v1/billing/requests/{rid}')).json()['data']['attempts'][0]
        assert detail['total_tokens'] == 11
        assert detail['state'] == ('settled' if final_report else 'pending')
        if final_report:
            assert Decimal(detail['agent_charge']) == Decimal('.000250')
        else:
            assert detail['agent_charge'] is None and detail['supplier_cost'] is None


@pytest.mark.parametrize('offset,expected_status', [(5, 402), (120, 200)])
async def test_r1_known_hike_only_within_deadline(server, case, primary_wire, backup_wire, monkeypatch,
                                                 offset, expected_status):
    from persistence import store
    start = datetime.now(timezone.utc)
    monkeypatch.setattr(store, 'now', lambda: start)
    high = dict(hit='10000', miss='10000', output='10000')
    hike = dict(version='expensive', effective_from=(start+timedelta(seconds=offset)).isoformat(), peak=high, off_peak=high)
    async with account(case[1], primary_wire, backup_wire, ('1','1','1'), next_tariff=hike) as (_, client):
        result = await client.post('/v1/llm', json=body(max_output_tokens=100, timeout_seconds=10))
        assert result.status_code == expected_status
        if expected_status == 402:
            assert result.json()['code'] == 'insufficient_budget'
            assert len(primary_wire.calls) == len(backup_wire.calls) == 0
            summary = (await client.get('/v1/billing/summary')).json()['data']
            assert Decimal(summary['available']) == 1 and Decimal(summary['reserved']) == 0
        else:
            assert len(primary_wire.calls) == 1 and not backup_wire.calls
            rid = result.headers['x-request-id']
            attempt, = (await client.get(f'/v1/billing/requests/{rid}')).json()['data']['attempts']
            assert Decimal(attempt['reserved_amount']) == Decimal('.000350')


@pytest.mark.parametrize('model', ['general-primary', 'general-backup'])
@pytest.mark.parametrize('crossed', [False, True])
async def test_r1_final_report_time_drives_settlement(server, case, primary_wire, backup_wire, monkeypatch, model, crossed):
    from persistence import store
    from providers import base
    start = datetime.now(timezone.utc) - timedelta(minutes=5)
    # Isolate UTC observation time from the later wall time of the database writer.
    boundary = start + timedelta(seconds=5)
    reported = boundary + timedelta(seconds=1 if crossed else -1)
    class ObservedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return reported.astimezone(tz)

    monkeypatch.setattr(store, 'now', lambda: start)
    monkeypatch.setattr(base, 'datetime', ObservedClock)
    extra = dict(version='later', effective_from=boundary.isoformat())
    backup_wire.raw = responses_body(20).encode()
    async with account(case[1], primary_wire, backup_wire, ('1','1','1'), next_tariff=extra) as (_, client):
        result = await client.post('/v1/llm', json=body(model=model, max_output_tokens=100))
        assert result.status_code == 200, result.text
        rid = result.headers['x-request-id']
        attempt, = (await client.get(f'/v1/billing/requests/{rid}')).json()['data']['attempts']
        assert attempt['state'] == ('pending' if crossed else 'settled')
        with server.connect(case[0]) as db:
            stored, = db.execute('SELECT billing_observed_at FROM tb_billing_attempt').fetchone()
        assert stored == reported
