from dataclasses import dataclass
from typing import Literal, Protocol

from contracts import Message
from services.models import ModelConfig


@dataclass(frozen=True)
class TokenCount:
    input_tokens: int
    counter_version: str
    kind: Literal['exact', 'estimated'] = 'exact'


class TokenCounter(Protocol):
    version: str
    supports_generation_constraints: bool

    def supports(self, model: ModelConfig) -> bool: ...
    def count(self, messages: list[Message], *, generation_constraint: dict | None = None) -> int: ...
