from registry_helpers import ModelRegistry
"""Snapshot reuse and O1-compatible history through real PG and public SSE."""
from services.models import ConcurrencyPolicy
from uuid import uuid4

import httpx
import pytest

from test_f5o2_http import server, case, make_app, body, observe, frames
from f4_cases import registry
from contracts import StreamRequest, Usage
from services.gateway import GatewayService
from services.models import ProviderConfig
from services.prompts import PromptRegistry
from providers.mock import MockProvider


async def test_prompt_removal_does_not_change_frozen_request(server,case):
    name,config=case;provider=MockProvider();app=make_app(config,provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            created=await client.post('/v1/llm/stream',json=body())
            rid=created.json()['data']['request_id']
            before,_,_=observe(server,name,rid)
            from utils.json_codec import loads
            snapshot=loads(before['input_snapshot'])
            app.state.prompt_registry=PromptRegistry([])
            result=await client.get(f'/v1/llm/stream/{rid}/events')
            assert frames(result.text)[-1][1]=='response.completed'
            assert [m.model_dump() for m in provider.stream_calls[0][1]]==snapshot['messages']
            assert provider.stream_options[0]['max_output_tokens']==snapshot['max_output_tokens']
            assert observe(server,name,rid)[0]['input_snapshot']==before['input_snapshot']


@pytest.mark.parametrize('resource',['provider','counter'])
async def test_incompatible_execution_resources_fail_without_call(server,case,resource,monkeypatch):
    name,config=case;provider=MockProvider();app=make_app(config,provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            rid=(await client.post('/v1/llm/stream',json=body())).json()['data']['request_id']
            manager=app.state.streaming_manager
            if resource=='provider':
                models=registry()
                manager.registry=ModelRegistry(models.endpoints,dict(models.providers)|{'mock-main':ProviderConfig(
                    adapter='deepseek',api_protocol='chat_completions',base_url='https://example.invalid',credential_ref='test-reference')}, concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))
            else:
                counter=manager.token_budget.counter_for(manager.registry.endpoints['general-primary'])
                monkeypatch.setattr(counter,'version','controlled-new-counter-version')
            result=await client.get(f'/v1/llm/stream/{rid}/events')
            assert frames(result.text)[-1][2]['code']=='execution_configuration_changed'
            row,attempts,events=observe(server,name,rid)
            assert row['status']=='failed' and attempts==[] and len(events)==1 and provider.stream_calls==[]


async def seed_o1_history(app,rid,count,status):
    state=app.state
    trace=state.traces.start_request(rid,operation='streaming')
    trace.agent_id = 'legacy-test'  # Owned history with O1 event and snapshot format.
    gateway=GatewayService(state.providers,state.traces,state.prompt_registry,state.model_registry,state.token_budget, limiter=state.concurrency_limiter)
    execution=gateway.prepare_stream(StreamRequest.model_validate(body()),trace)
    # O1's real Store path, before pending/two-stage creation existed.
    await state.storage.prepare(trace,execution.snapshot)
    attempt=state.traces.start_attempt(trace,'general-primary',provider='mock-main',adapter='mock',
        configured_model=execution.prepared.model.model_id,api_protocol='chat_completions')
    await state.storage.begin_attempt(trace,attempt)
    for seq in range(1,count+1):
        await state.storage.append_event(trace,'response.delta',{'request_id':rid,'delta':f'{seq} 中文🙂\n'})
    attempt.usage=Usage(input_tokens=10,output_tokens=3,total_tokens=13)
    code='provider_invalid_response' if status=='failed' else None
    data={'request_id':rid,**({'code':code,'message':'历史失败'} if code else {'usage':attempt.usage.model_dump()})}
    await state.storage.finish(trace,attempt,error_code=code,event=('response.'+status,data))


@pytest.mark.parametrize('status',['completed','failed'])
async def test_205_legacy_deltas_replay_across_pg_pages_without_mutation(server,case,status):
    name,config=case;provider=MockProvider();app=make_app(config,provider)
    async with app.router.lifespan_context(app):
        rid=str(uuid4());await seed_o1_history(app,rid,205,status)
        original=observe(server,name,rid)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            url=f'/v1/llm/stream/{rid}/events'
            for cursor in [0,99,100,101,200,205]:
                result=await client.get(url,params={'after_seq':str(cursor).zfill(5)})
                parsed=frames(result.text)
                assert [e[0] for e in parsed]==list(range(cursor+1,207))
                assert parsed[-1][1]=='response.'+status
                assert parsed==[(r['seq'],r['event_type'],__import__('json').loads(r['payload'])) for r in original[2] if r['seq']>cursor]
            assert (await client.get(url+'?after_seq=206')).status_code==204
            assert observe(server,name,rid)==original
            assert provider.stream_calls==[] and provider.calls==[]
            # O2 retains an independent sequence, rather than continuing history's206.
            fresh=(await client.post('/v1/llm/stream',json=body())).json()['data']['request_id']
            result=await client.get(f'/v1/llm/stream/{fresh}/events')
            assert frames(result.text)[0][0]==1 and len(provider.stream_calls)==1
