"""C06: cancel actual non-streaming classifier HTTP before local consumption.

An HTTP 200 is observed from real Bailian before the test blocks its response
hook. This proves local response cleanup, not remote generation/billing stopped.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import sys
import threading
from time import monotonic, sleep

import httpx2
import pytest

from f13_real_support import (MODELS, f13_live_pg, f13_real_environment, save_evidence)
from services.classification import CLASSIFIER_TEMPLATE
from live.test_f13_routing import verify_quality_lifecycle
from utils.json_codec import loads

pytestmark = pytest.mark.live


def phase(request):
    payload = loads(request.content)
    messages = payload['input']
    return ('classification' if messages[0]['content'] == CLASSIFIER_TEMPLATE.system_template
            else 'generation'), payload['model']


def terminal_cancel(client,rid):
    deadline = monotonic()+10
    while monotonic() < deadline:
        reply = client.post('/v1/llm/stream/'+rid+'/cancel')
        assert reply.status_code in (200,202),reply.text
        state = reply.json()['data']['status']
        if state in {'completed','failed','canceled'}: return state
        assert state == 'canceling'
        sleep(.01)
    pytest.fail('classification cancellation did not reach terminal')


def test_real_classifier_http_cancel_closes_response_and_never_starts_generation(
        f13_real_environment,monkeypatch,record_property):
    env = f13_real_environment; app = env.application
    provider = app.state.providers['bailian-main']
    sdk_http = provider.client._client
    received = threading.Event(); closed = threading.Event(); hook_exited = threading.Event()
    captured = {}; outbound = []; statuses = []
    original_close = httpx2.Response.aclose

    async def observe_close(response):
        # Observe the actual close operation; do not replace or suppress it.
        await original_close(response)
        if response is captured.get('response'):
            assert response.is_closed
            closed.set()

    async def observe_request(request):
        purpose,model = phase(request)
        outbound.append(dict(phase=purpose,model=model))

    async def hold_classifier_response(response):
        purpose,model = phase(response.request)
        statuses.append(dict(phase=purpose,model=model,status=response.status_code))
        if purpose != 'classification' or response.status_code != 200 or captured:
            return
        captured.update(response=response,gate=asyncio.Event())
        received.set()
        try:
            await captured['gate'].wait()
        finally:
            hook_exited.set()

    monkeypatch.setattr(httpx2.Response,'aclose',observe_close)
    sdk_http.event_hooks.setdefault('request',[]).append(observe_request)
    sdk_http.event_hooks.setdefault('response',[]).append(hold_classifier_response)
    rid = None; reader = None; reply = None; setup_failures = []

    def preserve(stage):
        """Retain completed prerequisite calls and failed assertions alike."""
        if rid is None:
            return
        document = dict(request_id=rid,stage=stage,setup_failures=list(setup_failures),
            outbound=list(outbound),http_status_observations=list(statuses),
            http_200_received=received.is_set(),real_response_closed=closed.is_set(),
            response_hook_exited=hook_exited.is_set(),response=reply.text if reply is not None else None,
            scope='real classifier cancellation prerequisite attempts are distinct requests and are not cancellation successes')
        for name,path in [('trace','/v1/traces/'+rid),('billing','/v1/billing/requests/'+rid)]:
            try:
                result = env.client.get(path)
                document[name] = dict(http_status=result.status_code,body=result.json())
            except Exception as exc:
                document[name] = dict(unavailable=True,error_type=type(exc).__name__)
        observation_path = env.directory/('availability-'+rid+'.json')
        if observation_path.exists():
            try: document['upstream_observation'] = json.loads(observation_path.read_text())
            except (OSError,ValueError) as exc:
                document['upstream_observation'] = dict(unavailable=True,error_type=type(exc).__name__)
        (env.directory/('classifier-cancel-'+rid+'-'+stage+'.json')).write_text(
            json.dumps(document,ensure_ascii=False))

    try:
        body = dict(model='auto-general',prompt={'name':'basic_text','version':'1.0.0'},
            messages=[{'role':'user','content':'请提取订单A123的订单号。'}],
            max_output_tokens=128,timeout_seconds=60)
        for setup_attempt in range(1,4):
            # Reset only after the preceding request and its evidence settled.
            received.clear(); closed.clear(); hook_exited.clear()
            captured.clear(); outbound.clear(); statuses.clear(); reply = None; rid = None
            created = env.client.post('/v1/llm/stream',json=body)
            assert created.status_code == 202,created.text
            rid = created.json()['data']['request_id']; url = '/v1/llm/stream/'+rid+'/events'
            assert not outbound and env.trace(rid)['status'] == 'pending'
            with ThreadPoolExecutor(max_workers=1) as pool:
                reader = pool.submit(env.client.get,url)
                try:
                    ready = received.wait(12)
                    if ready:
                        assert app.state.concurrency_limiter.snapshot() == {
                            'active_requests':1,'agents':{'legacy-test':1}}
                        assert terminal_cancel(env.client,rid) == 'canceled'
                    reply = reader.result(timeout=6 if ready else 70)
                finally:
                    # A failing assertion must not leave the test-side hook blocked.
                    if captured and not hook_exited.is_set():
                        env.client.portal.call(captured['gate'].set)
            if ready:
                break
            failed_trace = env.trace(rid)
            failed_bill = env.client.get('/v1/billing/requests/'+rid)
            routing = failed_trace.get('auto_routing') or {}
            failure = dict(request_id=rid,attempt=setup_attempt,
                fallback_reason=routing.get('fallback_reason'),validated=False,
                evidence='classifier-cancel-'+rid+'-setup-unavailable.json')
            setup_failures.append(failure)
            preserve('setup-unavailable')
            # No broad retry policy: only these unavailable prerequisites may
            # create another independent request. Completed fallbacks still owe
            # full lifecycle, unknown-charge and real Plus settlement checks.
            assert reply.status_code == 200 and failed_trace['status'] == 'completed',reply.text
            assert routing.get('source') == 'default_fallback' and routing.get('resolved_group') == 'general'
            assert routing.get('fallback_reason') in {
                'provider_timeout','provider_connection_error','provider_unavailable','provider_rate_limited'}
            assert not captured and not any(value['phase'] == 'classification' and value['status'] == 200 for value in statuses)
            assert outbound == [dict(phase='classification',model=MODELS['light']),
                                dict(phase='generation',model=MODELS['general'])]
            attempts = failed_trace['attempts']
            assert len(attempts) == 2 and [item['purpose'] for item in attempts] == ['classification','generation']
            assert attempts[0]['status'] == 'failed' and attempts[0]['usage'] is None
            assert attempts[1]['status'] == 'completed' and attempts[1]['actual_model'] == MODELS['general']
            assert 'response.completed' in reply.text and 'response.failed' not in reply.text
            verify_quality_lifecycle(env,rid,failed_trace,reply,failed_bill)
            failure['validated'] = True
            preserve('setup-unavailable')
        else:
            pytest.fail('real classifier HTTP 200 prerequisite unavailable in all 3 independent requests; no cancellation success observed')
        trace = env.trace(rid)
        bill_reply = env.client.get('/v1/billing/requests/'+rid)
        assert bill_reply.status_code == 200,bill_reply.text
        bill = bill_reply.json()['data']
        observation_path = env.directory/('availability-'+rid+'.json')
        observation = json.loads(observation_path.read_text()) if observation_path.exists() else None
        extra = dict(outbound=outbound,http_status_observations=statuses,
            setup_failures=list(setup_failures),
            real_response_closed=closed.is_set(),response_hook_exited=hook_exited.is_set(),
            upstream_observation=observation,response=reply.text,
            scope='actual classifier HTTP 200 then cancellation before local body consumption; remote generation and supplier billing stop not asserted')
        save_evidence(env,rid,trace,bill,extra)
        record_property('f13_request_id',rid)
        record_property('classifier_cancel_setup_failures',json.dumps(setup_failures))
        assert outbound == [dict(phase='classification',model=MODELS['light'])]
        assert statuses == [dict(phase='classification',model=MODELS['light'],status=200)]
        assert closed.is_set() and hook_exited.is_set()
        assert reply.status_code == 200 and 'response.canceled' in reply.text
        assert 'response.delta' not in reply.text and 'response.completed' not in reply.text
        assert trace['status'] == 'canceled' and len(trace['attempts']) == 1
        attempt = trace['attempts'][0]
        assert attempt['purpose'] == 'classification' and attempt['status'] == 'canceled'
        assert attempt['model'] == 'light-endpoint' and attempt['usage'] is None
        assert attempt['actual_model'] is None
        assert trace['usage']['known_subtotal'] is None and trace['usage']['unknown_attempts'] == [1]
        assert len(bill['attempts']) == 1
        charge = bill['attempts'][0]
        assert charge['state'] == 'pending' and charge['agent_charge'] is None and charge['supplier_cost'] is None
        assert charge['usage_source'] == 'real' and charge['usage_complete'] is False
        with env.server.connect(env.database_name) as sql:
            pk,status = sql.execute('SELECT id,status FROM tb_llm_request WHERE request_id=%s',(rid,)).fetchone()
            attempts = sql.execute('SELECT status,input_tokens,output_tokens,total_tokens FROM tb_llm_attempt WHERE request_pk=%s',(pk,)).fetchall()
            events = sql.execute('SELECT seq,event_type,payload FROM tb_llm_stream_event WHERE request_pk=%s ORDER BY seq',(pk,)).fetchall()
            charges = sql.execute('SELECT state,agent_charge,supplier_cost FROM tb_billing_attempt WHERE request_pk=%s',(pk,)).fetchall()
        assert status == 'canceled' and attempts == [('canceled',None,None,None)]
        assert charges == [('pending',None,None)]
        assert [event[0] for event in events] == list(range(1,len(events)+1))
        assert [event[1] for event in events if event[1] in {
            'response.completed','response.failed','response.canceled'}] == ['response.canceled']
        assert sum(event[1] == 'response.canceling' for event in events) == 1
        before = list(outbound)
        assert env.client.get(url,params={'after_seq':0}).text == reply.text
        assert env.client.get(url,params={'after_seq':events[-1][0]}).status_code == 204
        assert outbound == before and env.client.get('/v1/billing/requests/'+rid).json()['data'] == bill
        assert app.state.concurrency_limiter.snapshot() == {'active_requests':0,'agents':{}}
        assert observation is not None and len(observation['attempts']) == 1
        assert observation['attempts'][0]['read_closed'] is True
        save_evidence(env,rid,trace,bill,extra)
    finally:
        primary = sys.exception()
        try:
            if captured and not hook_exited.is_set():
                env.client.portal.call(captured['gate'].set)
            preserve('final')
        except Exception as evidence_error:
            if primary is None:
                raise
            primary.add_note('Final evidence could not be persisted: '+type(evidence_error).__name__)
        finally:
            sdk_http.event_hooks['request'].remove(observe_request)
            sdk_http.event_hooks['response'].remove(hold_classifier_response)
