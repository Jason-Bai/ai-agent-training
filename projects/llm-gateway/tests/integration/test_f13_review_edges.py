"""Review regressions: billing purpose, queued evidence, shared timeout.

Only owned PostgreSQL, local HTTP and controlled Provider scripts are used.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import threading
from time import monotonic

from fastapi.testclient import TestClient

from providers.bailian import BailianProvider
from test_f13_lifecycle import (f12_pg, lifecycle_case, application, PhaseProvider,
    HEADERS, body, bill, get_trace, reported, NO_PERMITS)
from protocol.test_f5_providers import stream_server, frame


def test_bill_attempts_directly_identify_classification_and_generation(lifecycle_case):
    provider = PhaseProvider(('classification','{"group":"general"}'),('generation','订单A123充电器'))
    with TestClient(application(lifecycle_case,provider),headers=HEADERS) as client:
        reply = client.post('/v1/llm',json=body())
        assert reply.status_code == 200,reply.text
        rid = reply.headers['x-request-id']; detail = bill(client,rid)
        assert [item.get('purpose') for item in detail['attempts']] == ['classification','generation']
        assert [item['attempt_no'] for item in detail['attempts']] == [1,2]
        assert [call['phase'] for call in provider.calls] == ['classification','generation']
        assert [item['purpose'] for item in get_trace(client,rid)['attempts']] == ['classification','generation']


def test_bailian_queued_eof_preserves_original_upstream_status_in_sql_and_trace(lifecycle_case,stream_server):
    stream_server.data = frame({'type':'response.created','response':{
        'object':'response','status':'queued','model':''}})
    provider = BailianProvider(stream_server.url,'fake-local-queued-key')
    pg,name,_,definition = lifecycle_case
    definition = replace(definition,providers={
        'controlled-main':definition.providers['controlled-main'].model_copy(
            update={'base_url':stream_server.url})})
    app = application(lifecycle_case,provider,definition=definition)
    with TestClient(app,headers=HEADERS) as client:
        try:
            created = client.post('/v1/llm/stream',json=body(streaming=True,model='general'))
            assert created.status_code == 202,created.text
            rid = created.json()['data']['request_id']
            reply = client.get('/v1/llm/stream/'+rid+'/events')
            assert reply.status_code == 200 and 'response.failed' in reply.text,reply.text
            assert len(stream_server.calls) == 1
            assert stream_server.calls[0]['path'] == '/responses'
            with pg.connect(name) as sql:
                row = sql.execute('SELECT a.upstream_status,a.actual_model,a.status FROM tb_llm_attempt a '
                    'JOIN tb_llm_request r ON r.id=a.request_pk WHERE r.request_id=%s',(rid,)).fetchall()
            assert row == [('queued',None,'failed')]
            data = get_trace(client,rid)
            assert data['status'] == 'failed' and len(data['attempts']) == 1
            assert data['attempts'][0]['upstream_status'] == 'queued'
            assert data['attempts'][0]['actual_model'] is None
            assert data['attempts'][0]['usage'] is None
            assert app.state.concurrency_limiter.snapshot() == NO_PERMITS
        finally:
            client.portal.call(provider.aclose)


def test_classifier_local_timeout_defaults_with_original_total_deadline(lifecycle_case,record_property):
    entered = threading.Event(); cleaned = threading.Event(); observed = {}
    async def slow_classifier(model):
        observed['classifier_entered_at'] = monotonic(); entered.set()
        try:
            await asyncio.sleep(6)
            return reported('{"group":"general"}',model)
        finally:
            cleaned.set()
    async def immediate_generation(model):
        observed['generation_entered_at'] = monotonic()
        observed['generation_permits'] = app.state.concurrency_limiter.snapshot()
        return reported('订单A123充电器',model,input_tokens=60,output_tokens=12)
    provider = PhaseProvider(('classification',slow_classifier),('generation',immediate_generation))
    app = application(lifecycle_case,provider)
    pg,name,_,_ = lifecycle_case
    with TestClient(app,headers=HEADERS) as client:
        payload = body(); payload['timeout_seconds'] = 10
        started = monotonic()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.post,'/v1/llm',json=payload)
            assert entered.wait(4), 'classification did not begin'
            assert app.state.concurrency_limiter.snapshot() == {'active_requests':1,'agents':{'legacy-test':1}}
            # This owned database contains exactly this one submitted request.
            with pg.connect(name) as sql:
                request_id,created_at,deadline_at = sql.execute(
                    "SELECT request_id::text,created_at,deadline_at FROM tb_llm_request WHERE requested_model='auto-general'").fetchone()
            reply = future.result(timeout=10)
        elapsed = monotonic()-started
        assert reply.status_code == 200,reply.text
        rid = reply.headers['x-request-id']; assert rid == request_id
        assert cleaned.is_set() and provider.closed == ['classification','generation']
        assert [call['phase'] for call in provider.calls] == ['classification','generation']
        class_options = provider.calls[0]['options']; generation_options = provider.calls[1]['options']
        assert 0 < class_options['timeout_seconds'] <= 5
        assert 0 < generation_options['timeout_seconds'] <= 5.1
        assert 4.5 <= elapsed < 9
        assert observed['generation_entered_at']-observed['classifier_entered_at'] >= 4.5
        assert observed['generation_permits'] == {'active_requests':1,'agents':{'legacy-test':1}}
        assert app.state.concurrency_limiter.snapshot() == NO_PERMITS
        data = get_trace(client,rid); detail = bill(client,rid)
        assert data['status'] == 'completed' and reply.json()['data']['text'] == '订单A123充电器'
        assert reply.json()['data']['usage'] is None
        auto = data['auto_routing']
        assert auto['source'] == 'default_fallback' and auto['resolved_group'] == 'general'
        assert auto['fallback_reason'] == 'provider_timeout'
        assert [(attempt['attempt_no'],attempt['purpose'],attempt['status']) for attempt in data['attempts']] == [
            (1,'classification','failed'),(2,'generation','completed')]
        assert data['attempts'][0]['error_code'] == 'provider_timeout' and data['attempts'][0]['usage'] is None
        assert data['attempts'][1]['usage'] == {'input_tokens':60,'output_tokens':12,'total_tokens':72}
        with pg.connect(name) as sql:
            saved_created,saved_deadline = sql.execute('SELECT created_at,deadline_at FROM tb_llm_request WHERE request_id=%s',(rid,)).fetchone()
            charges = sql.execute('SELECT b.state,b.agent_charge,b.supplier_cost FROM tb_billing_attempt b '
                'JOIN tb_llm_request r ON r.id=b.request_pk WHERE r.request_id=%s ORDER BY b.attempt_no',(rid,)).fetchall()
            attempts = sql.execute('SELECT a.attempt_no,a.status FROM tb_llm_attempt a '
                'JOIN tb_llm_request r ON r.id=a.request_pk WHERE r.request_id=%s ORDER BY a.attempt_no',(rid,)).fetchall()
        assert saved_created == created_at and saved_deadline == deadline_at
        assert 9.8 <= (deadline_at-created_at).total_seconds() <= 10.1
        assert attempts == [(1,'failed'),(2,'completed')]
        # Synthetic fixture rates: uncached input1/output2 CNY per million.
        expected = (Decimal(60)+Decimal(12)*2)/Decimal(1000000)
        assert charges == [('pending',None,None),('settled',expected,expected)]
        assert detail['attempts'][0]['state'] == 'pending' and detail['attempts'][0]['agent_charge'] is None
        assert Decimal(detail['attempts'][1]['agent_charge']) == Decimal(detail['charged']) == expected
        record_property('classifier_timeout_observation',dict(request_id=rid,elapsed_seconds=elapsed,
            classification_timeout=class_options['timeout_seconds'],generation_timeout=generation_options['timeout_seconds'],
            original_deadline_at=deadline_at.isoformat()))
