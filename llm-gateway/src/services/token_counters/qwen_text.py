"""Local empirical Qwen text estimate, not a model tokenizer or proven bound.

UTF-8 JSON bytes account for all message contents, roles and constraints. The
fixed and proportional margins cover observed protocol/template overhead; real
Usage calibration is required before accepting a model binding. Estimates are
for preflight/reservation only, never a replacement for reported billing Usage.
"""
from contracts import Message
from services.models import ModelConfig
from utils.json_codec import dumps


class QwenTextEstimateCounter:
    kind = 'estimated'
    supports_generation_constraints = True
    fixed_overhead_tokens = 512
    safety_percent = 10

    @property
    def version(self) -> str:
        return (f'qwen_text_estimate_v1:utf8-json:fixed{self.fixed_overhead_tokens}'
                f':percent{self.safety_percent}')

    @staticmethod
    def encode(messages: list[Message], *, generation_constraint: dict | None = None) -> str:
        value = {'messages': [message.model_dump() for message in messages]}
        if generation_constraint is not None:
            value['generation_constraint'] = generation_constraint
        return dumps(value, sort_keys=True)

    def count(self, messages: list[Message], *, generation_constraint: dict | None = None) -> int:
        encoded = self.encode(messages, generation_constraint=generation_constraint)
        # Unpaired surrogates have no UTF-8 encoding. Escaping them retains
        # their representation in this count-only view rather than dropping it.
        size = len(encoded.encode('utf-8', errors='backslashreplace'))
        return ((size + self.fixed_overhead_tokens) * (100 + self.safety_percent) + 99) // 100

    def supports(self, model: ModelConfig) -> bool:
        return model.model_id in {'qwen3.8-flash', 'qwen3.7-plus', 'qwen3.8-max'} and model.thinking_mode == 'disabled'
