from pricing_support import write_pricing
"""F12 T10/T22/T24: distinct tariffs and actual Chat/Responses wire requests."""
from dataclasses import replace
from decimal import Decimal

from fastapi.testclient import TestClient
import pytest
import yaml

from f12_cases import request, FixedCounter
from legacy_identity import HEADERS
from main import create_app
from services.models import ProviderConfig, RecoveryPolicy
from services.token_budget import TokenBudget
from settings import Settings
from protocol.test_providers import server as chat_wire, server as response_wire, SUCCESS
from test_f12_stream_plan import f12_pg, f12_case, rows


def paid_app(case,chat,response,*,order=('chat-a','responses-b')):
    _,config,baseline=case
    endpoints=baseline.endpoints
    group=baseline.get_group('general-primary').model_copy(update={'candidates':order})
    definitions={
        'mock-main':ProviderConfig(adapter='deepseek',api_protocol='chat_completions',
            base_url=chat.url,credential_ref='f12-local-key'),
        'mock-backup':ProviderConfig(adapter='openai_compatible',api_protocol='responses',
            base_url=response.url,credential_ref='f12-local-key')}
    definition=replace(baseline,providers=definitions,model_groups={'general-primary':group},
        recovery=RecoveryPolicy(max_attempts=2,max_attempts_per_endpoint=1,
                                base_delay_seconds=0,max_delay_seconds=0))
    (config/'credentials.yaml').write_text('credentials: {f12-local-key: {api_key: FAKE-F12-SECRET}}')
    tariffs=[]
    for supplier,input_rate,output_rate in [('chat-supplier','1','2'),('responses-supplier','10','20')]:
        prices={'hit':input_rate,'miss':input_rate,'output':output_rate}
        tariffs.append(dict(supplier=supplier,model='deepseek-flash',version='f12-fixture',
            effective_from='2020-01-01T00:00:00Z',peak=prices,off_peak=prices))
    write_pricing(config, {'bindings':{'mock-main':'chat-supplier',
        'mock-backup':'responses-supplier'},'versions':tariffs})
    return create_app(settings=Settings(env='test',_env_file=None),config_dir=config,
        model_registry=definition,token_budget=TokenBudget({'deepseek_v41':FixedCounter(250,'f12-wire-v1')}))


def structured_wire(wire):
    wire.body=SUCCESS | {'choices':[{'index':0,'message':{'role':'assistant','content':'{"order_id":"A123"}'},
                                  'finish_reason':'stop'}],
                        'usage':{'prompt_tokens':200,'completion_tokens':20,'total_tokens':220}}


@pytest.mark.parametrize('selected',['chat-a','responses-b'])
def test_real_adapter_wire_uses_selected_endpoint_contract_and_tariff(f12_case,f12_pg,chat_wire,response_wire,selected):
    name,_,_=f12_case;structured_wire(chat_wire);structured_wire(response_wire)
    order=(selected,'responses-b' if selected=='chat-a' else 'chat-a')
    with TestClient(paid_app(f12_case,chat_wire,response_wire,order=order),headers=HEADERS) as client:
        reply=client.post('/v1/llm',json=request(structured=True,max_output_tokens=100).model_dump(exclude_none=True))
        assert reply.status_code==200,reply.text
        assert reply.json()['data']['parsed']=={'order_id':'A123'}
        rid=reply.headers['x-request-id'];wire=chat_wire if selected=='chat-a' else response_wire
        other=response_wire if selected=='chat-a' else chat_wire
        assert len(wire.calls)==1 and not other.calls
        sent=wire.calls[0]
        if selected=='chat-a':
            assert sent['path']=='/chat/completions'
            assert sent['body']['response_format']=={'type':'json_object'}
            assert '"schema":' in sent['body']['messages'][-1]['content']
        else:
            assert sent['path']=='/responses'
            assert sent['body']['text']['format']['type']=='json_schema'
            assert sent['body']['text']['format']['schema']['type']=='object'
        diagnostic=client.get('/v1/traces/'+rid).json()['data']
        assert diagnostic['routing']['selected_endpoint']==selected
        assert diagnostic['attempts'][0]['model']==selected
        detail=client.get('/v1/billing/requests/'+rid).json()['data']
        assert Decimal(detail['attempts'][0]['agent_charge'])==Decimal('.00024' if selected=='chat-a' else '.0024')


def test_fallback_bills_each_actual_tariff_and_keeps_unknown_usage_pending(f12_case,f12_pg,chat_wire,response_wire):
    name,_,_=f12_case;chat_wire.status=503;structured_wire(response_wire)
    with TestClient(paid_app(f12_case,chat_wire,response_wire),headers=HEADERS) as client:
        reply=client.post('/v1/llm',json=request(structured=True,max_output_tokens=100).model_dump(exclude_none=True))
        assert reply.status_code==200,reply.text
        rid=reply.headers['x-request-id']
        assert len(chat_wire.calls)==len(response_wire.calls)==1
        assert '"schema":' in chat_wire.calls[0]['body']['messages'][-1]['content']
        assert '"schema":' not in response_wire.calls[0]['body']['input'][-1]['content']
        data=client.get('/v1/traces/'+rid).json()['data']
        assert data['routing']['selected_endpoint']=='chat-a'
        assert [a['model'] for a in data['attempts']]==['chat-a','responses-b']
        detail=client.get('/v1/billing/requests/'+rid).json()['data']
        first,second=detail['attempts']
        assert first['state']=='pending' and second['state']=='settled'
        assert Decimal(first['reserved_amount'])==Decimal('.00045')
        assert Decimal(second['reserved_amount'])==Decimal('.0045')
        assert Decimal(second['agent_charge'])==Decimal('.0024')
        with f12_pg.connect(name) as sql:
            totals=sql.execute('SELECT state,reserved_amount,agent_charge FROM tb_billing_attempt ORDER BY attempt_no').fetchall()
        assert totals==[('pending',Decimal('.00045'),None),('settled',Decimal('.0045'),Decimal('.0024'))]


def test_trace_read_is_side_effect_free_and_never_projects_body_or_connection_secret(f12_case,f12_pg,chat_wire,response_wire):
    name,_,_=f12_case;structured_wire(chat_wire);structured_wire(response_wire)
    with TestClient(paid_app(f12_case,chat_wire,response_wire),headers=HEADERS) as client:
        response=client.post('/v1/llm',json=request(messages=[{'role':'user','content':'PRIVATE-F12-BODY'}]).model_dump(exclude_none=True))
        assert response.status_code==200,response.text
        rid=response.headers['x-request-id'];before=rows(f12_pg,name,rid)
        for _ in range(2):
            result=client.get('/v1/traces/'+rid)
            assert result.status_code==200
            for secret in ['PRIVATE-F12-BODY','FAKE-F12-SECRET','f12-local-key',chat_wire.url,response_wire.url]:
                assert secret not in result.text
            assert result.json()['data']['routing']['config_fingerprint']
        assert rows(f12_pg,name,rid)==before
        assert len(chat_wire.calls)==1 and not response_wire.calls
        assert 'PRIVATE-F12-BODY' in before[0][3]


def test_historical_trace_does_not_invent_routing_for_pre_f12_metadata(f12_case,f12_pg):
    import json
    from test_f12_stream_plan import app,pending
    name,config,definition=f12_case
    with TestClient(app(config,definition),headers=HEADERS) as client:
        rid=pending(client)
        with f12_pg.connect(name) as sql:
            metrics,=sql.execute('SELECT timing_snapshot FROM tb_llm_request WHERE request_id=%s',(rid,)).fetchone()
            value=json.loads(metrics);value.pop('routing',None)
            sql.execute('UPDATE tb_llm_request SET timing_snapshot=%s,input_snapshot=NULL WHERE request_id=%s',
                        (json.dumps(value),rid))
        result=client.get('/v1/traces/'+rid)
        assert result.status_code==200,result.text
        assert result.json()['data']['routing'] is None
        assert result.json()['data']['attempts']==[]
