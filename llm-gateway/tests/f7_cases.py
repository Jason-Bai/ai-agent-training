from registry_helpers import ModelRegistry
"""F7 test-owned input/scripts; HTTP, recovery and PostgreSQL remain product code."""
from services.models import ConcurrencyPolicy
from contextlib import asynccontextmanager
from copy import deepcopy
import httpx
from f4_cases import registry as previous_registry
from main import create_app
from providers.mock import MockProvider
from providers.base import ProviderResult
from services.models import RecoveryPolicy
from settings import Settings

SCHEMA={'type':'object','properties':{'order_id':{'type':'string'},'action':{'enum':['draft_ticket','ask_order']}},
        'required':['order_id','action'],'additionalProperties':False}
VALID='{"order_id":"A123","action":"draft_ticket"}'


def body(**changes):
    value={'model':'general-primary','prompt':{'name':'basic_text','version':'1.0.0'},
           'messages':[{'role':'user','content':'订单 A123'}]} | changes
    if changes.get('response_schema') is not None:
        value['prompt']={'name':'context_answer','version':'1.2.0','variables':{'context':'订单A123商品破损，允许生成工单草稿。'}}
    return value


def registry(changes=None, policy=None):
    base=previous_registry(changes)
    models=dict(base.endpoints)
    models['general-primary']=models['general-primary'].model_copy(update={'fallback_models':('general-backup',)})
    return ModelRegistry(models,base.providers,recovery=policy or RecoveryPolicy(base_delay_seconds=0.0,max_delay_seconds=0.0), concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))


class SequenceProvider(MockProvider):
    def __init__(self,script,*,label='primary',log=None,before=None):
        super().__init__();self.script=list(script);self.label=label
        self.log=[] if log is None else log
        self.before=before

    async def complete(self,model,messages,*,max_output_tokens,timeout_seconds,response_schema=None):
        self.calls.append((model,deepcopy(messages)))
        self.call_options.append({'max_output_tokens':max_output_tokens,'timeout_seconds':timeout_seconds,
                                  'response_schema':deepcopy(response_schema)})
        self.log.append(self.label)
        if self.before is not None: self.before()
        assert self.script, 'unexpected additional Provider call'
        value=self.script.pop(0)
        if isinstance(value,BaseException): raise value
        return value


@asynccontextmanager
async def application(config,primary,backup=None,*,models=None,budget=None):
    app=create_app(settings=Settings(env='test',_env_file=None),config_dir=config,
        model_registry=models or registry(),providers={'mock-main':primary,'mock-backup':backup or MockProvider()},
        token_budget=budget)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),base_url='http://test') as client:
            yield app,client
