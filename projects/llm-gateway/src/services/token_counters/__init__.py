from collections.abc import Iterable

from services.models import ConfigurationError, ModelConfig
from services.token_counters.base import TokenCounter
from services.token_counters.deepseek_v41 import DeepSeekV41Counter
from services.token_counters.qwen_text import QwenTextEstimateCounter


COUNTER_FACTORIES = {"deepseek_v41": DeepSeekV41Counter,
                     "qwen_text_estimate_v1": QwenTextEstimateCounter}


def build_counters(models: Iterable[ModelConfig]) -> dict[str, TokenCounter]:
    identifiers = dict.fromkeys(model.token_counter for model in models)
    if any(identifier not in COUNTER_FACTORIES for identifier in identifiers):
        raise ConfigurationError("计数策略未注册")
    return {identifier: COUNTER_FACTORIES[identifier]() for identifier in identifiers}
