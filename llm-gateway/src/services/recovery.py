"""Request-local recovery decisions; callers own I/O, persistence and cancellation."""
from dataclasses import dataclass, field
from random import random
from urllib.parse import urlsplit

from services.models import RecoveryPolicy


@dataclass
class RecoveryState:
    policy: RecoveryPolicy
    initial_output: int
    max_output: int
    total: int = field(default=0, init=False)
    counts: dict[str, int] = field(default_factory=dict, init=False)
    repair_used: bool = field(default=False, init=False)
    stop_reason: str | None = field(default=None, init=False)
    current_output: int = field(init=False)
    waits: dict[tuple, float] = field(default_factory=dict, init=False)

    def __post_init__(self):
        if not 0 < self.initial_output <= self.max_output:
            raise ValueError('输出预算关系不合法')
        self.current_output = self.initial_output

    def available(self, model):
        return (self.total < self.policy.max_attempts
                and self.counts.get(model, 0) < self.policy.max_attempts_per_endpoint)

    def start(self, model):
        if not self.available(model):
            raise ValueError('模型调用尝试额度已耗尽')
        self.total += 1
        self.counts[model] = self.counts.get(model, 0) + 1

    def decide(self, error, model, *, has_content=False, structured=False):
        self.stop_reason = None
        if has_content:
            self.stop_reason = 'content_committed'
        elif self.repair_used:
            self.stop_reason = 'repair_finished'
        elif self.total >= self.policy.max_attempts:
            self.stop_reason = 'attempts_exhausted'
        if self.stop_reason is not None:
            return 'stop'
        code = getattr(error, 'code', None)
        if code == 'output_truncated':
            if self.current_output < self.max_output and self.available(model):
                self.current_output = self.max_output
                return 'grow'
            self.stop_reason = ('output_limit_reached' if self.current_output >= self.max_output
                                else 'model_attempts_exhausted')
            return 'stop'
        if code in {'invalid_json', 'schema_validation_failed'}:
            if structured and self.available(model):
                self.repair_used = True
                return 'repair'
            self.stop_reason = ('model_attempts_exhausted' if structured
                                else 'format_repair_not_applicable')
            return 'stop'
        transient = (getattr(error, 'transient', False) is True
                     or getattr(error, 'upstream_http_status', None) in {500, 502, 503, 504}
                     or getattr(error, 'finish_reason', None) in {'aborted', 'insufficient_system_resource'})
        if code in {'provider_connection_error', 'provider_timeout', 'provider_rate_limited'} or (
                code == 'provider_unavailable' and transient):
            return 'retry' if self.available(model) else 'fallback'
        self.stop_reason = 'nonrecoverable_error'
        return 'stop'

    def delay(self, error, model, *, random_value=None):
        ceiling = min(self.policy.max_delay_seconds,
                      self.policy.base_delay_seconds * 2 ** max(0, self.counts.get(model, 0) - 1))
        sample = random() if random_value is None else random_value
        upstream = getattr(error, 'retry_after_seconds', None)
        return max(ceiling * sample, upstream if upstream is not None else 0)

    @staticmethod
    def wait_domain(provider):
        if not provider.base_url:
            # Mock has no upstream Retry-After domain or shared remote quota.
            return ('mock', id(provider))
        parsed = urlsplit(provider.base_url)
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        return (parsed.scheme.lower(), parsed.hostname.lower(), port, provider.credential_ref)

    def note_wait(self, provider, delay, now):
        domain = self.wait_domain(provider)
        self.waits[domain] = max(self.waits.get(domain, now), now + delay)

    def wait_remaining(self, provider, now):
        return max(0, self.waits.get(self.wait_domain(provider), now) - now)
