"""Real wrappers retain the original F10 nodes and parameterization."""
import asyncio
from decimal import Decimal

import pytest

from f10_support import f10_environment, payload
from bug002_scenarios import (
    agent_success_and_cost, disconnect_resume_keeps_one_generation,
    cancel_pending_and_in_progress, cancel_until_terminal, evidence, request_state,
)

pytestmark = pytest.mark.live
MODELS = ['general-primary', 'general-backup']


@pytest.mark.parametrize('model', MODELS)
@pytest.mark.parametrize('mode', ['text','structured','streaming'])
async def test_real_agent_success_and_cost(f10_environment, model, mode, record_property):
    await agent_success_and_cost(f10_environment, model, mode, record_property)


@pytest.mark.parametrize('model', MODELS)
async def test_real_disconnect_resume_keeps_one_generation(f10_environment,model,record_property):
    await disconnect_resume_keeps_one_generation(f10_environment,model,record_property)


@pytest.mark.parametrize('model',MODELS)
async def test_real_cancel_pending_and_in_progress(f10_environment,model,record_property):
    await cancel_pending_and_in_progress(f10_environment,model,record_property)


async def test_real_cli_rotation_ownership_restart_and_budget(f10_environment,record_property):
    env=f10_environment
    def counts():
        with env.pg.connect(env.name) as sql:
            return tuple(sql.execute('SELECT count(*) FROM '+table).fetchone()[0]
                         for table in ('tb_llm_request','tb_llm_attempt','tb_billing_attempt'))
    initial_counts=await asyncio.to_thread(counts)
    for authorization in (None,'Bearer invalid-test-token'):
        request=env.client.build_request('POST','/v1/llm',json=payload())
        request.headers.pop('Authorization',None)
        if authorization is not None: request.headers['Authorization']=authorization
        rejected=await env.client.send(request)
        assert rejected.status_code==401 and rejected.json()['data'] is None
    assert await asyncio.to_thread(counts)==initial_counts
    created=await env.client.post('/v1/llm/stream',json=payload())
    rid=created.json()['data']['request_id']
    original=await request_state(env,rid)
    for method,path in [('get',f'/v1/traces/{rid}'),('get',f'/v1/billing/requests/{rid}'),
                        ('get',f'/v1/llm/stream/{rid}/events'),('post',f'/v1/llm/stream/{rid}/cancel'),
                        ('get',f'/v1/llm/stream/{rid}/events%0A'),('post',f'/v1/llm/stream/{rid}/cancel%0A')]:
        assert (await getattr(env.client,method)(path,headers=env.other_headers)).status_code==404
    assert await request_state(env,rid)==original
    old=env.headers['Authorization']
    public=old.split(' ',1)[1].split('.',1)[0]
    new=await env.cli('rotate','a',public)
    assert (await env.client.get('/v1/traces/'+rid,headers={'Authorization':old})).status_code==401
    env.headers={'Authorization':'Bearer '+new}
    env.client.headers.update(env.headers)
    assert (await env.client.get('/v1/traces/'+rid)).status_code==200
    await env.restart()
    assert (await request_state(env,rid))==original
    assert (await env.client.get('/v1/traces/'+rid)).status_code==200
    await cancel_until_terminal(env,rid)
    await env.verify(rid)
    await env.cli('create-agent','poor','zero budget')
    poor=await env.cli('issue','poor')
    headers={'Authorization':'Bearer '+poor}
    rejected=[]
    for path in ['/v1/llm','/v1/models/general-primary/health']:
        response=await env.client.post(path,json=payload() if path=='/v1/llm' else None,headers=headers)
        assert response.status_code==402 and response.json()['code']=='insufficient_budget'
        rejected.append(response.headers['x-request-id'])
    pending=(await env.client.post('/v1/llm/stream',json=payload(),headers=headers)).json()['data']['request_id']
    response=await env.client.get(f'/v1/llm/stream/{pending}/events',headers=headers)
    assert 'insufficient_budget' in response.text
    rejected.append(pending)
    for rejected_id in rejected:
        assert (await request_state(env,rejected_id))['attempts']==0
    totals=await env.account_totals(headers)
    assert Decimal(totals['charged'])==Decimal(totals['reserved'])==0
    await env.assert_balances()
    evidence(record_property,request_id=rid,zero_budget_requests=rejected,rotation_verified=True,restart_verified=True)
