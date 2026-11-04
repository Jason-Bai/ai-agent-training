"""Public metadata allowlist for the request diagnostic endpoint."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from contracts import StrictObject, Usage


class TraceAttempt(StrictObject):
    attempt_no: int
    model: str
    provider: str
    adapter: str
    api_protocol: str
    configured_model: str
    actual_model: str | None
    reason: str | None
    status: str
    finish_reason: str | None
    upstream_status: str | None
    upstream_finish_reason: str | None
    error_code: str | None
    provider_error_code: str | None
    cleanup_error_code: str | None
    started_at: datetime
    ended_at: datetime | None
    latency_ms: float | None
    provider_duration_ms: float | None
    provider_entered: bool | None
    prompt_fingerprint: str | None
    input_token_count: int | None
    counter_version: str | None
    input_token_kind: Literal['exact', 'estimated'] | None = None
    max_output_tokens: int | None
    usage: Usage | None
    purpose: Literal['classification', 'generation'] = 'generation'


class TraceUsage(StrictObject):
    known_subtotal: Usage | None
    unknown_attempts: list[int]


class TraceTiming(StrictObject):
    request_duration_ms: float | None
    ttft_ms: float | None
    generation_duration_ms: float | None
    stream_duration_ms: float | None


class SkippedCandidate(StrictObject):
    model: str
    code: str


class TraceRecovery(StrictObject):
    wait_ms: float | None
    skipped_candidates: list[SkippedCandidate] | None
    stop_reason: str | None


class TraceCancellation(StrictObject):
    requested_at: datetime | None
    ended_at: datetime | None
    cleanup_error_code: str | None


class RouteCandidateTrace(StrictObject):
    endpoint_id: str
    status: Literal['eligible', 'skipped']
    code: Literal['endpoint_disabled', 'unsupported_capability', 'invalid_output_budget',
                  'prompt_too_long', 'context_length_exceeded'] | None


class TraceRouting(StrictObject):
    version: Literal[1]
    strategy: Literal['ordered', 'probe_first_enabled']
    config_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidates: list[RouteCandidateTrace]
    selected_endpoint: str | None
    reason: Literal['first_eligible', 'no_eligible_endpoint']


class AutoGroupTrace(StrictObject):
    group: str
    status: Literal['eligible', 'skipped']
    code: str | None


class TraceAutoRouting(StrictObject):
    version: Literal[1]
    profile: str
    candidate_groups: list[AutoGroupTrace]
    resolved_group: str | None
    source: Literal['single_eligible_group', 'classifier', 'default_fallback'] | None
    fallback_reason: str | None
    classifier_prompt_name: str
    classifier_prompt_version: str
    classification_duration_ms: float | None


class TraceData(StrictObject):
    request_id: UUID
    operation: str
    status: str
    requested_model: str | None
    prompt_name: str | None
    prompt_version: str | None
    prompt_fingerprint: str | None
    schema_fingerprint: str | None
    structured_output_mode: str | None
    input_token_count: int | None
    counter_version: str | None
    input_token_kind: Literal['exact', 'estimated'] | None = None
    max_output_tokens: int | None
    timeout_seconds: int | None
    created_at: datetime
    started_at: datetime | None
    deadline_at: datetime | None
    ended_at: datetime | None
    error_code: str | None
    error_stage: str | None
    delivery_error_code: str | None
    attempts: list[TraceAttempt]
    usage: TraceUsage
    timing: TraceTiming
    recovery: TraceRecovery
    cancellation: TraceCancellation
    routing: TraceRouting | None = None
    auto_routing: TraceAutoRouting | None = None


class TraceResponse(StrictObject):
    success: Literal[True] = True
    code: Literal['ok'] = 'ok'
    data: TraceData
    message: str = '成功'
