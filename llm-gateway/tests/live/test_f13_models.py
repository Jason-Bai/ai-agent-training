"""F13 phase A genuine three-model calls; default collection performs no calls."""
import json

import pytest

from constants import MAX_USER_MESSAGE_CHARS
from f13_real_support import MODELS, f13_live_pg, f13_real_environment, save_evidence

pytestmark=pytest.mark.live


def payload(group,structured=False):
    value=dict(model=group,prompt={'name':'context_answer','version':'1.2.0',
        'variables':{'context':'已确认订单A123，商品为充电器。'}},
        messages=[{'role':'user','content':'只输出订单号和商品名称，简短回答。'}],
        max_output_tokens=128,timeout_seconds=60)
    if structured:
        value['response_schema']={'type':'object','properties':{
            'order_id':{'type':'string','enum':['A123']},
            'product':{'type':'string','enum':['充电器']}},
            'required':['order_id','product'],'additionalProperties':False}
    return value


def sse_events(text):
    events=[]
    for frame in text.split('\n\n'):
        fields=dict(line.split(': ',1) for line in frame.splitlines() if ': ' in line)
        if fields.get('event') and fields['event']!='response.heartbeat':
            events.append((fields['event'],int(fields['id']),json.loads(fields['data'])))
    return events


@pytest.mark.parametrize('group',list(MODELS))
@pytest.mark.parametrize('mode',['text','schema','streaming','health'])
def test_three_real_bailian_models_public_operations(f13_real_environment,group,mode,record_property):
    env=f13_real_environment;client=env.client
    if mode=='health':reply=client.post('/v1/models/'+group+'/health')
    else:
        body=payload(group,structured=mode=='schema')
        if mode=='streaming':
            body.pop('response_schema',None)
            reply=client.post('/v1/llm/stream',json=body)
            assert reply.status_code==202,reply.text
            rid=reply.headers['x-request-id'];pending=env.trace(rid)
            assert pending['status']=='pending' and pending['attempts']==[]
            reply=client.get('/v1/llm/stream/'+rid+'/events')
        else:reply=client.post('/v1/llm',json=body)
    if mode!='streaming':rid=reply.headers['x-request-id']
    trace=env.trace(rid)
    record_property('f13_request_id',rid);record_property('f13_trace',json.dumps(trace,ensure_ascii=False))
    assert reply.status_code==200,reply.text
    if mode=='streaming':
        events=sse_events(reply.text)
        assert [seq for _,seq,_ in events]==list(range(1,len(events)+1))
        assert events[-1][0]=='response.completed'
        assert sum(event in {'response.completed','response.failed','response.canceled'} for event,_,_ in events)==1
        text=''.join(value['delta'] for event,_,value in events if event=='response.delta')
        assert 'A123' in text and '充电器' in text
        assert client.get('/v1/llm/stream/'+rid+'/events',params={'after_seq':events[-1][1]}).status_code==204
    elif mode=='schema':assert reply.json()['data']['parsed']=={'order_id':'A123','product':'充电器'}
    elif mode=='text':assert 'A123' in reply.json()['data']['text'] and '充电器' in reply.json()['data']['text']
    else:assert reply.json()['data']['latency_ms']>=0
    assert trace['status']=='completed' and trace['input_token_kind']=='estimated'
    assert trace['counter_version'].startswith('qwen_text_estimate_v1:')
    assert trace['requested_model']==group
    assert trace['attempts'] and all(a['adapter']=='bailian' and a['api_protocol']=='responses'
        and a['configured_model']==MODELS[group] and a['actual_model']==MODELS[group]
        and a['input_token_kind']=='estimated' for a in trace['attempts'])
    billing=env.verify(rid,trace)
    save_evidence(env,rid,trace,billing,dict(group=group,mode=mode))


CALIBRATION_CASES=['chinese','english','mixed','code','unicode','near_user_limit','few_shot','large_schema']


def calibration_payload(group,case):
    suffix='\n以上是校准资料，请只回复OK，不解释。'
    values={
        'chinese':'这是一段用于检查中文分词与完整消息封装的订单资料。'*20,
        'english':'Order A123 contains a charger. This input calibrates token estimation. '*20,
        'mixed':'订单A123 / charger / status=delivered；这是混合语种校准。'*20,
        'code':'def order_id(value):\n    return value.get("order_id", "A123")\n'*20,
        'unicode':'🙂🚀咖啡e\u0301 Ελληνικά العربية 日本語 한국어 '*20,
        'near_user_limit':('中文资料'*MAX_USER_MESSAGE_CHARS)[:MAX_USER_MESSAGE_CHARS-len(suffix)],
    }
    value=dict(model=group,prompt={'name':'basic_text','version':'1.0.0'},
        messages=[{'role':'user','content':values.get(case,'依据资料输出校准标记。')+suffix}],
        max_output_tokens=256,timeout_seconds=120)
    if case in {'few_shot','large_schema'}:
        value['prompt']={'name':'context_answer','version':'1.2.0' if case=='large_schema' else '1.1.0',
            'variables':{'context':'校准资料约定：校准标记为OK。'}}
    if case=='few_shot':
        value['messages']=[{'role':'user','content':'根据所提供资料，约定的校准标记是什么？'}]
    if case=='large_schema':
        value['response_schema']={'type':'object','properties':{'marker':{
            'type':'string','enum':['OK'],'description':'校准标记说明。'*500}},
            'required':['marker'],'additionalProperties':False}
    return value


@pytest.mark.parametrize('group',list(MODELS))
@pytest.mark.parametrize('case',CALIBRATION_CASES)
def test_real_qwen_estimate_covers_reported_input(f13_real_environment,group,case,record_property):
    env=f13_real_environment;body=calibration_payload(group,case)
    reply=env.client.post('/v1/llm',json=body);rid=reply.headers['x-request-id'];trace=env.trace(rid)
    record_property('f13_request_id',rid);record_property('f13_trace',json.dumps(trace,ensure_ascii=False))
    assert reply.status_code==200,reply.text
    assert trace['status']=='completed' and trace['input_token_kind']=='estimated'
    calibration=[]
    for attempt in trace['attempts']:
        assert attempt['usage'] is not None
        estimated=attempt['input_token_count'];actual=attempt['usage']['input_tokens']
        assert estimated>=actual,(group,case,estimated,actual)
        calibration.append(dict(estimate=estimated,actual=actual,difference=estimated-actual,
            ratio=estimated/actual if actual else None,counter_version=attempt['counter_version']))
    if case=='near_user_limit':assert len(body['messages'][0]['content'])==MAX_USER_MESSAGE_CHARS
    if case=='large_schema':assert reply.json()['data']['parsed']=={'marker':'OK'}
    billing=env.verify(rid,trace)
    record_property('f13_calibration',json.dumps(calibration))
    save_evidence(env,rid,trace,billing,dict(group=group,case=case,calibration=calibration))
