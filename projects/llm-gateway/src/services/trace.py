import asyncio
from datetime import datetime
import logging
from time import monotonic
from dataclasses import dataclass
from typing import Literal

from contracts import Usage


Status = Literal["pending", "running", "canceling", "completed", "failed", "canceled"]


def log_storage_failure(request_id, stage, attempt=None):
    logging.getLogger(__name__).warning(
        '持久化失败 request_id=%s stage=%s attempt_no=%s', request_id, stage,
        attempt.number if attempt is not None else None)


@dataclass
class RequestTrace:
    request_id: str
    operation: Literal["completion", "provider_probe", "streaming"] = "completion"
    requested_model: str | None = None
    prompt_name: str | None = None
    prompt_version: str | None = None
    status: Status = "running"
    attempts: int = 0
    error_code: str | None = None
    prompt_fingerprint: str | None = None
    max_output_tokens: int | None = None
    timeout_seconds: int | None = None
    input_token_count: int | None = None
    counter_version: str | None = None
    input_token_kind: str | None = None
    schema_fingerprint: str | None = None
    structured_output_mode: str | None = None
    stream_started_at: float | None = None
    first_text_sent_at: float | None = None
    upstream_first_text_at: float | None = None
    upstream_terminal_at: float | None = None
    stream_ended_at: float | None = None
    recovery_wait_ms: float | None = None
    error_stage: str | None = None
    cleanup_error_code: str | None = None
    delivery_error_code: str | None = None
    routing: dict | None = None
    auto_routing: dict | None = None

    def record_storage_failure(self, stage, attempt=None):
        self.error_stage = stage
        log_storage_failure(self.request_id, stage, attempt)

    async def wait_for_recovery(self, seconds):
        started = monotonic()
        try:
            await asyncio.sleep(seconds)
        except BaseException:
            self.error_stage = 'recovery_wait'
            raise
        finally:
            self.recovery_wait_ms += (monotonic() - started) * 1000

    @property
    def ttft_ms(self) -> float | None:
        return self._duration(self.stream_started_at, self.first_text_sent_at)

    @property
    def generation_duration_ms(self) -> float | None:
        return self._duration(self.upstream_first_text_at, self.upstream_terminal_at)

    @property
    def stream_duration_ms(self) -> float | None:
        return self._duration(self.stream_started_at, self.stream_ended_at)

    @staticmethod
    def _duration(start, end):
        return None if start is None or end is None else (end - start) * 1000


@dataclass
class AttemptTrace:
    request_id: str
    number: int
    model: str
    provider: str | None = None
    adapter: str | None = None
    configured_model: str | None = None
    actual_model: str | None = None
    finish_reason: str | None = None
    upstream_status: str | None = None
    upstream_finish_reason: str | None = None
    api_protocol: str | None = None
    usage: Usage | None = None
    latency_ms: float | None = None
    status: Status = "running"
    error_code: str | None = None
    provider_error_code: str | None = None
    prompt_fingerprint: str | None = None
    input_token_count: int | None = None
    counter_version: str | None = None
    input_token_kind: str | None = None
    max_output_tokens: int | None = None
    reason: str | None = None
    provider_duration_ms: float | None = None
    provider_entered: bool = False
    cached_input_tokens: int | None = None
    billing_observed_at: datetime | None = None
    purpose: Literal['classification', 'generation'] = 'generation'


class TraceStore:
    """Process-local diagnostics; admitted Attempt inputs may include authorized snapshots."""

    def __init__(self):
        self.requests: dict[str, RequestTrace] = {}
        self.attempts: list[AttemptTrace] = []

    def start_request(self, request_id: str, operation="completion") -> RequestTrace:
        record = RequestTrace(request_id=request_id, operation=operation)
        self.requests[request_id] = record
        return record

    def start_attempt(self, request: RequestTrace, model: str, *, provider=None, adapter=None, configured_model=None, api_protocol=None) -> AttemptTrace:
        request.attempts += 1
        record = AttemptTrace(request_id=request.request_id, number=request.attempts, model=model,
                              provider=provider, adapter=adapter, configured_model=configured_model, api_protocol=api_protocol)
        self.attempts.append(record)
        return record

    def finish_request(self, request: RequestTrace, *, error_code: str | None) -> None:
        if request.status != "running":
            raise RuntimeError("Request trace already finalized")
        request.status = "failed" if error_code else "completed"
        request.error_code = error_code
