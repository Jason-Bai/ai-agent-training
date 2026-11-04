"""Public Gateway acceptance steps shared by explicit Mock and real backends."""
import asyncio
from decimal import Decimal
import json

from f10_support import frames, payload


def evidence(record_property, **values):
    for name, value in values.items():
        record_property(name, json.dumps(value, ensure_ascii=False) if isinstance(value, (dict,list)) else value)


async def committed(environment, rid):
    async with asyncio.timeout(12):
        while True:
            response = await environment.client.get('/v1/traces/'+rid)
            assert response.status_code == 200
            if response.json()['data']['status'] in {'completed','failed','canceled'}:
                return response.json()['data']
            await asyncio.sleep(.02)


async def request_state(environment, rid):
    def read():
        with environment.pg.connect(environment.name) as sql:
            row = sql.execute('SELECT id,status FROM tb_llm_request WHERE request_id=%s',(rid,)).fetchone()
            attempts = sql.execute('SELECT count(*) FROM tb_llm_attempt WHERE request_pk=%s',(row[0],)).fetchone()[0]
            bills = sql.execute('SELECT count(*) FROM tb_billing_attempt WHERE request_pk=%s',(row[0],)).fetchone()[0]
            events = sql.execute('SELECT count(*) FROM tb_llm_stream_event WHERE request_pk=%s',(row[0],)).fetchone()[0]
        return dict(status=row[1], attempts=attempts, bills=bills, events=events)
    return await asyncio.to_thread(read)


async def agent_success_and_cost(env, model, mode, record_property):
    initial = await env.account_totals()
    assert Decimal(initial['deposited']) == Decimal('10000')
    result = await env.call(model,mode)
    assert result['status'] == 'completed', result['error_code']
    trace = await committed(env,result['request_id'])
    assert trace['requested_model'] == model
    assert trace['usage']['unknown_attempts'] == []
    verified = await env.verify(result['request_id'])
    assert verified['attempts'] == 1
    env.assert_provider_contract(trace)
    if mode == 'streaming':
        assert result['first_text_ms'] is not None
        deltas = [e[2]['delta'] for e in result['events'] if e[1]=='response.delta']
        assert deltas and 'A123' in ''.join(deltas) and '充电器' in ''.join(deltas)
    await env.assert_balances()
    evidence(record_property,model=model,mode=mode,request_id=result['request_id'],
             observation=verified,latency_ms=result['elapsed_ms'],first_text_ms=result['first_text_ms'])


async def disconnect_resume_keeps_one_generation(env,model,record_property):
    response = await env.client.post('/v1/llm/stream',json=payload(model,'streaming'))
    assert response.status_code==202
    rid = response.json()['data']['request_id']
    initial = await request_state(env,rid)
    assert initial == {'status':'pending','attempts':0,'bills':0,'events':0}
    initial_routing = (await env.client.get('/v1/traces/'+rid)).json()['data']['routing']
    url = f'/v1/llm/stream/{rid}/events'
    first = None
    async with env.client.stream('GET',url) as response:
        assert response.status_code==200
        async for event in frames(response):
            if event[1]=='response.delta':
                first=event
                break
    assert first is not None
    resumed=[]
    async with env.client.stream('GET',url,params={'after_seq':first[0]}) as response:
        assert response.status_code==200
        async for event in frames(response):
            if event[1]!='response.heartbeat': resumed.append(event)
    assert resumed and resumed[-1][1]=='response.completed'
    assert [event[0] for event in resumed]==list(range(first[0]+1,resumed[-1][0]+1))
    text=first[2]['delta']+''.join(event[2]['delta'] for event in resumed if event[1]=='response.delta')
    assert 'A123' in text and '充电器' in text
    final_trace = await committed(env,rid)
    assert final_trace['routing'] == initial_routing
    env.assert_provider_contract(final_trace)
    detail=(await env.client.get('/v1/billing/requests/'+rid)).json()['data']
    assert len(detail['attempts'])==1
    assert (await env.client.get(url,params={'after_seq':resumed[-1][0]})).status_code==204
    replay=(await env.client.get(url,params={'after_seq':first[0]})).text
    assert 'response.completed' in replay
    assert (await env.client.get('/v1/billing/requests/'+rid)).json()['data']==detail
    checked=await env.verify(rid)
    assert checked['attempts']==1
    await env.assert_balances()
    evidence(record_property,model=model,request_id=rid,first_seq=first[0],last_seq=resumed[-1][0],observation=checked)


async def cancel_until_terminal(env,rid):
    async with asyncio.timeout(12):
        while True:
            response=await env.client.post(f'/v1/llm/stream/{rid}/cancel')
            assert response.status_code in {200,202}
            status=response.json()['data']['status']
            if response.status_code==200:
                assert status in {'completed','failed','canceled'}
                return status
            assert status=='canceling'
            await asyncio.sleep(.02)


async def cancel_pending_and_in_progress(env,model,record_property):
    pending=(await env.client.post('/v1/llm/stream',json=payload(model,'streaming'))).json()['data']['request_id']
    assert await cancel_until_terminal(env,pending)=='canceled'
    assert (await request_state(env,pending))['attempts']==0
    await env.verify(pending)
    rid=(await env.client.post('/v1/llm/stream',json=payload(model,'streaming'))).json()['data']['request_id']
    received=[]
    async with env.client.stream('GET',f'/v1/llm/stream/{rid}/events') as response:
        assert response.status_code==200
        iterator=frames(response)
        async with asyncio.timeout(35):
            async for event in iterator:
                if event[1]!='response.heartbeat': received.append(event)
                if event[1]=='response.delta': break
            assert received and received[-1][1]=='response.delta'
            terminal=await cancel_until_terminal(env,rid)
            async for event in iterator:
                if event[1]!='response.heartbeat': received.append(event)
    assert received[-1][1]=='response.'+terminal
    before=(await env.client.get('/v1/billing/requests/'+rid)).json()['data']
    assert await cancel_until_terminal(env,rid)==terminal
    assert (await env.client.get('/v1/billing/requests/'+rid)).json()['data']==before
    checked=await env.verify(rid)
    await env.assert_balances()
    evidence(record_property,model=model,request_id=rid,pending_id=pending,terminal=terminal,observation=checked)


