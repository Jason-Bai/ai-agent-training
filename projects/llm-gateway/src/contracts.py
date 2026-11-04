from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from constants import (
    MAX_SCHEMA_BYTES, MAX_SCHEMA_EXPANDED_DEPTH, MAX_SCHEMA_RAW_DEPTH,
    MAX_SCHEMA_RAW_NODES, MAX_USER_MESSAGE_CHARS,
)


def _model_default(schema):
    # Defaults depend on the selected model; null is not a legal request value.
    schema.pop("default", None)


class StrictObject(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class PromptSelection(StrictObject):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    variables: dict[str, str] = Field(default_factory=dict)


class Message(StrictObject):
    role: Literal["system", "user", "assistant"]
    content: str


class UserMessage(StrictObject):
    role: Literal["user"]
    content: str = Field(min_length=1, max_length=MAX_USER_MESSAGE_CHARS)

    @field_validator("content")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


class LLMRequest(StrictObject):
    model: str = Field(min_length=1)
    prompt: PromptSelection
    messages: list[UserMessage] = Field(min_length=1, max_length=1)
    max_output_tokens: int | SkipJsonSchema[None] = Field(default=None, gt=0, json_schema_extra=_model_default)
    initial_output_tokens: int | SkipJsonSchema[None] = Field(default=None, gt=0, json_schema_extra=_model_default)
    timeout_seconds: int | SkipJsonSchema[None] = Field(default=None, gt=0, json_schema_extra=_model_default)
    response_schema: dict[str, Any] | SkipJsonSchema[None] = Field(
        default=None, json_schema_extra=_model_default,
        description='可选 Draft 2020-12 平台子集，对象根；完整规则见项目 docs/response-schema.md。'
                    f'省略为文本，显式 null 不合法；最大 {MAX_SCHEMA_BYTES // 1024} KiB，'
                    f'结构深度 {MAX_SCHEMA_EXPANDED_DEPTH}，原始深度 {MAX_SCHEMA_RAW_DEPTH}，'
                    f'节点各 {MAX_SCHEMA_RAW_NODES}。',
    )

    @field_validator("initial_output_tokens", "max_output_tokens", "timeout_seconds", "response_schema", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("omit the field to use the selected model default")
        return value


class StreamRequest(StrictObject):
    model: str = Field(min_length=1)
    prompt: PromptSelection
    messages: list[UserMessage] = Field(min_length=1, max_length=1)
    max_output_tokens: int | SkipJsonSchema[None] = Field(default=None, gt=0, json_schema_extra=_model_default)
    initial_output_tokens: int | SkipJsonSchema[None] = Field(default=None, gt=0, json_schema_extra=_model_default)
    timeout_seconds: int | SkipJsonSchema[None] = Field(default=None, gt=0, json_schema_extra=_model_default)

    @field_validator("initial_output_tokens", "max_output_tokens", "timeout_seconds", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("omit the field to use the selected model default")
        return value


class Usage(StrictObject):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def check_total(self):
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("usage total does not match input and output")
        return self


class LLMData(StrictObject):
    text: str
    usage: Usage | None = None


class StructuredLLMData(LLMData):
    parsed: dict[str, Any]


class ProbeData(StrictObject):
    latency_ms: float = Field(ge=0)


class ProbeResponse(StrictObject):
    success: Literal[True] = True
    code: Literal["ok"] = "ok"
    data: ProbeData
    message: str = "模型探测成功"


class SuccessResponse(StrictObject):
    success: Literal[True] = True
    code: Literal["ok"] = "ok"
    data: StructuredLLMData | LLMData
    message: str = "成功"


class ErrorResponse(StrictObject):
    success: Literal[False] = False
    code: str
    data: None = None
    message: str


class StreamCreatedData(StrictObject):
    request_id: str
    status: Literal['pending'] = 'pending'


class StreamCreatedResponse(StrictObject):
    success: Literal[True] = True
    code: Literal['ok'] = 'ok'
    data: StreamCreatedData
    message: str = '请求已创建'


class StreamCancelData(StrictObject):
    request_id: str
    status: Literal['canceling', 'canceled', 'failed', 'completed']


class StreamCancelResponse(StrictObject):
    success: Literal[True] = True
    code: Literal['ok'] = 'ok'
    data: StreamCancelData
    message: str
