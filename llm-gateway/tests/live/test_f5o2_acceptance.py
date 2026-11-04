"""Exactly two explicitly authorized paid calls; no historical live test replay."""
import pytest
from psycopg.rows import dict_row

from test_f5o1_acceptance import accepted_server, live_pg
from utils.json_codec import loads

pytestmark = pytest.mark.live


async def frames(response):
    fields={}
    async for line in response.aiter_lines():
        if line:
            key,value=line.split(': ',1);fields[key]=value
        elif fields:
            yield (int(fields['id']) if 'id' in fields else None,fields['event'],loads(fields['data']))
            fields={}
    assert not fields


@pytest.mark.parametrize('model,adapter,protocol',[
    ('general-primary','deepseek','chat_completions'),
    ('general-backup','openai_compatible','responses'),
])
async def test_real_two_stage_stream_disconnect_and_resume(accepted_server,model,adapter,protocol,record_property):
    app,client,pg,name=accepted_server
    body={'model':model,'prompt':{'name':'basic_text','version':'1.0.0'},
          'messages':[{'role':'user','content':'用一句话解释 TTFT，不超过四十字。'}],
          'max_output_tokens':128,'timeout_seconds':30}
    created=await client.post('/v1/llm/stream',json=body)
    assert created.status_code==202,created.text
    rid=created.json()['data']['request_id'];url=f'/v1/llm/stream/{rid}/events'
    with pg.connect(name,row_factory=dict_row) as conn:
        pending=conn.execute('SELECT * FROM tb_llm_request WHERE request_id=%s',(rid,)).fetchone()
        assert pending['status']=='pending' and pending['started_at'] is None
        assert conn.execute('SELECT count(*) FROM tb_llm_attempt WHERE request_pk=%s',(pending['id'],)).fetchone()['count']==0
    first=None
    async with client.stream('GET',url) as response:
        assert response.status_code==200 and response.headers['x-request-id']==rid
        async for item in frames(response):
            if item[1]=='response.delta':
                first=item;break
    assert first is not None and first[0]>=1
    received=[first]
    async with client.stream('GET',url+f'?after_seq={first[0]}') as response:
        assert response.status_code==200
        async for item in frames(response):
            if item[1]!='response.heartbeat': received.append(item)
    assert received[-1][1]=='response.completed'
    assert ''.join(value['delta'] for _,kind,value in received if kind=='response.delta').strip()
    with pg.connect(name,row_factory=dict_row) as conn:
        row=conn.execute('SELECT * FROM tb_llm_request WHERE request_id=%s',(rid,)).fetchone()
        attempts=conn.execute('SELECT * FROM tb_llm_attempt WHERE request_pk=%s',(row['id'],)).fetchall()
        saved=conn.execute('SELECT * FROM tb_llm_stream_event WHERE request_pk=%s ORDER BY seq',(row['id'],)).fetchall()
    assert row['status']=='completed' and len(attempts)==1 and attempts[0]['status']=='completed'
    attempt=attempts[0]
    assert (attempt['adapter'],attempt['api_protocol'])==(adapter,protocol)
    assert attempt['actual_model'] and attempt['input_tokens'] is not None
    assert attempt['total_tokens']==attempt['input_tokens']+attempt['output_tokens']
    assert [(event['seq'],event['event_type'],loads(event['payload'])) for event in saved]==received
    assert [event['seq'] for event in saved]==list(range(1,len(saved)+1))
    assert (await client.get(url+f'?after_seq={saved[-1]["seq"]}')).status_code==204
    for key,value in {'request_id':rid,'adapter':adapter,'protocol':protocol,'actual_model':attempt['actual_model'],
                      'input_tokens':attempt['input_tokens'],'output_tokens':attempt['output_tokens'],
                      'events':len(saved),'first_seq':first[0]}.items():record_property(key,value)
