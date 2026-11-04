"""Business rejection and final financial evidence are independent facts."""
import json
from decimal import Decimal

import pytest

from f6_cases import case, server
from f7_cases import body
from test_f9_paid_flow import account, primary_wire, backup_wire


@pytest.mark.parametrize('terminal',[False,True])
@pytest.mark.parametrize('cache',[False,True])
async def test_finality_not_business_content_or_cache_controls_known_usage(case,primary_wire,backup_wire,
                                                                        terminal,cache):
    usage={'prompt_tokens':10,'completion_tokens':1,'total_tokens':11}
    if cache:
        usage.update(prompt_cache_hit_tokens=7,prompt_cache_miss_tokens=3)
    chunk={'object':'chat.completion.chunk','model':'deepseek-flash','usage':usage,
           'choices':[{'index':0,'delta':{'content':[]},'finish_reason':'stop' if terminal else None}]}
    primary_wire.raw=('data: '+json.dumps(chunk)+'\n\n').replace('\\n','\n').encode()
    async with account(case[1],primary_wire,backup_wire,('20','30','50')) as (_,client):
        rid=(await client.post('/v1/llm/stream',json=body(max_output_tokens=100))).headers['x-request-id']
        reply=await client.get(f'/v1/llm/stream/{rid}/events')
        assert 'provider_invalid_response' in reply.text
        bill=(await client.get('/v1/billing/requests/'+rid)).json()['data']
        attempt,=bill['attempts']
        trace=(await client.get('/v1/traces/'+rid)).json()['data']
        assert trace['attempts'][0]['usage']['total_tokens']==attempt['total_tokens']==11
        summary=(await client.get('/v1/billing/summary')).json()['data']
        real=summary['usage']['real']
        assert real['attempts']==1 and real['unknown_attempts']==(0 if terminal else 1)
        assert real['known_subtotal']==({'input_tokens':10,'output_tokens':1,'total_tokens':11} if terminal else None)
        assert attempt['state']==('settled' if terminal and cache else 'pending')
        assert attempt['usage_complete']==terminal
        if terminal and cache:
            assert Decimal(attempt['agent_charge'])==Decimal(attempt['supplier_cost'])==Decimal('.000280')
            assert Decimal(summary['available'])==Decimal('.999720')
            assert Decimal(bill['reserved'])==0
        else:
            assert attempt['agent_charge'] is attempt['supplier_cost'] is None
            assert Decimal(bill['reserved'])>0
