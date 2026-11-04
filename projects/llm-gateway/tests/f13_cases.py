"""Owned auto-routing definitions and the real in-process service pipeline."""
from uuid import uuid4

from f12_cases import registry as legacy_registry, ScriptedProvider
from providers.base import ProviderResult
from services.models import ModelRegistry
from services.prompts import DEFAULT_TEMPLATES, PromptRegistry
from services.classification import CLASSIFIER_TEMPLATE
from services.gateway import GatewayService
from services.concurrency import ConcurrencyLimiter
from services.trace import TraceStore
from services.token_budget import TokenBudget


def registry(changes=None, *, profile_changes=None):
    base=legacy_registry()
    endpoints={name:base.endpoints['chat-a'].model_dump() for name in ('light-e','general-e','analysis-e')}
    for name,value in (changes or {}).items():endpoints[name].update(value)
    group=dict(default_output_tokens=128,max_output_tokens=4096,
               default_timeout_seconds=30,max_timeout_seconds=120)
    groups={name:dict(group,candidates=[name+'-e'],routing_description=description)
        for name,description in [('light','提取与简单改写'),('general','依据明确资料回答和整理'),
                                 ('analysis','冲突证据比较与多条件推导')]}
    profile=dict(classifier_group='light',candidate_groups=['light','general','analysis'],
        default_group='general',classifier_prompt={'name':'task_router','version':'1.0.0'},
        default_output_tokens=128)
    profile.update(profile_changes or {})
    return ModelRegistry(endpoints=endpoints,providers=base.providers,model_groups=groups,
        concurrency=base.concurrency,recovery=base.recovery,auto_routes={'auto-general':profile})


def service(definitions=None,*items):
    definitions=definitions or registry()
    provider=ScriptedProvider(*(items or [ProviderResult(text='{"group":"general"}'),
                                         ProviderResult(text='订单A123：充电器。')]))
    traces=TraceStore();prompts=PromptRegistry([*DEFAULT_TEMPLATES,CLASSIFIER_TEMPLATE])
    gateway=GatewayService({'mock-main':provider,'mock-backup':provider},traces,prompts,
        definitions,TokenBudget.from_models(definitions.endpoints.values()),
        limiter=ConcurrencyLimiter(definitions.concurrency))
    trace=traces.start_request(str(uuid4()));trace.agent_id='f13-agent'
    return gateway,trace,provider,traces
