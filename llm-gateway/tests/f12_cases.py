"""Owned three-layer fixtures; public groups deliberately differ from endpoint IDs."""
from copy import deepcopy
from uuid import uuid4

from contracts import LLMRequest, StreamRequest
from providers.base import ProviderResult
from services.models import (EndpointConfig, ModelGroupConfig, ModelRegistry,
                             ProviderConfig, ConcurrencyPolicy, RecoveryPolicy)
from services.concurrency import ConcurrencyLimiter
from services.gateway import GatewayService
from services.prompts import DEFAULT_TEMPLATES, PromptRegistry
from services.token_budget import TokenBudget
from services.trace import TraceStore

ENDPOINT = dict(model_id='deepseek-flash', thinking_mode='disabled', enabled=True,
                token_counter='deepseek_v41', supports_streaming=True,
                structured_output_mode='json_object', context_window_tokens=1048576,
                provider_max_output_tokens=393216, max_output_tokens=4096,
                safety_margin_tokens=16, max_timeout_seconds=30)
GROUP = dict(default_output_tokens=128, max_output_tokens=4096,
             default_timeout_seconds=30, max_timeout_seconds=120)


def registry(changes=None, *, order=('chat-a','responses-b'), group_changes=None,
             extra_groups=None, policy=None):
    endpoints={}
    for name, provider, mode in [('chat-a','mock-main','json_object'),
                                 ('responses-b','mock-backup','json_schema')]:
        endpoints[name]=EndpointConfig(**(ENDPOINT | {'provider':provider,
            'structured_output_mode':mode} | deepcopy((changes or {}).get(name,{}))))
    groups={'general-primary':ModelGroupConfig(**(GROUP | {'candidates':list(order)} |
                  deepcopy(group_changes or {})))}
    for name, definition in (extra_groups or {}).items():
        groups[name]=ModelGroupConfig(**(GROUP | definition))
    return ModelRegistry(endpoints=endpoints, providers={
        'mock-main':ProviderConfig(adapter='mock',api_protocol='chat_completions'),
        'mock-backup':ProviderConfig(adapter='mock',api_protocol='responses')},
        model_groups=groups, concurrency=ConcurrencyPolicy(max_requests=4,max_requests_per_agent=4),
        recovery=policy or RecoveryPolicy(base_delay_seconds=0,max_delay_seconds=0))


def request(*, streaming=False, structured=False, **changes):
    body=dict(model='general-primary',prompt={'name':'basic_text','version':'1.0.0'},
              messages=[{'role':'user','content':'订单A123的充电器损坏，请分析。'}])
    if structured:
        body.update(prompt={'name':'context_answer','version':'1.2.0',
            'variables':{'context':'订单A123，商品充电器。'}},
            response_schema={'type':'object','properties':{'order_id':{'type':'string'}},
                             'required':['order_id'],'additionalProperties':False})
    body.update(changes)
    return (StreamRequest if streaming else LLMRequest).model_validate(body)


def service(definition=None, primary=None, backup=None, budget=None):
    definition=definition or registry()
    primary=primary or ScriptedProvider(ProviderResult(text='primary'))
    backup=backup or ScriptedProvider(ProviderResult(text='backup'))
    traces=TraceStore()
    svc=GatewayService({'mock-main':primary,'mock-backup':backup},traces,
        PromptRegistry(DEFAULT_TEMPLATES),definition,
        budget or TokenBudget.from_models(definition.endpoints.values()),
        limiter=ConcurrencyLimiter(definition.concurrency))
    trace=traces.start_request(str(uuid4()));trace.agent_id='f12-agent'
    return svc,trace,primary,backup,traces


class ScriptedProvider:
    def __init__(self,*items):
        self.items=list(items);self.calls=[]

    async def complete(self,model,messages,**options):
        self.calls.append((model,deepcopy(messages),deepcopy(options)))
        assert self.items,'unexpected Provider call'
        item=self.items.pop(0)
        if isinstance(item,BaseException):raise item
        return item


class FixedCounter:
    supports_generation_constraints=True
    def __init__(self,count,version):self.value=count;self.version=version;self.observed=[]
    def supports(self,model):return True
    def count(self,messages,**options):
        self.observed.append((deepcopy(messages),deepcopy(options)))
        return self.value
