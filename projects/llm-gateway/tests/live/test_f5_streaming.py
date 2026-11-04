"""Two explicit real SSE paths; default collection never opens credentials."""
import asyncio
import json
from time import monotonic

import pytest

from test_real_providers import live_server  # Reuse isolated real-registry/HTTP fixture.


pytestmark = pytest.mark.live


@pytest.mark.parametrize('model,adapter,protocol',[
    pytest.param('general-primary','deepseek','chat_completions',id='F5-L01-primary-stream'),
    pytest.param('general-backup','openai_compatible','responses',id='F5-L02-backup-stream'),
])
async def test_real_streaming(live_server,model,adapter,protocol,record_property):
    app,client=live_server
    payload={
        'model':model,
        'prompt':{'name':'context_answer','version':'1.1.0',
                  'variables':{'context':'订单 A123 的商品为充电器。'}},
        'messages':[{'role':'user','content':'订单 A123 的商品是什么？请简短回答。'}],
        'max_output_tokens':128,'timeout_seconds':30,
    }
    names=[];texts=[];frame=[];sequences=[];usage=None;first_delta_at=None;started=monotonic()
    created=await client.post('/v1/llm/stream',json=payload)
    assert created.status_code==202
    request_id=created.json()['data']['request_id']
    assert created.json()['data']['status']=='pending'
    assert created.headers['x-request-id']==request_id
    async with client.stream('GET',f'/v1/llm/stream/{request_id}/events') as response:
        assert response.status_code==200
        assert response.headers['content-type'].startswith('text/event-stream')
        assert response.headers['x-request-id']==request_id
        async for line in response.aiter_lines():
            if line:
                frame.append(line)
                continue
            if not frame:
                continue
            event_line=next(line for line in frame if line.startswith('event: '))
            data_line=next(line for line in frame if line.startswith('data: '))
            ids=[line[4:] for line in frame if line.startswith('id: ')]
            name=event_line[7:];data=json.loads(data_line[6:]);frame=[]
            if name=='response.heartbeat':
                assert ids==[]
            else:
                assert len(ids)==1
                sequences.append(int(ids[0]))
            assert data['request_id']==request_id
            assert not names or names[-1] not in {'response.completed','response.failed'}
            names.append(name)
            if name=='response.delta':
                assert set(data)=={'request_id','delta'}
                assert isinstance(data['delta'],str) and data['delta']!=''
                texts.append(data['delta'])
                if first_delta_at is None:first_delta_at=monotonic()
            elif name=='response.heartbeat':
                assert set(data)=={'request_id'}
            elif name=='response.completed':
                assert set(data)=={'request_id','usage'}
                usage=data['usage']
            else:
                # Preserve the platform classification without printing model output.
                record_property('failure_code',data.get('code'))
                pytest.fail('真实流未正常完成；失败分类见JUnit元信息')
    assert not frame
    assert sequences==list(range(1,len(sequences)+1))
    assert names and names[-1]=='response.completed' and names.count('response.completed')==1
    assert any(text.strip() for text in texts)
    assert first_delta_at is not None and usage is not None
    trace=app.state.traces.requests[request_id]
    async with asyncio.timeout(2):
        while trace.status=='running':await asyncio.sleep(.005)
    attempts=[a for a in app.state.traces.attempts if a.request_id==request_id]
    assert trace.operation=='streaming' and trace.status=='completed' and trace.attempts==len(attempts)==1
    attempt=attempts[0]
    assert attempt.status=='completed' and attempt.error_code is None
    assert (attempt.adapter,attempt.api_protocol)==(adapter,protocol)
    assert attempt.actual_model and attempt.usage is not None
    assert attempt.usage.model_dump()==usage
    assert trace.ttft_ms is not None and trace.ttft_ms>=0
    assert trace.generation_duration_ms is not None and trace.generation_duration_ms>=0
    # Generation may finish before the first committed page reaches a subscriber.
    assert trace.stream_duration_ms is not None and trace.stream_duration_ms>=0
    assert trace.counter_version and trace.prompt_fingerprint and trace.input_token_count>0
    delta=attempt.usage.input_tokens-trace.input_token_count
    for name,value in {
        'request_id':request_id,'adapter':adapter,'api_protocol':protocol,'actual_model':attempt.actual_model,
        'upstream_status':attempt.upstream_status,'upstream_finish_reason':attempt.upstream_finish_reason,
        'prompt_fingerprint':trace.prompt_fingerprint,'counter_version':trace.counter_version,
        'max_output_tokens':trace.max_output_tokens,'timeout_seconds':trace.timeout_seconds,
        'local_input_tokens':trace.input_token_count,'input_delta':delta,
        'input_tokens':usage['input_tokens'],'output_tokens':usage['output_tokens'],'total_tokens':usage['total_tokens'],
        'ttft_ms':trace.ttft_ms,'generation_duration_ms':trace.generation_duration_ms,
        'stream_duration_ms':trace.stream_duration_ms,'client_first_delta_ms':(first_delta_at-started)*1000,
        'delta_event_count':names.count('response.delta'),'attempts':len(attempts),'status':trace.status,
    }.items():record_property(name,value)
    assert (trace.max_output_tokens,trace.timeout_seconds)==(128,30)
    assert delta==0
