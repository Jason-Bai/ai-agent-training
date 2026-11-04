from registry_helpers import ModelConfig, ModelRegistry
"""Same-provider/different-counter and cross-provider/shared-counter behavior."""
from services.models import ConcurrencyPolicy
import asyncio
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi.testclient import TestClient

from f3_cases import MODEL_VALUES
from main import create_app
from providers.mock import MockProvider
from services.models import ProviderConfig
from services.token_budget import TokenBudget
from settings import Settings


@dataclass
class Counter:
    version: str
    tokens: int
    calls: list = field(default_factory=list)

    def supports(self, model):
        return model.model_id in {'fixture-a','fixture-b'} and model.thinking_mode=='disabled'

    def count(self, messages):
        self.calls.append(messages)
        return self.tokens


def application(*, shared_provider=False, shared_counter=False, window=100):
    first=Counter('counter-a-v1',24);second=first if shared_counter else Counter('counter-b-v2',40)
    definitions={};clients={'p1':MockProvider(text='ONE')}
    if not shared_provider:
        clients['p2']=MockProvider(text='TWO')
    for name,model_id,provider,counter in [('a','fixture-a','p1','c1'),('b','fixture-b','p1' if shared_provider else 'p2','c1' if shared_counter else 'c2')]:
        definitions[name]=ModelConfig(**dict(MODEL_VALUES, model_id=model_id, provider=provider, token_counter=counter,
            context_window_tokens=window, default_output_tokens=16,max_output_tokens=32,safety_margin_tokens=8))
    registry=ModelRegistry(definitions,{key:ProviderConfig(adapter='mock', api_protocol='chat_completions') for key in clients}, concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))
    budget=TokenBudget({'c1':first,'c2':second})
    app=create_app(settings=Settings(env='test',_env_file=None),model_registry=registry,providers=clients,token_budget=budget)
    return app,clients,first,second


def request(model):
    return {'model':model,'prompt':{'name':'basic_text','version':'1.0.0'},'messages':[{'role':'user','content':model}], 'max_output_tokens':16}


@pytest.mark.parametrize('shared_provider,shared_counter',[(True,False),(False,True),(False,False)])
def test_counter_choice_follows_model_not_provider(shared_provider,shared_counter):
    app,clients,a,b=application(shared_provider=shared_provider,shared_counter=shared_counter)
    with TestClient(app) as client:
        for name,counter in [('a',a),('b',b)]:
            response=client.post('/v1/llm',json=request(name))
            assert response.status_code==200
            assert set(response.json()['data'])=={'text','usage'}
            trace=app.state.traces.requests[response.headers['X-Request-ID']]
            assert (trace.input_token_count,trace.counter_version)==(counter.tokens,counter.version)
            assert trace.status=='completed' and trace.attempts==1
    assert sum(len(p.calls) for p in clients.values())==2
    assert sum(len(c.calls) for c in {id(a):a,id(b):b}.values())==2


@pytest.mark.parametrize('window,status',[(48,200),(47,400)])
def test_strategy_budget_preserves_count_before_rejection(window,status):
    app,clients,a,_=application(window=window)
    with TestClient(app) as client:
        response=client.post('/v1/llm',json=request('a'))
    assert response.status_code==status
    trace=app.state.traces.requests[response.headers['X-Request-ID']]
    assert (trace.input_token_count,trace.counter_version,trace.max_output_tokens)==(24,'counter-a-v1',16)
    assert trace.prompt_fingerprint and len(a.calls)==1
    assert trace.attempts==len(clients['p1'].calls)==(1 if status==200 else 0)
    if status==400:
        assert response.json()['code']=='context_length_exceeded' and trace.status=='failed'
        assert response.json()['data'] is None


async def test_interleaved_requests_keep_model_counter_version_and_result():
    app,clients,a,b=application()
    entered=asyncio.Event();release=asyncio.Event()
    original=clients['p1'].complete
    async def delayed(*args,**kwargs):
        entered.set();await release.wait();return await original(*args,**kwargs)
    clients['p1'].complete=delayed
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            pending=asyncio.create_task(client.post('/v1/llm',json=request('a')))
            try:
                await asyncio.wait_for(entered.wait(),2)
                second=await client.post('/v1/llm',json=request('b'))
                assert not pending.done()
            finally:
                release.set()
            first=await pending
    for response,expected,counter in [(first,'ONE',a),(second,'TWO',b)]:
        assert response.status_code==200 and response.json()['data']['text']==expected
        trace=app.state.traces.requests[response.headers['X-Request-ID']]
        assert (trace.input_token_count,trace.counter_version)==(counter.tokens,counter.version)
        attempts=[t for t in app.state.traces.attempts if t.request_id==trace.request_id]
        assert len(attempts)==trace.attempts==1 and attempts[0].status=='completed'
    assert len(clients['p1'].calls)==len(clients['p2'].calls)==1
