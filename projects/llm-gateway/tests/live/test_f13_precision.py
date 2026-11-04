"""A02: decimal values survive public HTTP, vendor Schema and parsed output."""
from decimal import Decimal
import json

import pytest

from f13_real_support import MODELS, f13_live_pg, f13_real_environment, save_evidence
from utils.json_codec import dumps, loads

pytestmark=pytest.mark.live


@pytest.mark.parametrize('group',list(MODELS))
@pytest.mark.parametrize('mode',['precise_number','precise_string','ordinary_number'])
def test_real_bailian_schema_preserves_precise_decimal(f13_real_environment,group,mode,record_property):
    env=f13_real_environment;value=Decimal('1.0000000000000001')
    if mode=='precise_string':expected=str(value)
    elif mode=='ordinary_number':expected=Decimal('1.25')
    else:expected=value
    amount_schema={'type':'string' if mode=='precise_string' else 'number','enum':[expected]}
    body={'model':group,'prompt':{'name':'context_answer','version':'1.2.0',
        'variables':{'context':f'订单A123的已确认金额是{expected}元，必须逐位保留。'}},
        'messages':[{'role':'user','content':'只返回JSON，字段order_id和amount，金额不得舍入。'}],
        'max_output_tokens':256,'timeout_seconds':60,
        'response_schema':{'type':'object','properties':{
            'order_id':{'type':'string','enum':['A123']},
            'amount':amount_schema,
            'items':{'type':'array','items':{'type':'object','properties':{
                'state':{'type':'string','enum':['verified']}},'required':['state'],
                'additionalProperties':False},'minItems':1,'maxItems':1}},
            'required':['order_id','amount','items'],'additionalProperties':False}}
    reply=env.client.post('/v1/llm',content=dumps(body),headers={'Content-Type':'application/json'})
    rid=reply.headers['x-request-id'];trace=env.trace(rid)
    record_property('f13_request_id',rid);record_property('f13_trace',json.dumps(trace,ensure_ascii=False))
    observation=json.loads((env.directory/('availability-'+rid+'.json')).read_text())
    raw_amounts=[loads(a['typed_text'])['amount'] for a in observation['attempts']]
    rounded=mode=='precise_number' and raw_amounts[-1]!=expected
    if rounded:
        # Accepted supplier limitation: never weaken the original Schema to
        # accept an altered value. This is failure correctness, not a happy path.
        assert reply.status_code==502,reply.text
        assert reply.json()['code']=='schema_validation_failed'
        assert trace['status']=='failed'
        assert all(amount!=expected for amount in raw_amounts)
    else:
        assert reply.status_code==200,reply.text
        parsed=loads(reply.text)['data']['parsed']
        assert parsed['order_id']=='A123' and parsed['amount']==expected
        assert type(parsed['amount']) is (str if mode=='precise_string' else Decimal)
        assert parsed['items']==[{'state':'verified'}]
        assert trace['status']=='completed'
    billing=env.verify(rid,trace)
    save_evidence(env,rid,trace,billing,dict(group=group,mode=mode,
        supplier_rounding_observed=rounded,response=reply.text))
