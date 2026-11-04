"""Real auto-routing public-chain acceptance; sealed corpus is read only at runtime.

Each quality node runs its entire original corpus once, preserves every result,
then evaluates the fixed quality thresholds. No best-of reruns or classifier bypass.
"""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError
import pytest

from constants import MAX_CONTEXT_VARIABLE_CHARS
from f13_real_support import MODELS, PRICE_VERSION, real_prices, f13_live_pg, f13_real_environment, save_evidence
from services.prompts import PromptRegistry, PromptTemplate, VariableSpec

pytestmark=pytest.mark.live


@contextmanager
def controlled_case(env,case):
    """Inject application-owned role as System, not a consumer role declaration."""
    previous=env.application.state.prompt_registry
    manager=env.application.state.streaming_manager
    previous_manager=manager.prompt_registry
    template=PromptTemplate(name='f13_controlled_case',version='1.0.0',
        system_template=case['task_role'],
        user_template='<context>${context}</context>\n<user_message>${message}</user_message>\n'
                      '<output_contract>${output_contract}</output_contract>',
        variable_specs={'context':VariableSpec(MAX_CONTEXT_VARIABLE_CHARS)},
        supports_structured_output=True,change_note='Synthetic controlled application role for F13 evaluation')
    registry=PromptRegistry([*previous.templates.values(),template])
    env.application.state.prompt_registry=registry
    manager.prompt_registry=registry
    body=dict(model='auto-general',prompt={'name':template.name,'version':template.version,
        'variables':{'context':case['context']}},
        messages=case.get('messages',[{'role':'user','content':case['message']}]),
        max_output_tokens=1024,initial_output_tokens=256,timeout_seconds=60)
    if 'response_schema' in case:body['response_schema']=case['response_schema']
    try:yield body
    finally:
        env.application.state.prompt_registry=previous
        manager.prompt_registry=previous_manager


def events_from(text):
    events=[]
    for frame in text.split('\n\n'):
        fields=dict(line.split(': ',1) for line in frame.splitlines() if ': ' in line)
        if fields.get('event') and fields['event']!='response.heartbeat':
            events.append((fields['event'],int(fields['id']),json.loads(fields['data'])))
    return events


def assert_two_stages(trace):
    assert trace['requested_model']=='auto-general'
    attempts=trace['attempts']
    assert len(attempts)>=2
    assert attempts[0]['purpose']=='classification'
    assert all(attempt['purpose']=='generation' for attempt in attempts[1:])
    assert attempts[0]['model']=='light-endpoint'
    assert attempts[0]['max_output_tokens']==128
    routing=trace['auto_routing']
    assert routing['source']=='classifier' and routing['fallback_reason'] is None
    assert routing['resolved_group'] in MODELS
    assert all(a['model']==routing['resolved_group']+'-endpoint' for a in attempts[1:])
    assert all(a['adapter']=='bailian' and a['usage'] is not None
        and a['configured_model']==MODELS['light' if a['purpose']=='classification' else routing['resolved_group']]
        and a['input_token_kind']=='estimated'
        and a['counter_version'].startswith('qwen_text_estimate_v1:')
        and a['input_token_count']>=a['usage']['input_tokens'] for a in attempts)


@pytest.mark.parametrize('mode',['text','schema','streaming'])
def test_real_auto_routing_public_operations(f13_real_environment,mode,record_property):
    env=f13_real_environment
    case=dict(task_role='你是订单字段提取助手，仅根据明确资料提取订单号和商品，不执行任何操作。',
        context='已确认订单A123，商品为充电器。',message='只输出订单号和商品名称，简短回答。')
    if mode=='schema':
        case['response_schema']={'type':'object','properties':{
            'order_id':{'type':'string','enum':['A123']},'product':{'type':'string','enum':['充电器']}},
            'required':['order_id','product'],'additionalProperties':False}
    with controlled_case(env,case) as body:
        reply=env.client.post('/v1/llm/stream' if mode=='streaming' else '/v1/llm',json=body)
        rid=reply.headers['x-request-id']
        if mode=='streaming':
            assert reply.status_code==202,reply.text
            pending=env.trace(rid);assert pending['status']=='pending' and pending['attempts']==[]
            reply=env.client.get('/v1/llm/stream/'+rid+'/events')
    trace=env.trace(rid)
    record_property('f13_request_id',rid)
    record_property('f13_trace',json.dumps(trace,ensure_ascii=False))
    billing=env.client.get('/v1/billing/requests/'+rid).json()
    save_evidence(env,rid,trace,billing,dict(mode=mode,response=reply.text))
    assert reply.status_code==200,reply.text
    if mode=='streaming':
        events=events_from(reply.text)
        assert [seq for _,seq,_ in events]==list(range(1,len(events)+1))
        assert events[-1][0]=='response.completed'
        assert sum(e in {'response.completed','response.failed','response.canceled'} for e,_,_ in events)==1
        text=''.join(data['delta'] for event,_,data in events if event=='response.delta')
        assert env.client.get('/v1/llm/stream/'+rid+'/events',params={'after_seq':events[-1][1]}).status_code==204
    else:
        data=reply.json()['data'];text=data['text']
        if mode=='schema':assert data['parsed']=={'order_id':'A123','product':'充电器'}
    assert 'A123' in text and '充电器' in text
    assert '"group"' not in text and 'candidate_groups' not in text
    assert trace['status']=='completed'
    assert_two_stages(trace)
    assert trace['structured_output_mode']==('json_schema' if mode=='schema' else 'none')
    generation=next(a for a in trace['attempts'] if a['purpose']=='generation')
    for field in ('prompt_fingerprint','input_token_count','counter_version','input_token_kind'):
        assert trace[field]==generation[field]
    verified=env.verify(rid,trace)
    save_evidence(env,rid,trace,verified,dict(mode=mode,response=reply.text))


def terminal_trace(env,rid):
    deadline=monotonic()+10
    trace=env.trace(rid)
    while trace['status'] not in {'completed','failed','canceled'} and monotonic()<deadline:
        sleep(.1)
        trace=env.trace(rid)
    return trace


def verify_quality_lifecycle(env,rid,trace,reply,billing_reply):
    """Failed generation still owes a terminal state and complete ledger evidence."""
    assert trace['status'] in {'completed','failed'},trace['status']
    assert billing_reply.status_code==200,billing_reply.text
    bill=billing_reply.json()['data'];attempts=trace['attempts']
    assert all(a['status'] in {'completed','failed'} for a in attempts)
    assert len(attempts)==len(bill['attempts'])
    assert all(a['purpose'] in {'classification','generation'} for a in attempts)
    for attempt in attempts:
        group='light' if attempt['purpose']=='classification' else trace['auto_routing']['resolved_group']
        assert attempt['model']==group+'-endpoint' and attempt['configured_model']==MODELS[group]
        assert attempt['input_token_kind']=='estimated'
        assert attempt['counter_version'].startswith('qwen_text_estimate_v1:')
        if attempt['usage'] is not None:
            assert attempt['input_token_count']>=attempt['usage']['input_tokens']
    if reply.status_code==200:
        assert trace['status']=='completed' and trace['error_code'] is None
    else:
        assert trace['status']=='failed' and trace['error_code']==reply.json()['code']
        assert trace['error_code'] in {'output_truncated','provider_timeout','provider_connection_error',
            'provider_rate_limited','provider_unavailable','request_timeout','provider_refused'},trace['error_code']
    if all(a['usage'] is not None for a in attempts):
        env.verify(rid,trace)
        return
    # A transport failure can leave consumption unknown. Verify those holds
    # explicitly, and still independently price every known successful charge.
    with env.server.connect(env.database_name) as sql:
        pk,status=sql.execute('SELECT id,status FROM tb_llm_request WHERE request_id=%s',(rid,)).fetchone()
        rows=sql.execute('SELECT model,status,input_tokens,output_tokens,total_tokens FROM tb_llm_attempt WHERE request_pk=%s ORDER BY attempt_no',(pk,)).fetchall()
        charges=sql.execute('SELECT state,agent_charge,supplier_cost,quote,cached_input_tokens FROM tb_billing_attempt WHERE request_pk=%s ORDER BY attempt_no',(pk,)).fetchall()
    assert status==trace['status'] and len(rows)==len(charges)==len(attempts)
    total=Decimal(0)
    for attempt,row,charge,public in zip(attempts,rows,charges,bill['attempts']):
        assert row[:2]==(attempt['model'],attempt['status'])
        assert public['usage_source']=='real'
        usage=attempt['usage']
        if usage is None:
            assert row[2:]==(None,None,None) and public['usage_complete'] is False
            if attempt['provider_entered'] is False:
                assert charge[:3]==('released',Decimal(0),Decimal(0))
                assert public['state']=='released' and Decimal(public['agent_charge'])==0
            else:
                assert charge[:3]==('pending',None,None)
                assert public['state']=='pending' and public['agent_charge'] is None
                assert public['supplier_cost'] is None
            continue
        assert row[2:]==(usage['input_tokens'],usage['output_tokens'],usage['total_tokens'])
        assert charge[0]==public['state']=='settled' and public['usage_complete'] is True
        assert isinstance(charge[3],dict) and charge[3]['version']==PRICE_VERSION
        cached=charge[4];assert type(cached) is int and 0<=cached<=usage['input_tokens']
        price=next(v for v in real_prices()['versions'] if v['model']==attempt['configured_model'])
        rate=price['peak'] if not price.get('input_tiers') else next(
            t for t in price['input_tiers'] if usage['input_tokens']<=t['max_input_tokens'])['peak']
        expected=(cached*Decimal(rate['hit'])+(usage['input_tokens']-cached)*Decimal(rate['miss'])
                  +usage['output_tokens']*Decimal(rate['output']))/Decimal(1000000)
        assert charge[1]==charge[2]==Decimal(public['agent_charge'])==Decimal(public['supplier_cost'])==expected
        total+=expected
    assert Decimal(bill['charged'])==total
    counts=env.application.state.concurrency_limiter.snapshot()
    assert counts['active_requests']==0 and not counts['agents']


def run_quality(env,split,record_property):
    # Never read either corpus at import/collection. In particular, acceptance
    # remains sealed until the maintainer explicitly runs this pytest node.
    path=Path(__file__).resolve().parents[1]/'fixtures'/('f13_routing_'+split+'.json')
    raw=path.read_bytes();corpus=json.loads(raw);cases=corpus['cases']
    assert len(cases)==30 and Counter(c['expected_group'] for c in cases)==dict.fromkeys(MODELS,10)
    batch=uuid4().hex
    report=dict(batch_id=batch,split=split,corpus_sha256=sha256(raw).hexdigest(),
        corpus_version=corpus['corpus_version'],started_at=datetime.now(timezone.utc).isoformat(),
        cases=[],confusion_matrix={g:dict.fromkeys(MODELS,0) for g in MODELS},
        misclassified=[],fallbacks=[],classification_failures=[],provider_failures=[],
        generation_failures=[],end_to_end_failures=[],transport_unknown=[],integrity_failures=[])
    report_path=env.directory/('f13-routing-'+split+'-'+batch+'.json')
    for case in cases:
        # Only role/context/messages/Schema enter the task. Golden labels and
        # rationale are read below exclusively to score the recorded result.
        with controlled_case(env,case) as body:
            reply=env.client.post('/v1/llm',json=body)
        rid=reply.headers['x-request-id'];trace=terminal_trace(env,rid)
        billing_reply=env.client.get('/v1/billing/requests/'+rid);bill=billing_reply.json()
        routing=trace.get('auto_routing') or {}
        resolved=routing.get('resolved_group');source=routing.get('source')
        classified=[a for a in trace['attempts'] if a['purpose']=='classification']
        valid=(source=='classifier' and resolved in MODELS and len(classified)==1
               and classified[0]['status']=='completed')
        result=dict(id=case['id'],request_id=rid,expected_group=case['expected_group'],
            resolved_group=resolved,source=source,fallback_reason=routing.get('fallback_reason'),
            valid_classification=valid,http_status=reply.status_code,status=trace['status'],
            response=reply.json(),trace=trace,billing=bill)
        report['cases'].append(result)
        if source!='classifier' or resolved not in MODELS:report['fallbacks'].append(case['id'])
        if not valid:report['classification_failures'].append(case['id'])
        else:
            report['confusion_matrix'][case['expected_group']][resolved]+=1
            if resolved!=case['expected_group']:report['misclassified'].append(case['id'])
        if reply.status_code!=200:
            failure=dict(id=case['id'],code=reply.json().get('code'),error_code=trace.get('error_code'))
            report['end_to_end_failures'].append(failure)
            if str(failure['code']).startswith('provider_'):report['provider_failures'].append(failure)
        for attempt in trace['attempts']:
            if attempt['purpose']=='generation' and attempt['status']=='failed':
                report['generation_failures'].append(dict(id=case['id'],attempt_no=attempt['attempt_no'],
                    error_code=attempt['error_code']))
            if attempt['usage'] is None and attempt['provider_entered'] is not False:
                report['transport_unknown'].append(dict(id=case['id'],attempt_no=attempt['attempt_no'],
                    purpose=attempt['purpose'],error_code=attempt['error_code']))
        try:
            verify_quality_lifecycle(env,rid,trace,reply,billing_reply)
            if valid:
                assert classified[0]['model']=='light-endpoint'
                assert classified[0]['max_output_tokens']==128
                assert all(a['model']==resolved+'-endpoint' for a in trace['attempts']
                           if a['purpose']=='generation')
            if reply.status_code==200 and 'response_schema' in case:
                Draft202012Validator(case['response_schema']).validate(reply.json()['data']['parsed'])
        except (AssertionError, ValidationError) as error:
            report['integrity_failures'].append(dict(id=case['id'],error=str(error)))
        save_evidence(env,rid,trace,bill,dict(batch_id=batch,case_id=case['id'],split=split))
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    report['valid']=sum(sum(row.values()) for row in report['confusion_matrix'].values())
    report['correct']=sum(report['confusion_matrix'][g][g] for g in MODELS)
    report['end_to_end']=dict(Counter(case['status'] for case in report['cases']))
    report['ended_at']=datetime.now(timezone.utc).isoformat()
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    record_property('f13_quality_report',str(report_path))
    record_property('f13_quality_summary',json.dumps({k:v for k,v in report.items() if k!='cases'},ensure_ascii=False))
    assert not report['integrity_failures'],report['integrity_failures']
    assert report['valid']==30,report['classification_failures']
    assert report['correct']>=27,report['misclassified']
    assert all(report['confusion_matrix'][g][g]>=8 for g in MODELS),report['confusion_matrix']


def test_real_routing_quality_development_30(f13_real_environment,record_property):
    run_quality(f13_real_environment,'development',record_property)


def test_real_routing_quality_sealed_acceptance_30(f13_real_environment,record_property):
    run_quality(f13_real_environment,'acceptance',record_property)
