from collections.abc import AsyncIterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import re
import sys
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, ValidationError, field_validator, model_validator
from starlette import status as http_status

from constants import CHAT_TERMINAL_REASONS
from contracts import Message, StrictObject, Usage
from errors import PROVIDER_MESSAGES, ProviderCallError
from services.models import ModelConfig
from utils.diagnostic_values import attempt_usage, metadata_text


class ProviderMetadata(StrictObject):
    actual_model: str | None = None
    usage: Usage | None = None
    billing_observed_at: AwareDatetime | None = None
    cached_input_tokens: int | None = Field(default=None, ge=0)

    @field_validator('cached_input_tokens', mode='before')
    @classmethod
    def cache_count_matches_usage(cls, value, info):
        usage = info.data.get('usage')
        if usage is not None and type(value) is int and 0 <= value <= usage.input_tokens:
            return value
        return None

    @field_validator('actual_model', 'finish_reason', 'upstream_status', 'upstream_finish_reason', check_fields=False)
    @classmethod
    def valid_text(cls, value):
        if value is not None and metadata_text(value) is None:
            raise ValueError('invalid Provider diagnostic text')
        return value

    @field_validator('usage')
    @classmethod
    def valid_usage(cls, value):
        if value is not None and attempt_usage(value) is None:
            raise ValueError('invalid per-attempt token counts')
        return value


class ProviderResult(ProviderMetadata):
    text: str
    finish_reason: str = 'stop'
    upstream_status: str | None = None
    upstream_finish_reason: str | None = None


class ProviderStreamEvent(ProviderMetadata):
    delta: str = ''
    status: Literal['running', 'completed', 'failed'] = 'running'
    error_code: str | None = None
    transient: bool = False
    retry_after_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    upstream_http_status: int | None = Field(default=None, ge=400, le=599)
    upstream_terminal_received: bool = False
    upstream_status: Literal['queued', 'in_progress', 'completed', 'failed', 'incomplete'] | None = None
    upstream_finish_reason: Literal['stop', 'length', 'content_filter', 'refusal', 'tool_calls',
                                    'aborted', 'insufficient_system_resource', 'max_output_tokens'] | None = None

    @model_validator(mode='after')
    def valid_combination(self):
        if self.status != 'failed' and (self.transient or self.retry_after_seconds is not None
                                        or self.upstream_http_status is not None):
            raise ValueError('only failed events carry recovery evidence')
        if self.status == 'failed':
            if self.error_code not in PROVIDER_MESSAGES:
                raise ValueError('failed event needs a known Provider error code')
        elif self.error_code is not None:
            raise ValueError('only failed events carry an error code')
        if self.status == 'running' and (self.usage is not None or self.billing_observed_at is not None or self.upstream_terminal_received):
            raise ValueError('running event cannot carry terminal data')
        if self.status == 'completed' and not self.upstream_terminal_received:
            raise ValueError('completed event requires upstream terminal evidence')
        return self


class Provider(Protocol):
    async def complete(self, model: ModelConfig, messages: list[Message], *,
                       max_output_tokens: int, timeout_seconds: int,
                       response_schema: dict | None = None) -> ProviderResult: ...
    def stream(self, model: ModelConfig, messages: list[Message], *,
               max_output_tokens: int, timeout_seconds: float) -> AsyncIterator[ProviderStreamEvent]: ...
    async def aclose(self) -> None: ...


def cached_input(counts, *, chat, total):
    """Conflicting, malformed or absent billing evidence remains unknown."""
    detail_key = 'prompt_tokens_details' if chat else 'input_tokens_details'
    hits = []
    if detail_key in counts:
        detail = counts[detail_key]
        if not isinstance(detail, dict):
            return None
        if 'cached_tokens' in detail:
            hits.append(detail['cached_tokens'])
    if chat and 'prompt_cache_hit_tokens' in counts:
        hits.append(counts['prompt_cache_hit_tokens'])
    if chat and 'prompt_cache_miss_tokens' in counts:
        miss = counts['prompt_cache_miss_tokens']
        if type(miss) is not int or not 0 <= miss <= total:
            return None
        hits.append(total - miss)
    if hits and all(type(hit) is int and hit == hits[0] and 0 <= hit <= total for hit in hits):
        return hits[0]
    return None


def observe_reported_metadata(raw, observed, *, chat):
    """Capture independent facts before rejecting malformed response data."""
    model = metadata_text(raw.get('model'))
    if model is not None:
        observed['actual_model'] = model
    usage = None
    counts = raw.get('usage')
    if counts is not None:
        usage = attempt_usage(Usage(input_tokens=counts['prompt_tokens' if chat else 'input_tokens'],
                                    output_tokens=counts['completion_tokens' if chat else 'output_tokens'],
                                    total_tokens=counts['total_tokens']))
        if usage is None:
            raise ValueError('invalid Provider token counts')
        observed['usage'] = usage
        observed['cached_input_tokens'] = cached_input(counts, chat=chat, total=usage.input_tokens)
    if model is None:
        raise ValueError('invalid Provider model')
    return model, usage


def observe_terminal_usage(observed):
    """Only a recognized terminal upstream report is evidence of the full bill."""
    if observed.get('usage') is not None:
        observed['billing_observed_at'] = datetime.now(timezone.utc)


def parse_response(raw) -> ProviderResult:
    """Validate raw wire types before SDK coercion; ignore unrelated metadata."""
    observed = {}
    try:
        if not isinstance(raw, dict):
            raise ValueError
        actual, usage = observe_reported_metadata(raw, observed, chat=True)
        choices = raw.get('choices')
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError
        choice = choices[0]
        if (isinstance(choice, dict) and raw.get('object') == 'chat.completion'
                and choice.get('finish_reason') in CHAT_TERMINAL_REASONS):
            observe_terminal_usage(observed)
        message = choice['message']
        if message['role'] != 'assistant' or message.get('tool_calls'):
            raise ValueError
        text = message['content']
        reason = choice['finish_reason']
        original_reason = reason
        if metadata_text(reason) is None:
            raise ValueError
        if message.get('refusal') and reason == 'stop':
            if not isinstance(message['refusal'], str):
                raise ValueError
            reason = 'refusal'
            text = text if text is not None else ''
        if not isinstance(text, str) or not isinstance(reason, str) or not reason.strip():
            raise ValueError
        return ProviderResult(text=text, actual_model=actual, finish_reason=reason, usage=usage, cached_input_tokens=observed.get('cached_input_tokens'),
                              billing_observed_at=observed.get('billing_observed_at'),
                              upstream_finish_reason=original_reason)
    except (KeyError, TypeError, ValueError, ValidationError):
        raise ProviderCallError(code='provider_invalid_response', **observed) from None


def parse_responses_response(raw) -> ProviderResult:
    """Decode Responses without treating HTTP 200 or SDK output_text as success."""
    observed = {}
    try:
        if not isinstance(raw, dict) or raw.get('object') != 'response':
            raise ValueError
        actual, usage = observe_reported_metadata(raw, observed, chat=False)
        state = raw.get('status')
        if state in ('failed', 'incomplete', 'completed'):
            observe_terminal_usage(observed)
        metadata = dict(observed, upstream_status=state)
        error, incomplete = raw.get('error'), raw.get('incomplete_details')
        if state == 'failed':
            if (not isinstance(error, dict) or not isinstance(error.get('code'), str)
                    or not error['code'].strip() or not isinstance(error.get('message'), str)
                    or incomplete is not None):
                raise ValueError
            code, status = {
                'rate_limit_exceeded': ('provider_rate_limited', http_status.HTTP_503_SERVICE_UNAVAILABLE),
                'server_error': ('provider_unavailable', http_status.HTTP_503_SERVICE_UNAVAILABLE),
                'invalid_api_key': ('provider_auth_error', http_status.HTTP_502_BAD_GATEWAY),
                'insufficient_quota': ('provider_quota_exceeded', http_status.HTTP_503_SERVICE_UNAVAILABLE),
            }.get(error['code'], ('provider_error', http_status.HTTP_502_BAD_GATEWAY))
            raise ProviderCallError(code=code, status_code=status,
                                    transient=error['code'] == 'server_error', **metadata)
        if error is not None:
            raise ValueError
        if state == 'incomplete':
            if not isinstance(incomplete, dict):
                raise ValueError
            reason = incomplete.get('reason')
            normalized = {'max_output_tokens':'length','content_filter':'content_filter'}.get(reason)
            if normalized is None:
                raise ValueError
            return ProviderResult(text='', finish_reason=normalized, upstream_finish_reason=reason, **metadata)
        if state != 'completed' or incomplete is not None:
            raise ValueError
        output = raw.get('output')
        if not isinstance(output, list):
            raise ValueError
        parts = []
        refusal = False
        messages = 0
        for item in output:
            if not isinstance(item, dict):
                raise ValueError
            if item.get('type') == 'reasoning':
                continue
            if item.get('type') != 'message' or item.get('role') != 'assistant' or item.get('status') != 'completed':
                raise ValueError
            messages += 1
            content = item.get('content')
            if not isinstance(content, list) or not content:
                raise ValueError
            for part in content:
                if not isinstance(part, dict):
                    raise ValueError
                if part.get('type') == 'refusal' and isinstance(part.get('refusal'), str) and part['refusal'].strip():
                    refusal = True
                elif part.get('type') == 'output_text' and isinstance(part.get('text'), str):
                    parts.append(part['text'])
                else:
                    raise ValueError
        if messages != 1:
            raise ValueError
        return ProviderResult(text=''.join(parts), finish_reason='refusal' if refusal else 'stop', **metadata)
    except (KeyError, TypeError, ValueError, ValidationError):
        raise ProviderCallError(code='provider_invalid_response', **observed) from None


def validate_result(result: ProviderResult) -> None:
    reason = result.finish_reason
    if reason == 'stop' and result.text.strip():
        return
    code, status = {
        'length': ('output_truncated', http_status.HTTP_502_BAD_GATEWAY),
        'content_filter': ('provider_content_filtered', http_status.HTTP_502_BAD_GATEWAY),
        'refusal': ('provider_refused', http_status.HTTP_502_BAD_GATEWAY),
        'aborted': ('provider_unavailable', http_status.HTTP_503_SERVICE_UNAVAILABLE),
        'insufficient_system_resource': ('provider_unavailable', http_status.HTTP_503_SERVICE_UNAVAILABLE),
    }.get(reason, ('provider_invalid_response', http_status.HTTP_502_BAD_GATEWAY))
    raise ProviderCallError(code=code, status_code=status, actual_model=result.actual_model,
                            transient=reason in {'aborted', 'insufficient_system_resource'},
                            finish_reason=reason, usage=result.usage, cached_input_tokens=result.cached_input_tokens,
                            billing_observed_at=result.billing_observed_at,
                            upstream_status=result.upstream_status,
                            upstream_finish_reason=result.upstream_finish_reason)


def retry_after_seconds(values, *, now=None):
    """Normalize one header value; duplicates are not a trustworthy wait hint."""
    if len(values) != 1:
        return None
    value = values[0].strip()
    if re.fullmatch(r'[0-9]+', value):
        # A huge valid integer must never overflow into an ignored hint and
        # cause an early retry. Saturation is far beyond any request deadline.
        delay = float(value)
        return delay if math.isfinite(delay) else sys.float_info.max
    # parsedate_to_datetime accepts trailing junk and numeric time zones;
    # HTTP-date only admits these three wire forms (all represent UTC).
    if not re.fullmatch(
        r'(?:[A-Za-z]{3}, [0-9]{2} [A-Za-z]{3} [0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT'
        r'|[A-Za-z]+, [0-9]{2}-[A-Za-z]{3}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT'
        r'|[A-Za-z]{3} [A-Za-z]{3} [ 0-9][0-9] [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4})', value):
        return None
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0.0, (date - (now or datetime.now(timezone.utc))).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def error_for_status(status: int, headers=None) -> ProviderCallError:
    if status == http_status.HTTP_401_UNAUTHORIZED:
        code, translated = 'provider_auth_error', http_status.HTTP_502_BAD_GATEWAY
    elif status == http_status.HTTP_402_PAYMENT_REQUIRED:
        code, translated = 'provider_quota_exceeded', http_status.HTTP_503_SERVICE_UNAVAILABLE
    elif status == http_status.HTTP_429_TOO_MANY_REQUESTS:
        code, translated = 'provider_rate_limited', http_status.HTTP_503_SERVICE_UNAVAILABLE
    elif status in {http_status.HTTP_400_BAD_REQUEST, http_status.HTTP_404_NOT_FOUND, http_status.HTTP_422_UNPROCESSABLE_CONTENT}:
        code, translated = 'provider_rejected_request', http_status.HTTP_502_BAD_GATEWAY
    elif status >= http_status.HTTP_500_INTERNAL_SERVER_ERROR:
        code, translated = 'provider_unavailable', http_status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        code, translated = 'provider_error', http_status.HTTP_502_BAD_GATEWAY
    values = [] if headers is None else headers.get_list('retry-after')
    return ProviderCallError(code=code, status_code=translated,
                            transient=status in {500, 502, 503, 504},
                            upstream_http_status=status,
                            retry_after_seconds=retry_after_seconds(values))
