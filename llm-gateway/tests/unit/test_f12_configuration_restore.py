"""T19: exact persisted routing configuration is checked before any execution."""
from dataclasses import replace
from decimal import Decimal

import pytest

from f12_cases import FixedCounter, GROUP, registry, request, service
from services.models import ModelGroupConfig, ProviderConfig, RecoveryPolicy
from services.streaming_manager import StreamingManager
from services.token_budget import TokenBudget
from utils.json_codec import dumps, loads


def frozen_request():
    """Use real connection definitions but injected, never-executed Providers."""
    definition = registry(
        policy=RecoveryPolicy(),
        extra_groups={'unrelated': {'candidates': ['chat-a']}},
    )
    definition = replace(definition, providers={
        'mock-main': ProviderConfig(adapter='deepseek', api_protocol='chat_completions',
                                    base_url='https://primary.invalid', credential_ref='fake-primary-key'),
        'mock-backup': ProviderConfig(adapter='openai_compatible', api_protocol='responses',
                                      base_url='https://backup.invalid', credential_ref='fake-backup-key'),
    })
    budget = TokenBudget({'deepseek_v41': FixedCounter(100, 'counter-v1')})
    gateway, trace, primary, backup, traces = service(definition, budget=budget)
    prepared = gateway.prepare_stream(request(streaming=True), trace)
    encoded = dumps(prepared.snapshot)
    # This is the production persistence codec, not stdlib JSON float parsing.
    decoded = loads(encoded)
    assert decoded['recovery']['base_delay_seconds'] == Decimal('0.1')
    assert type(decoded['recovery']['base_delay_seconds']) is Decimal
    row = {'input_snapshot': encoded, 'requested_model': 'general-primary'}
    return gateway, trace, primary, backup, traces, row


def restore(gateway, trace, traces, row, *, definition=None, budget=None):
    manager = StreamingManager(None, traces, definition or gateway.model_registry,
                               budget or gateway.token_budget, gateway.providers,
                               None, set(), awaiting_persistence=lambda request_id: False,
                               limiter=gateway.limiter)
    return manager._restore(row, trace)


def assert_not_entered(primary, backup, traces, gateway):
    assert primary.calls == []
    assert backup.calls == []
    assert traces.attempts == []
    assert gateway.limiter.snapshot() == {'active_requests': 0, 'agents': {}}


def test_unchanged_default_recovery_configuration_restores_after_exact_json_roundtrip():
    gateway, trace, primary, backup, traces, row = frozen_request()
    restored = restore(gateway, trace, traces, row)
    assert restored.prepared.model_name == 'chat-a'
    assert [candidate.model_name for candidate, _ in restored.candidates] == ['chat-a', 'responses-b']
    assert restored.recovery.policy == RecoveryPolicy()
    assert restored.snapshot['routing'] == loads(row['input_snapshot'])['routing']
    assert_not_entered(primary, backup, traces, gateway)


@pytest.mark.parametrize('provider', ['mock-main', 'mock-backup'])
@pytest.mark.parametrize('field,value', [
    ('base_url', 'https://changed.invalid'),
    ('credential_ref', 'fake-rotated-reference'),
])
def test_related_provider_definition_change_rejects_before_any_call(provider, field, value):
    gateway, trace, primary, backup, traces, row = frozen_request()
    before = gateway.model_registry
    providers = dict(before.providers)
    providers[provider] = providers[provider].model_copy(update={field: value})
    changed = replace(before, providers=providers)
    with pytest.raises(ValueError, match='changed routing configuration'):
        restore(gateway, trace, traces, row, definition=changed)
    assert_not_entered(primary, backup, traces, gateway)


def test_endpoint_provider_reference_change_rejects_with_new_registry_still_valid():
    gateway, trace, primary, backup, traces, row = frozen_request()
    before = gateway.model_registry
    providers = dict(before.providers)
    providers['new-primary'] = before.providers['mock-main']
    endpoints = dict(before.endpoints)
    endpoints['chat-a'] = endpoints['chat-a'].model_copy(update={'provider': 'new-primary'})
    changed = replace(before, providers=providers, endpoints=endpoints)
    with pytest.raises(ValueError, match='changed routing configuration'):
        restore(gateway, trace, traces, row, definition=changed)
    assert_not_entered(primary, backup, traces, gateway)


def test_counter_version_change_rejects_before_any_call():
    gateway, trace, primary, backup, traces, row = frozen_request()
    changed_budget = TokenBudget({'deepseek_v41': FixedCounter(100, 'counter-v2')})
    with pytest.raises(ValueError, match='changed routing configuration'):
        restore(gateway, trace, traces, row, budget=changed_budget)
    assert_not_entered(primary, backup, traces, gateway)


def test_removed_endpoint_and_updated_group_rejects_without_invalid_registry_setup():
    gateway, trace, primary, backup, traces, row = frozen_request()
    before = gateway.model_registry
    endpoints = {'chat-a': before.endpoints['chat-a']}
    groups = dict(before.model_groups)
    groups['general-primary'] = ModelGroupConfig(**(GROUP | {'candidates': ['chat-a']}))
    changed = replace(before, endpoints=endpoints, model_groups=groups)
    with pytest.raises(ValueError, match='changed routing configuration'):
        restore(gateway, trace, traces, row, definition=changed)
    assert_not_entered(primary, backup, traces, gateway)


def test_unrelated_group_change_does_not_invalidate_frozen_request():
    gateway, trace, primary, backup, traces, row = frozen_request()
    before = gateway.model_registry
    groups = dict(before.model_groups)
    groups['unrelated'] = groups['unrelated'].model_copy(update={
        'default_output_tokens': 64, 'candidates': ('responses-b', 'chat-a'),
    })
    changed = replace(before, model_groups=groups)
    restored = restore(gateway, trace, traces, row, definition=changed)
    assert restored.prepared.model_name == 'chat-a'
    assert restored.snapshot['routing']['config_fingerprint'] == loads(row['input_snapshot'])['routing']['config_fingerprint']
    assert_not_entered(primary, backup, traces, gateway)
