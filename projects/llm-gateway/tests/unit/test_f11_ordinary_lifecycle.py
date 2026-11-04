"""Recovery uses one real permit; validation and errors cannot leak it."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from contracts import LLMRequest
from errors import GatewayError, ProviderCallError
from f7_cases import registry, body, SequenceProvider, SCHEMA, VALID
from providers.base import ProviderResult
from services.concurrency import ConcurrencyLimiter
from services.gateway import GatewayService
from services.models import ConcurrencyPolicy, RecoveryPolicy
from services.prompts import PromptRegistry, DEFAULT_TEMPLATES
from services.token_budget import TokenBudget
from services.trace import TraceStore


class CountedLimiter(ConcurrencyLimiter):
    def __init__(self, policy):
        super().__init__(policy)
        self.acquisitions = []

    def acquire(self, agent_id, request_id):
        self.acquisitions.append((agent_id, request_id))
        return super().acquire(agent_id, request_id)


def service(primary, policy=None):
    reg = replace(registry(policy=policy),
                  concurrency=ConcurrencyPolicy(max_requests=1, max_requests_per_agent=1))
    traces = TraceStore()
    limiter = CountedLimiter(reg.concurrency)
    gateway = GatewayService({'mock-main': primary, 'mock-backup': primary}, traces,
        PromptRegistry(DEFAULT_TEMPLATES), reg, TokenBudget.from_models(reg.endpoints.values()), limiter=limiter)
    trace = traces.start_request(str(uuid4()))
    trace.agent_id = 'unit-agent'
    return gateway, trace, limiter


@pytest.mark.parametrize('kind', ['connection', 'rate-limit', 'format-repair', 'output-growth', 'fallback'])
async def test_recovery_acquires_once_for_every_attempt(kind):
    success = ProviderResult(text='A123 充电器')
    first = ProviderCallError(code='provider_connection_error')
    changes, policy = {}, None
    if kind == 'rate-limit':
        first = ProviderCallError(code='provider_rate_limited', retry_after_seconds=.001)
    elif kind == 'format-repair':
        first, success = ProviderResult(text='not JSON'), ProviderResult(text=VALID)
        changes['response_schema'] = SCHEMA
    elif kind == 'output-growth':
        first = ProviderResult(text='partial', finish_reason='length')
        changes.update(initial_output_tokens=32, max_output_tokens=128)
    elif kind == 'fallback':
        policy = RecoveryPolicy(max_attempts=2, max_attempts_per_endpoint=1,
                                base_delay_seconds=0.0, max_delay_seconds=0.0)
    observations = []
    provider = SequenceProvider([first, success], before=lambda: observations.append(limiter.snapshot()))
    gateway, trace, limiter = service(provider, policy=policy)
    result, attempt = await gateway._complete(LLMRequest.model_validate(body(**changes)), trace)
    assert len(limiter.acquisitions) == 1 and len(provider.calls) == 2
    assert observations == [{'active_requests': 1, 'agents': {'unit-agent': 1}}] * 2
    assert limiter.snapshot() == {'active_requests': 0, 'agents': {}}
    assert result.text == success.text
    assert attempt.model == ('general-backup' if kind == 'fallback' else 'general-primary')


@pytest.mark.parametrize('failure', ['provider', 'unexpected', 'cancel'])
async def test_unrecoverable_call_exit_releases_capacity(failure):
    exc = {'provider': ProviderCallError(code='provider_auth_error'),
           'unexpected': RuntimeError('private-test-failure'),
           'cancel': asyncio.CancelledError()}[failure]
    provider = SequenceProvider([exc])
    gateway, trace, limiter = service(provider)
    with pytest.raises(type(exc)):
        await gateway._complete(LLMRequest.model_validate(body()), trace)
    assert len(provider.calls) == 1
    assert limiter.snapshot() == {'active_requests': 0, 'agents': {}}


@pytest.mark.parametrize('invalid', ['model', 'schema', 'budget'])
async def test_deterministic_validation_precedes_full_capacity(invalid):
    provider = SequenceProvider([])
    gateway, trace, limiter = service(provider)
    occupied = limiter.acquire('other-agent', 'other-request')
    changes = {'model': 'missing-model'} if invalid == 'model' else (
        {'response_schema': {'type': 'array'}} if invalid == 'schema' else {'max_output_tokens': 1000000})
    try:
        with pytest.raises(GatewayError) as caught:
            await gateway._complete(LLMRequest.model_validate(body(**changes)), trace)
        assert caught.value.code != 'concurrency_limited'
        assert provider.calls == [] and len(limiter.acquisitions) == 1
        assert limiter.snapshot()['active_requests'] == 1
    finally:
        occupied.release()


def test_factory_shares_limiter_across_services_but_not_applications(tmp_path):
    from main import create_app
    from api.dependencies import get_gateway
    from settings import Settings
    reg = replace(registry(), concurrency=ConcurrencyPolicy(max_requests=2, max_requests_per_agent=1))
    first = create_app(settings=Settings(env='test', _env_file=None), model_registry=reg, database=object(), config_dir=tmp_path)
    second = create_app(settings=Settings(env='test', _env_file=None), model_registry=reg, database=object(), config_dir=tmp_path)
    a = get_gateway(SimpleNamespace(app=first))
    b = get_gateway(SimpleNamespace(app=first))
    assert a.limiter is b.limiter is first.state.streaming_manager.limiter
    permit = a.limiter.acquire('a', 'request-a')
    try:
        assert second.state.concurrency_limiter.snapshot()['active_requests'] == 0
        with pytest.raises(GatewayError):
            b.limiter.acquire('a', 'request-b')
    finally:
        permit.release()
