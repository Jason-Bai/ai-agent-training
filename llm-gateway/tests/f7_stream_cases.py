from registry_helpers import ModelRegistry
"""F7 script controls only the Provider; real Gateway/PG owns recovery and cancellation."""
from services.models import ConcurrencyPolicy
import asyncio
from contextlib import asynccontextmanager
import httpx
from f4_cases import registry
from f6_cases import body, observe, frames, server, case, poll, cancel_url, until
from main import create_app
from services.models import RecoveryPolicy
from settings import Settings
from providers.mock import MockProvider
from providers.base import ProviderStreamEvent
from errors import ProviderCallError


def failure(code='provider_connection_error', **kw):
    return ProviderCallError(code=code, **kw)


def success(delta='订单 A123'):
    return [ProviderStreamEvent(delta=delta), ProviderStreamEvent(status='completed', upstream_terminal_received=True)]


class ScriptedStream(MockProvider):
    def __init__(self, scripts):
        super().__init__()
        self.scripts=scripts
        self.budgets=[]
        self.timeouts=[]
    async def stream(self, model, messages, *, max_output_tokens, timeout_seconds):
        index=len(self.stream_calls)
        self.stream_calls.append((model,list(messages)))
        self.budgets.append(max_output_tokens)
        self.timeouts.append(timeout_seconds)
        try:
            for item in self.scripts[index]:
                if isinstance(item,asyncio.Event): await item.wait()
                elif isinstance(item,BaseException): raise item
                else: yield item
        finally:self.stream_close_calls+=1


@asynccontextmanager
async def application(config, primary, backup=None, **policy):
    base=registry({'general-primary': {'fallback_models':['general-backup']}})
    models=ModelRegistry(base.endpoints,base.providers,model_groups=base.model_groups,recovery=RecoveryPolicy(base_delay_seconds=0.,max_delay_seconds=0.,**policy), concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))
    app=create_app(settings=Settings(env='test',_env_file=None),config_dir=config,model_registry=models,
                   providers={'mock-main':primary,'mock-backup':backup or ScriptedStream([success()])})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),base_url='http://test') as client:
            yield app,client


async def create(client, **changes):
    response=await client.post('/v1/llm/stream',json=body(**changes))
    assert response.status_code==202,response.text
    return response.json()['data']['request_id']


class ClosingFailureProvider(MockProvider):
    """The first failed invocation owns an iterator with a controlled close."""
    def __init__(self,mode):
        super().__init__();self.mode=mode;self.closing=asyncio.Event();self.release=asyncio.Event();self.cancelled=asyncio.Event();self.active_closes=0
    def stream(self,model,messages,*,max_output_tokens,timeout_seconds):
        self.stream_calls.append((model,list(messages)))
        owner=self
        class Iterator:
            def __aiter__(self):return self
            async def __anext__(self):
                return ProviderStreamEvent(status='failed',error_code='provider_connection_error')
            async def aclose(self):
                owner.stream_close_calls+=1;owner.active_closes+=1;owner.closing.set()
                try:
                    if owner.mode=='error':raise RuntimeError('private iterator close exception')
                    while not owner.release.is_set():
                        try:await owner.release.wait()
                        except asyncio.CancelledError:
                            if owner.mode!='swallow':raise
                            owner.cancelled.set()
                finally:owner.active_closes-=1
        return Iterator()
