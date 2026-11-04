from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette import status as http_status

from errors import GatewayError


DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / 'config'


class ConfigurationError(ValueError):
    """Only safe field/file identifiers may appear in configuration diagnostics."""


class Definition(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra='forbid', hide_input_in_errors=True)

    @field_validator('*')
    @classmethod
    def nonblank_strings(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError('配置文本不能为空')
        return value


class ConcurrencyPolicy(Definition):
    max_requests: int = Field(gt=0)
    max_requests_per_agent: int = Field(gt=0)

    @model_validator(mode='after')
    def limits(self):
        if self.max_requests_per_agent > self.max_requests:
            raise ValueError('Agent并发上限超过全局上限')
        return self


class RecoveryPolicy(Definition):
    max_attempts: int = Field(default=3, ge=1, le=3)
    max_attempts_per_endpoint: int = Field(default=2, ge=1, le=2)
    base_delay_seconds: float = Field(default=0.1, ge=0, allow_inf_nan=False)
    max_delay_seconds: float = Field(default=1.0, ge=0, allow_inf_nan=False)

    @model_validator(mode='after')
    def limits(self):
        if self.max_attempts_per_endpoint > self.max_attempts:
            raise ValueError('单端点次数超过总次数')
        if self.base_delay_seconds > self.max_delay_seconds:
            raise ValueError('基础退避超过最大退避')
        return self


class ProviderConfig(Definition):
    adapter: Literal['mock', 'deepseek', 'openai_compatible', 'bailian']
    api_protocol: Literal['chat_completions', 'responses']
    base_url: str | None = None
    credential_ref: str | None = None

    @model_validator(mode='after')
    def connection_definition(self):
        required_protocol = {'deepseek': 'chat_completions', 'openai_compatible': 'responses',
                             'bailian': 'responses'}.get(self.adapter)
        if required_protocol is not None and self.api_protocol != required_protocol:
            raise ValueError('适配器与 API 协议不兼容')
        if self.adapter == 'mock':
            if self.base_url is not None or self.credential_ref is not None:
                raise ValueError('Mock 不需要连接和凭据配置')
        else:
            if not self.base_url or not self.credential_ref:
                raise ValueError('真实 Provider 需要地址和凭据引用')
            parsed = urlsplit(self.base_url)
            if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
                raise ValueError('Provider 地址必须为 HTTP(S) 地址')
        return self


class EndpointConfig(Definition):
    provider: str
    model_id: str
    thinking_mode: Literal['disabled'] = 'disabled'
    enabled: bool
    token_counter: str
    supports_streaming: bool
    structured_output_mode: Literal['none', 'json_object', 'json_schema']
    context_window_tokens: int = Field(gt=0)
    provider_max_output_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    min_output_tokens: int = Field(default=1, gt=0)
    safety_margin_tokens: int = Field(gt=0)
    max_timeout_seconds: int = Field(gt=0)

    @model_validator(mode='after')
    def budget_relationships(self):
        if self.max_output_tokens > self.provider_max_output_tokens:
            raise ValueError('端点输出上限超过供应商上限')
        if self.min_output_tokens > self.max_output_tokens:
            raise ValueError('端点最小输出超过接入上限')
        return self


# Adapters consume a concrete endpoint. Preserve their annotation name, not the
# old combined configuration format or its public model-to-provider binding.
ModelConfig = EndpointConfig


class ModelGroupConfig(Definition):
    candidates: tuple[str, ...] = Field(min_length=1)
    default_output_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    default_timeout_seconds: int = Field(gt=0)
    max_timeout_seconds: int = Field(gt=0)
    routing_description: str | None = None

    @field_validator('candidates', mode='before')
    @classmethod
    def freeze_candidates(cls, value):
        if not isinstance(value, (list, tuple)):
            raise ValueError('候选端点必须为列表')
        return tuple(value)

    @model_validator(mode='after')
    def relationships(self):
        if any(not value.strip() for value in self.candidates) or len(set(self.candidates)) != len(self.candidates):
            raise ValueError('候选端点不能为空或重复')
        if self.default_output_tokens > self.max_output_tokens:
            raise ValueError('默认输出超过模型组上限')
        if self.default_timeout_seconds > self.max_timeout_seconds:
            raise ValueError('默认超时超过模型组上限')
        return self


class ClassifierPromptConfig(Definition):
    name: str = Field(min_length=1)
    version: str = Field(pattern=r'^[0-9]+\.[0-9]+\.[0-9]+$')


class AutoRouteConfig(Definition):
    classifier_group: str
    candidate_groups: tuple[str, ...] = Field(min_length=1)
    default_group: str
    classifier_prompt: ClassifierPromptConfig
    classifier_timeout_seconds: int = Field(default=5, gt=0, le=5)
    classifier_max_output_tokens: int = Field(default=128, gt=0, le=128)
    default_output_tokens: int = Field(default=1024, gt=0)
    max_output_tokens: int = Field(default=4096, gt=0, le=4096)
    default_timeout_seconds: int = Field(default=30, gt=0)
    max_timeout_seconds: int = Field(default=120, gt=0, le=120)

    @field_validator('candidate_groups', mode='before')
    @classmethod
    def freeze_candidates(cls, value):
        if not isinstance(value, (list, tuple)):
            raise ValueError('候选模型组必须为列表')
        return tuple(value)

    @model_validator(mode='after')
    def relationships(self):
        if any(not name.strip() for name in self.candidate_groups) or len(set(self.candidate_groups)) != len(self.candidate_groups):
            raise ValueError('候选模型组不能为空或重复')
        if self.default_group not in self.candidate_groups:
            raise ValueError('默认模型组必须位于候选中')
        if self.default_output_tokens > self.max_output_tokens:
            raise ValueError('默认输出超过自动路由上限')
        if self.default_timeout_seconds > self.max_timeout_seconds:
            raise ValueError('默认超时超过自动路由上限')
        return self


@dataclass(frozen=True)
class ModelRegistry:
    endpoints: Mapping[str, EndpointConfig]
    providers: Mapping[str, ProviderConfig]
    model_groups: Mapping[str, ModelGroupConfig]
    concurrency: ConcurrencyPolicy
    recovery: RecoveryPolicy = field(default_factory=RecoveryPolicy)
    auto_routes: Mapping[str, AutoRouteConfig] = field(default_factory=dict)

    def __post_init__(self):
        try:
            if not self.endpoints or not self.providers or not self.model_groups:
                raise ValueError
            endpoints = {name: endpoint if isinstance(endpoint, EndpointConfig) else EndpointConfig.model_validate(endpoint)
                         for name, endpoint in self.endpoints.items()}
            providers = {name: provider if isinstance(provider, ProviderConfig) else ProviderConfig.model_validate(provider)
                         for name, provider in self.providers.items()}
            groups = {name: group if isinstance(group, ModelGroupConfig) else ModelGroupConfig.model_validate(group)
                      for name, group in self.model_groups.items()}
            if any(not isinstance(name, str) or not name.strip() for name in [*endpoints, *providers, *groups]):
                raise ValueError
            if any(endpoint.provider not in providers for endpoint in endpoints.values()):
                raise ValueError
            if any(name not in endpoints for group in groups.values() for name in group.candidates):
                raise ValueError
            concurrency = (self.concurrency if isinstance(self.concurrency, ConcurrencyPolicy)
                           else ConcurrencyPolicy.model_validate(self.concurrency))
            recovery = (self.recovery if isinstance(self.recovery, RecoveryPolicy)
                        else RecoveryPolicy.model_validate(self.recovery))
            modes = {'chat_completions': {'none', 'json_object'}, 'responses': {'none', 'json_schema'}}
            if any(endpoint.structured_output_mode not in modes[providers[endpoint.provider].api_protocol]
                   for endpoint in endpoints.values()):
                raise ValueError
            if not isinstance(self.auto_routes, Mapping):
                raise ValueError
            auto_routes = {name: profile if isinstance(profile, AutoRouteConfig) else AutoRouteConfig.model_validate(profile)
                           for name, profile in self.auto_routes.items()}
            if any(not isinstance(name, str) or not name.strip() or name in groups for name in auto_routes):
                raise ValueError
            for profile in auto_routes.values():
                if profile.classifier_group not in groups or any(name not in groups for name in profile.candidate_groups):
                    raise ValueError
                if any(not groups[name].routing_description for name in profile.candidate_groups):
                    raise ValueError
                classifier = groups[profile.classifier_group]
                if (profile.classifier_max_output_tokens > classifier.max_output_tokens
                        or profile.classifier_timeout_seconds > classifier.max_timeout_seconds):
                    raise ValueError
                # Disabled endpoints remain valid configuration. Availability is
                # checked at execution time, but one endpoint must physically
                # support the complete fixed classifier budget and JSON output.
                if not any(
                    endpoint.min_output_tokens <= profile.classifier_max_output_tokens <= endpoint.max_output_tokens
                    and profile.classifier_timeout_seconds <= endpoint.max_timeout_seconds
                    and profile.classifier_max_output_tokens + endpoint.safety_margin_tokens < endpoint.context_window_tokens
                    and endpoint.structured_output_mode != 'none'
                    for endpoint in (endpoints[name] for name in classifier.candidates)
                ):
                    raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ConfigurationError('模型注册配置不合法：请检查字段、预算和 Provider/端点引用') from None
        object.__setattr__(self, 'endpoints', MappingProxyType(endpoints))
        object.__setattr__(self, 'providers', MappingProxyType(providers))
        object.__setattr__(self, 'model_groups', MappingProxyType(groups))
        object.__setattr__(self, 'recovery', recovery)
        object.__setattr__(self, 'concurrency', concurrency)
        object.__setattr__(self, 'auto_routes', MappingProxyType(auto_routes))

    def candidates(self, name: str) -> tuple[str, ...]:
        return self.get_group(name).candidates

    def get_group(self, name: str) -> ModelGroupConfig:
        try:
            return self.model_groups[name]
        except KeyError:
            raise GatewayError('unknown_model', '模型组未注册', http_status.HTTP_400_BAD_REQUEST) from None

    def get_endpoint(self, name: str) -> EndpointConfig:
        return self.endpoints[name]

    def get_auto_route(self, name: str) -> AutoRouteConfig | None:
        return self.auto_routes.get(name)


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node):
    pairs = loader.construct_pairs(node)
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('YAML 存在重复键')
        result[key] = value
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def read_yaml(path: Path) -> dict:
    try:
        with path.open(encoding='utf-8') as stream:
            data = yaml.load(stream, Loader=UniqueKeyLoader)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, TypeError, RecursionError):
        raise ConfigurationError(f'{path.name} 无法读取或 YAML 结构不合法') from None


def load_registry(config_dir: Path = DEFAULT_CONFIG_DIR) -> ModelRegistry:
    data = read_yaml(config_dir / 'gateway.yaml')
    required = {'endpoints', 'model_groups', 'providers', 'concurrency'}
    if not required <= set(data) or set(data) - (required | {'database', 'recovery', 'auto_routes', 'pricing'}):
        raise ConfigurationError('gateway.yaml 必须包含 endpoints、model_groups、providers、concurrency，可包含 database、recovery、auto_routes、pricing，不能包含其他顶层字段')
    return ModelRegistry(endpoints=data['endpoints'], providers=data['providers'], model_groups=data['model_groups'],
                         concurrency=data['concurrency'], recovery=data.get('recovery', {}),
                         auto_routes=data.get('auto_routes', {}))
