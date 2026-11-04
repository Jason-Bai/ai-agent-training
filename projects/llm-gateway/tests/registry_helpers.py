from collections.abc import Mapping
"""Historical fixture inputs split into real F12 endpoints/groups, never production YAML compatibility."""
from typing import Literal
from pydantic import Field, field_validator, model_validator
from services.models import (Definition, EndpointConfig, ModelGroupConfig,
    ModelRegistry as RuntimeRegistry, RecoveryPolicy, ConfigurationError)

class ModelConfig(Definition):
    enabled: bool = True
    provider: str
    model_id: str
    thinking_mode: Literal['disabled'] = 'disabled'
    token_counter: str
    supports_streaming: bool
    structured_output_mode: Literal['none', 'json_object', 'json_schema']
    context_window_tokens: int = Field(gt=0)
    provider_max_output_tokens: int = Field(gt=0)
    default_output_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    safety_margin_tokens: int = Field(gt=0)
    default_timeout_seconds: int = Field(gt=0)
    max_timeout_seconds: int = Field(gt=0)
    fallback_models: tuple[str, ...] = Field(default=())

    @field_validator('fallback_models', mode='before')
    @classmethod
    def freeze_fallbacks(cls, value):
        if not isinstance(value, (list, tuple)):
            raise ValueError('备用模型必须为列表')
        return tuple(value)

    @model_validator(mode='after')
    def budget_relationships(self):
        if not self.default_output_tokens <= self.max_output_tokens <= self.provider_max_output_tokens:
            raise ValueError('默认输出、平台输出和供应商输出上限关系不合法')
        if self.default_timeout_seconds > self.max_timeout_seconds:
            raise ValueError('默认超时超过平台上限')
        if self.default_output_tokens + self.safety_margin_tokens >= self.context_window_tokens:
            raise ValueError('上下文窗口没有为输入保留空间')
        return self




def split_model(value):
    raw = value.model_dump() if hasattr(value, 'model_dump') else dict(value)
    endpoint_fields = set(EndpointConfig.model_fields)
    endpoint = EndpointConfig.model_validate({k:v for k,v in raw.items() if k in endpoint_fields} | {'enabled': raw.get('enabled', True)})
    group = dict(default_output_tokens=raw.get('default_output_tokens', min(1024,endpoint.max_output_tokens)),
                 max_output_tokens=raw['max_output_tokens'], default_timeout_seconds=raw.get('default_timeout_seconds',min(30,endpoint.max_timeout_seconds)),
                 max_timeout_seconds=raw['max_timeout_seconds'])
    return endpoint,group,tuple(getattr(value,'fallback_models',raw.get('fallback_models',())))


def ModelRegistry(models=None, providers=None, *, endpoints=None, model_groups=None, concurrency, recovery=None):
    definitions = models if models is not None else endpoints
    if not isinstance(definitions, Mapping):
        raise ConfigurationError('test fixture endpoint mapping invalid')
    converted={}; groups={} if model_groups is None else dict(model_groups)
    for name,value in definitions.items():
        if value is None:
            raise ConfigurationError('test fixture endpoint invalid')
        endpoint,group,fallbacks=split_model(value)
        converted[name]=endpoint
        if name not in groups:
            groups[name]=ModelGroupConfig(candidates=(name,*fallbacks),**group)
        elif fallbacks:
            groups[name]=groups[name].model_copy(update={'candidates':(name,*fallbacks)})
    return RuntimeRegistry(endpoints=converted,providers=providers,model_groups=groups,
                           concurrency=concurrency,recovery=recovery or RecoveryPolicy())
