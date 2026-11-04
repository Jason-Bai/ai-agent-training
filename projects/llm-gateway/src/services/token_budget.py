from collections.abc import Iterable, Mapping
from types import MappingProxyType

from starlette import status as http_status

from contracts import Message
from errors import GatewayError
from services.models import ConfigurationError, ModelConfig
from services.token_counters import build_counters
from services.token_counters.base import TokenCount, TokenCounter


class TokenBudget:
    def __init__(self, counters: Mapping[str, TokenCounter]):
        self._counters = MappingProxyType(dict(counters))

    @classmethod
    def from_models(cls, models: Iterable[ModelConfig]):
        models = tuple(models)
        budget = cls(build_counters(models))
        budget.validate_models(models)
        return budget

    def counter_for(self, model: ModelConfig) -> TokenCounter:
        try:
            counter = self._counters[model.token_counter]
        except KeyError:
            raise ConfigurationError("计数策略未注册") from None
        if not counter.supports(model):
            raise ConfigurationError("计数策略与模型或运行模式不匹配")
        if getattr(model, 'structured_output_mode', 'none') != 'none' and not getattr(counter, 'supports_generation_constraints', False):
            raise ConfigurationError("计数策略不支持生成约束")
        return counter

    def validate_models(self, models: Iterable[ModelConfig]) -> None:
        for model in models:
            self.counter_for(model)

    def count(self, model: ModelConfig, messages: list[Message], *, response_schema: dict | None = None) -> TokenCount:
        counter = self.counter_for(model)
        options = {}
        if response_schema is not None:
            # Chat's JSON mode adds a format flag to the encoded input even
            # though the requested Schema itself already occurs in User text.
            constraint = {'type': 'json_object'} if model.structured_output_mode == 'json_object' else response_schema
            options['generation_constraint'] = constraint
        return TokenCount(counter.count(messages, **options), counter.version,
                          kind=getattr(counter, 'kind', 'exact'))

    @staticmethod
    def check(input_tokens, output_tokens, model):
        if input_tokens + output_tokens + model.safety_margin_tokens > model.context_window_tokens:
            raise GatewayError("context_length_exceeded", "输入与预留输出超过模型上下文预算", http_status.HTTP_400_BAD_REQUEST)
