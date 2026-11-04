from registry_helpers import ModelConfig, ModelRegistry
"""Fixed business fixtures; no dependence on runtime YAML or real credentials."""
from services.models import ConcurrencyPolicy
from copy import deepcopy
import json

from f3_cases import MODEL_VALUES
from main import create_app
from providers.mock import MockProvider
from services.models import ProviderConfig, RecoveryPolicy
from settings import Settings


SCHEMA = {
    'type': 'object',
    '$defs': {'Product': {
        'type': 'object',
        'properties': {
            'name': {'type': 'string', 'description': '商品名称'},
            'quantity': {'type': 'integer', 'minimum': 1},
            'kind': {'type': 'string', 'enum': ['配件', '其他']},
        },
        'required': ['name', 'quantity', 'kind'], 'additionalProperties': False,
    }},
    'properties': {'order_id': {'type': 'string'}, 'product': {'$ref': '#/$defs/Product'},
                   'note': {'anyOf': [{'type': 'string'}, {'type': 'null'}]}},
    'required': ['order_id', 'product', 'note'], 'additionalProperties': False,
}
PARSED = {'order_id': 'A123', 'product': {'name': '充电器', 'quantity': 1, 'kind': '配件'}, 'note': None}
TEXT = json.dumps(PARSED, ensure_ascii=False, separators=(',', ':'))
CONTEXT = '订单 A123 的商品为充电器，数量为 1，类别为配件。资料未提供备注。'


def payload(model='general-primary'):
    return {
        'model': model, 'prompt': {'name':'context_answer','version':'1.2.0','variables':{'context':CONTEXT}},
        'messages':[{'role':'user','content':'提取订单号、商品信息和备注；备注未知时使用 null。'}],
        'response_schema':deepcopy(SCHEMA), 'max_output_tokens':128, 'timeout_seconds':30,
    }


def registry(changes=None):
    models={}
    for name, provider, mode in [('general-primary','mock-main','json_object'),
                                 ('general-backup','mock-backup','json_schema')]:
        values=dict(MODEL_VALUES, provider=provider, structured_output_mode=mode)
        values.update((changes or {}).get(name, {}))
        models[name]=ModelConfig(**values)
    return ModelRegistry(models, {
        'mock-main':ProviderConfig(adapter='mock',api_protocol='chat_completions'),
        'mock-backup':ProviderConfig(adapter='mock',api_protocol='responses'),
    }, recovery=RecoveryPolicy(max_attempts=1, max_attempts_per_endpoint=1), concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))


def make_app(provider=None, *, changes=None, budget=None):
    return create_app(settings=Settings(env='test',_env_file=None), provider=provider or MockProvider(text=TEXT),
                      model_registry=registry(changes), token_budget=budget)
