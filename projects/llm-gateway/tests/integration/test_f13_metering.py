from pricing_support import write_pricing
"""A06/A08–A11: isolated PG proves estimates, real-fee paths and accounting."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import asyncio
import threading
import time

from fastapi.testclient import TestClient
import pytest
import yaml

from contracts import Usage
from f12_cases import FixedCounter, request
from legacy_identity import HEADERS
from main import create_app
from providers.base import ProviderResult, ProviderStreamEvent
from providers.mock import MockProvider
from services.models import ProviderConfig
from services.token_budget import TokenBudget
from settings import Settings
from test_f12_stream_plan import f12_pg, f12_case, rows, trace


MODEL='qwen3.7-plus'


class EstimatedCounter(FixedCounter):
    kind='estimated'


def result(*,tokens=200,cached=20,actual=MODEL,observed=True,reported_usage=True):
    return ProviderResult(text='订单A123充电器',actual_model=actual,
        usage=Usage(input_tokens=tokens,output_tokens=16,total_tokens=tokens+16) if reported_usage else None,
        cached_input_tokens=cached,
        billing_observed_at=datetime.now(timezone.utc) if observed else None)


def events(value):
    return [ProviderStreamEvent(delta=value.text),ProviderStreamEvent(status='completed',
        actual_model=value.actual_model,usage=value.usage,cached_input_tokens=value.cached_input_tokens,
        billing_observed_at=value.billing_observed_at,upstream_terminal_received=True)]


def application(case,provider):
    _,directory,baseline=case
    endpoint=baseline.get_endpoint('chat-a').model_copy(update={
        'model_id':MODEL,'token_counter':'qwen_text_estimate_v1',
        'structured_output_mode':'json_schema','min_output_tokens':16})
    definition=replace(baseline,endpoints={'chat-a':endpoint},providers={
        'mock-main':ProviderConfig(adapter='bailian',api_protocol='responses',
            base_url='http://127.0.0.1:9',credential_ref='unused-injected-key')},
        model_groups={'general-primary':baseline.get_group('general-primary').model_copy(
            update={'candidates':('chat-a',)})})
    low={'hit':'0.5','miss':'1','output':'2'};high={'hit':'2','miss':'10','output':'20'}
    write_pricing(directory, {'bindings':{'mock-main':'bailian-fixture'},
        'versions':[{'supplier':'bailian-fixture','model':MODEL,'version':'metering-v1',
            'effective_from':'2020-01-01T00:00:00Z','peak':low,'off_peak':low,
            'input_tiers':[{'max_input_tokens':100,'peak':low,'off_peak':low},
                           {'max_input_tokens':1000000,'peak':high,'off_peak':high}]}]})
    return create_app(settings=Settings(env='test',_env_file=None),config_dir=directory,
        model_registry=definition,providers={'mock-main':provider},
        token_budget=TokenBudget({'qwen_text_estimate_v1':EstimatedCounter(100,'metering-est-v1')}))


def body(*,streaming=False):
    return request(streaming=streaming,max_output_tokens=32).model_dump(exclude_none=True)


def funds(pg,name,amount):
    with pg.connect(name) as sql:
        sql.execute("UPDATE tb_billing_operation SET amount=%s WHERE operation_id='initial-test-funds'",
                    (Decimal(amount),))


def bill(client,rid):
    r=client.get('/v1/billing/requests/'+rid);assert r.status_code==200,r.text
    return r.json()['data']


def assert_estimated(client,rid,attempts):
    data=trace(client,rid)
    assert data['input_token_kind']=='estimated'
    assert [a['input_token_kind'] for a in data['attempts']]==['estimated']*attempts
    assert data['counter_version']=='metering-est-v1'
    return data


@pytest.mark.parametrize('streaming',[False,True])
def test_estimated_kind_and_actual_cross_tier_cost_are_persisted(f12_case,f12_pg,streaming):
    name,_,_=f12_case;value=result();provider=MockProvider(result=value,stream_events=events(value))
    with TestClient(application(f12_case,provider),headers=HEADERS) as client:
        r=client.post('/v1/llm/stream' if streaming else '/v1/llm',json=body(streaming=streaming))
        assert r.status_code==(202 if streaming else 200),r.text
        rid=r.json()['data']['request_id'] if streaming else r.headers['x-request-id']
        if streaming:
            assert_estimated(client,rid,0)
            assert not provider.calls and not provider.stream_calls
            row,attempts,_,bills=rows(f12_pg,name,rid);assert not attempts and not bills
            assert 'response.completed' in client.get(f'/v1/llm/stream/{rid}/events').text
        assert assert_estimated(client,rid,1)['status']=='completed'
        detail=bill(client,rid);attempt=detail['attempts'][0]
        # 100 estimated input at highest miss10 and 32 output20 = .00164 reserved.
        # Actual high tier: cached20*2 + uncached180*10 + output16*20 = .00216.
        assert attempt['adapter']=='bailian' and attempt['usage_source']=='real'
        assert attempt['state']=='settled'
        assert Decimal(attempt['reserved_amount'])==Decimal('.00164')
        assert Decimal(attempt['agent_charge'])==Decimal('.00216')
        with f12_pg.connect(name) as sql:
            timing,=sql.execute('SELECT timing_snapshot FROM tb_llm_request WHERE request_id=%s',(rid,)).fetchone()
            assert 'estimated' in timing
            assert sql.execute('SELECT agent_charge FROM tb_billing_attempt').fetchall()==[(Decimal('.00216'),)]
        assert len(provider.calls)+len(provider.stream_calls)==1


def test_pending_restart_execution_and_historical_query_keep_estimate_kind(f12_case,f12_pg):
    provider=MockProvider();app=application(f12_case,provider)
    with TestClient(app,headers=HEADERS) as client:
        rid=client.post('/v1/llm/stream',json=body(streaming=True)).json()['data']['request_id']
        assert_estimated(client,rid,0)
    value=result();running=MockProvider(stream_events=events(value))
    with TestClient(application(f12_case,running),headers=HEADERS) as client:
        assert 'response.completed' in client.get(f'/v1/llm/stream/{rid}/events').text
        assert_estimated(client,rid,1)
    with TestClient(application(f12_case,MockProvider()),headers=HEADERS) as client:
        assert assert_estimated(client,rid,1)['status']=='completed'
    assert not provider.stream_calls and len(running.stream_calls)==1


def cancel_until_terminal(client,rid):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        r=client.post(f'/v1/llm/stream/{rid}/cancel');assert r.status_code in (200,202),r.text
        if r.status_code==200:return r.json()['data']['status']
        time.sleep(.01)
    pytest.fail('cancel did not settle')


@pytest.mark.parametrize('running',[False,True])
def test_cancel_persists_estimate_without_inventing_usage(f12_case,f12_pg,running):
    name,_,_=f12_case;entered=threading.Event()
    async def hold():
        entered.set();await asyncio.sleep(60)
    provider=MockProvider(stream_events=[hold]);app=application(f12_case,provider)
    with TestClient(app,headers=HEADERS) as client:
        rid=client.post('/v1/llm/stream',json=body(streaming=True)).json()['data']['request_id']
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(client.get,f'/v1/llm/stream/{rid}/events') if running else None
            if running:assert entered.wait(3)
            assert cancel_until_terminal(client,rid)=='canceled'
            data=assert_estimated(client,rid,int(running));assert data['status']=='canceled'
            if future:assert 'response.canceled' in future.result(timeout=5).text
        _,attempts,_,bills=rows(f12_pg,name,rid)
        assert len(attempts)==int(running)
        assert bills==[('pending',)] if running else bills==[]
        assert len(provider.stream_calls)==int(running)
        assert app.state.concurrency_limiter.snapshot()['active_requests']==0


def test_actual_overrun_creates_debt_blocks_paid_calls_and_deposit_restores(f12_case,f12_pg):
    name,_,_=f12_case;funds(f12_pg,name,'.002');provider=MockProvider(result=result())
    with TestClient(application(f12_case,provider),headers=HEADERS) as client:
        first=client.post('/v1/llm',json=body());assert first.status_code==200,first.text
        summary=client.get('/v1/billing/summary').json()['data']
        assert Decimal(summary['available'])==Decimal('-.00016')
        refused=client.post('/v1/llm',json=body());assert refused.status_code==402,refused.text
        assert refused.json()['code']=='insufficient_budget' and len(provider.calls)==1
        assert bill(client,first.headers['x-request-id'])['billing_status']=='settled'
        with f12_pg.connect(name) as sql:
            sql.execute("INSERT INTO tb_billing_operation(operation_id,kind,agent_id,amount,reason,details) VALUES ('restore-funds','deposit','legacy-test',1,'test','{}')")
        restored=client.post('/v1/llm',json=body());assert restored.status_code==200,restored.text
        assert len(provider.calls)==2
        assert Decimal(client.get('/v1/billing/summary').json()['data']['available'])==Decimal('.99768')


@pytest.mark.parametrize('changes',[{'cached':None},{'observed':False},{'actual':None},
                                  {'reported_usage':False},{'actual':'qwen3.8-max'},{'cached':201}])
def test_missing_actual_billing_evidence_stays_pending(f12_case,changes):
    provider=MockProvider(result=result(**changes))
    with TestClient(application(f12_case,provider),headers=HEADERS) as client:
        r=client.post('/v1/llm',json=body());assert r.status_code==200,r.text
        attempt=bill(client,r.headers['x-request-id'])['attempts'][0]
        assert attempt['state']=='pending' and attempt['agent_charge'] is None
        assert Decimal(attempt['reserved_amount'])==Decimal('.00164')


@pytest.mark.parametrize('tokens,cached,cost',[
    (99,0,'.000131'),(99,20,'.000121'),(99,99,'.0000815'),
    (100,0,'.000132'),(100,20,'.000122'),(100,100,'.000082'),
    (101,0,'.00133'),(101,20,'.00117'),(101,101,'.000522')])
def test_actual_tier_boundaries_and_cache_split_use_reported_tokens(f12_case,tokens,cached,cost):
    provider=MockProvider(result=result(tokens=tokens,cached=cached))
    with TestClient(application(f12_case,provider),headers=HEADERS) as client:
        reply=client.post('/v1/llm',json=body());assert reply.status_code==200,reply.text
        rid=reply.headers['x-request-id'];detail=bill(client,rid)
        entry=detail['attempts'][0]
        assert entry['state']=='settled' and Decimal(entry['agent_charge'])==Decimal(cost)
        assert entry['input_tokens']==tokens and entry['cached_input_tokens']==cached
        assert len(entry['quote']['input_tiers'])==2
        assert entry['usage_source']=='real' and len(provider.calls)==1
        assert_estimated(client,rid,1)


def test_concurrent_actual_overruns_settle_once_and_preserve_account_arithmetic(f12_case,f12_pg):
    name,_,_=f12_case;funds(f12_pg,name,'.004');entered=threading.Barrier(2)
    class Gated(MockProvider):
        async def complete(self,*args,**kwargs):
            await asyncio.to_thread(entered.wait,3)
            return await super().complete(*args,**kwargs)
    provider=Gated(result=result())
    with TestClient(application(f12_case,provider),headers=HEADERS) as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(client.post,'/v1/llm',json=body()) for _ in range(2)]
            replies=[f.result(timeout=7) for f in futures]
        assert [r.status_code for r in replies]==[200,200]
        ids={r.headers['x-request-id'] for r in replies};assert len(ids)==2
        assert all(Decimal(bill(client,rid)['charged'])==Decimal('.00216') for rid in ids)
        summary=client.get('/v1/billing/summary').json()['data']
        assert Decimal(summary['charged'])==Decimal('.00432')
        assert Decimal(summary['available'])==Decimal('-.00032')
        assert Decimal(summary['reserved'])==0
        assert client.post('/v1/llm',json=body()).status_code==402
        assert len(provider.calls)==2
