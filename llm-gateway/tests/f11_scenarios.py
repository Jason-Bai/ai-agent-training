"""Shared F11 public scenarios: the same assertions on scripted Mock and real adapters."""
import asyncio

from bug002_scenarios import committed, evidence, request_state, cancel_until_terminal
from f10_support import CredentialHeaders, payload, frames
from f11_support import counts, held_request, provider_records, release


def arm(env):
    (env.directory/'f11-arm').touch()


def rejected(response):
    assert response.status_code == 429, response.text
    value = response.json()
    assert set(value) == {'success','code','data','message'}
    assert value['success'] is False and value['code'] == 'concurrency_limited'
    assert value['data'] is None
    assert response.headers.get('x-request-id')
    return response.headers['x-request-id']


async def no_attempt(env, rid, *, headers=None):
    response = await env.client.get('/v1/traces/'+rid, headers=headers)
    assert response.status_code == 200
    trace = response.json()['data']
    assert trace['status'] == 'failed' and trace['error_code'] == 'concurrency_limited'
    assert trace['error_stage'] == 'admission'
    assert trace['attempts'] == []
    assert provider_records(env, rid) == []
    saved = await request_state(env, rid)
    assert saved['attempts'] == saved['bills'] == saved['events'] == 0


async def verify_completion(env, result, model):
    assert result['status'] == 'completed', result
    trace = await committed(env, result['request_id'])
    env.assert_provider_contract(trace)
    assert trace['requested_model'] == model
    assert trace['usage']['unknown_attempts'] == []
    checked = await env.verify(result['request_id'], expected_agent='a')
    assert checked['attempts'] == 1
    return checked


async def global_admission_and_pending(env, model, record_property):
    arm(env)
    task = asyncio.create_task(env.call(model, 'text'))
    rid = None
    try:
        rid = await held_request(env)
        assert provider_records(env, rid) == ['complete_entered','complete_result']
        failed = await env.client.post('/v1/llm', json=payload(model), headers=env.other_headers)
        denied_id = rejected(failed)
        await counts(env, {'active_requests':1, 'agents':{'a':1}})
        await no_attempt(env, denied_id, headers=env.other_headers)
        # Creation, read-only/control paths work even at the global ceiling.
        created = await env.client.post('/v1/llm/stream', json=payload(model))
        assert created.status_code == 202
        pending = created.json()['data']['request_id']
        before = await request_state(env, pending)
        assert before == {'status':'pending','attempts':0,'bills':0,'events':0}
        response = await env.client.get(f'/v1/llm/stream/{pending}/events')
        rejected(response)
        assert await request_state(env, pending) == before
        assert provider_records(env, pending) == []
        def pending_times():
            with env.pg.connect(env.name) as sql:
                return sql.execute('SELECT started_at,deadline_at,error_code FROM tb_llm_request WHERE request_id=%s', (pending,)).fetchone()
        assert await asyncio.to_thread(pending_times) == (None,None,None)
        assert (await env.client.get('/healthz')).status_code == 200
        assert (await env.client.get('/v1/traces/'+rid)).status_code == 200
        assert (await env.client.get('/v1/billing/summary')).status_code == 200
        rejected(await env.client.post(f'/v1/models/{model}/health'))
        assert await cancel_until_terminal(env, pending) == 'canceled'
        assert provider_records(env, pending) == []
        # A separate pending request rejected now must be startable later using the same ID.
        retry_pending = (await env.client.post('/v1/llm/stream', json=payload(model))).json()['data']['request_id']
        retry_url = f'/v1/llm/stream/{retry_pending}/events'
        retry_before = await request_state(env, retry_pending)
        rejected(await env.client.get(retry_url))
        assert await request_state(env, retry_pending) == retry_before
        await counts(env, {'active_requests':1, 'agents':{'a':1}})
        release(env, rid)
        result = await task
        checked = await verify_completion(env, result, model)
        await counts(env, {'active_requests':0, 'agents':{}})
        retry_events = []
        async with env.client.stream('GET', retry_url) as response:
            assert response.status_code == 200
            async for event in frames(response):
                if event[1] != 'response.heartbeat': retry_events.append(event)
        assert retry_events[-1][1] == 'response.completed'
        assert (await env.verify(retry_pending, expected_agent='a'))['attempts'] == 1
        retry_trace = await committed(env, retry_pending)
        assert retry_trace['error_code'] is None
        await counts(env, {'active_requests':0, 'agents':{}})
        # Capacity is reusable by a later actual model call.
        subsequent = await env.call(model, 'text')
        await verify_completion(env, subsequent, model)
        await env.assert_balances()
        evidence(record_property, backend=env.backend, model=model, held_request_id=rid,
                 rejected_request_id=denied_id, pending_id=pending, observation=checked)
    finally:
        if rid: release(env, rid)
        if not task.done(): task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def agent_credential_and_model_isolation(env, model, record_property):
    alternate = 'general-backup' if model == 'general-primary' else 'general-primary'
    extra = CredentialHeaders(Authorization='Bearer '+await env.cli('issue','a'))
    arm(env)
    task = asyncio.create_task(env.call(model, 'text'))
    rid = None
    try:
        rid = await held_request(env)
        await counts(env, {'active_requests':1, 'agents':{'a':1}})
        # The second credential and another model cannot mint extra Agent capacity.
        response = await env.client.post('/v1/llm', json=payload(alternate), headers=extra)
        denied = rejected(response)
        await no_attempt(env, denied)
        independent = await env.call(alternate, 'text', headers=env.other_headers)
        assert independent['status'] == 'completed'
        checked_other = await env.verify(independent['request_id'], expected_agent='b')
        env.assert_provider_contract(await committed_other(env, independent['request_id']))
        assert checked_other['attempts'] == 1
        assert (await env.client.get('/v1/traces/'+rid, headers=env.other_headers)).status_code == 404
        await counts(env, {'active_requests':1, 'agents':{'a':1}})
        release(env, rid)
        checked = await verify_completion(env, await task, model)
        await counts(env, {'active_requests':0, 'agents':{}})
        later = await env.call(alternate, 'text')
        await verify_completion(env, later, alternate)
        await env.assert_balances()
        evidence(record_property, backend=env.backend, model=model, held_request_id=rid,
                 rejected_request_id=denied, independent_request_id=independent['request_id'], observation=checked)
    finally:
        if rid: release(env, rid)
        if not task.done(): task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def committed_other(env, rid):
    response = await env.client.get('/v1/traces/'+rid, headers=env.other_headers)
    assert response.status_code == 200
    return response.json()['data']


async def stream_disconnect_retains_capacity(env, model, record_property):
    arm(env)
    created = await env.client.post('/v1/llm/stream', json=payload(model))
    assert created.status_code == 202
    rid = created.json()['data']['request_id']
    url = f'/v1/llm/stream/{rid}/events'
    first = None
    try:
        async with env.client.stream('GET', url) as response:
            assert response.status_code == 200
            async for event in frames(response):
                if event[1] == 'response.delta':
                    first = event
                    break
        assert first is not None
        assert await held_request(env) == rid
        await counts(env, {'active_requests':1, 'agents':{'a':1}})
        denied = rejected(await env.client.post('/v1/llm', json=payload(model)))
        await no_attempt(env, denied)
        # Re-subscribe while still full: reading the existing first frame is allowed.
        async with env.client.stream('GET', url) as response:
            assert response.status_code == 200
            async for event in frames(response):
                if event[1] == 'response.delta':
                    assert event == first
                    break
        await counts(env, {'active_requests':1, 'agents':{'a':1}})
        release(env, rid)
        resumed = []
        async with env.client.stream('GET', url, params={'after_seq':first[0]}) as response:
            assert response.status_code == 200
            async for event in frames(response):
                if event[1] != 'response.heartbeat': resumed.append(event)
        assert resumed[-1][1] == 'response.completed'
        assert [e[0] for e in resumed] == list(range(first[0]+1, resumed[-1][0]+1))
        answer = first[2]['delta']+''.join(e[2]['delta'] for e in resumed if e[1]=='response.delta')
        upstream, _ = await env.upstream_answer(rid, model, 'streaming')
        assert answer == upstream and 'A123' in answer and '充电器' in answer
        trace = await committed(env, rid)
        env.assert_provider_contract(trace)
        checked = await env.verify(rid, expected_agent='a')
        assert checked['attempts'] == 1
        assert provider_records(env, rid) == ['stream_entered','stream_closed']
        await counts(env, {'active_requests':0, 'agents':{}})
        assert (await env.client.get(url, params={'after_seq':resumed[-1][0]})).status_code == 204
        await env.assert_balances()
        evidence(record_property, backend=env.backend, model=model, request_id=rid,
                 rejected_request_id=denied, observation=checked)
    finally:
        release(env, rid)
