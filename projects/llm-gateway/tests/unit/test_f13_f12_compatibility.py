"""Restore an actual F12-captured pending snapshot, not a current-shape fixture."""
from dataclasses import replace
from pathlib import Path

import pytest

from f12_cases import FixedCounter, request, service
from services.streaming_manager import StreamingManager
from services.token_budget import TokenBudget
from utils.json_codec import loads


CAPTURED = Path(__file__).resolve().parents[1] / 'fixtures/f12_pending_v1.json'


def pending(*, kind='exact'):
    counter = FixedCounter(100, 'review-counter-v1')
    counter.kind = kind
    gateway, trace, primary, backup, traces = service(budget=TokenBudget({'deepseek_v41': counter}))
    manager = StreamingManager(None, traces, gateway.model_registry, gateway.token_budget,
        gateway.providers, None, set(), awaiting_persistence=lambda request_id: False,
        limiter=gateway.limiter)
    row = dict(input_snapshot=CAPTURED.read_text(), requested_model='general-primary')
    return gateway, trace, primary, backup, traces, manager, row


def assert_no_calls(gateway, primary, backup, traces):
    assert primary.calls == backup.calls == traces.attempts == []
    assert gateway.limiter.snapshot() == {'active_requests': 0, 'agents': {}}


@pytest.mark.parametrize('kind', ['exact', 'estimated'])
def test_actual_f12_pending_restores_without_provider_calls(kind):
    gateway, trace, primary, backup, traces, manager, row = pending(kind=kind)
    restored = manager._restore(row, trace)
    assert [item.model_name for item, _ in restored.candidates] == ['chat-a', 'responses-b']
    assert restored.snapshot['routing'] == loads(row['input_snapshot'])['routing']
    assert_no_calls(gateway, primary, backup, traces)


def test_default_positive_minimum_keeps_the_original_endpoint_snapshot_shape():
    gateway, trace, primary, backup, traces, _, row = pending()
    snapshot = gateway.prepare_stream(request(streaming=True), trace).snapshot
    old = loads(row['input_snapshot'])
    assert snapshot['routing_configuration'] == old['routing_configuration']
    assert snapshot['routing']['config_fingerprint'] == old['routing']['config_fingerprint']
    assert snapshot['model'] == old['model']
    assert [entry['model'] for entry in snapshot['candidates']] == [entry['model'] for entry in old['candidates']]
    assert_no_calls(gateway, primary, backup, traces)


@pytest.mark.parametrize('field,value', [
    ('min_output_tokens', 16), ('enabled', False), ('max_output_tokens', 4095),
])
def test_actual_endpoint_changes_still_reject_actual_f12_pending(field, value):
    gateway, trace, primary, backup, traces, manager, row = pending()
    endpoints = dict(manager.registry.endpoints)
    endpoints['chat-a'] = endpoints['chat-a'].model_copy(update={field: value})
    manager.registry = replace(manager.registry, endpoints=endpoints)
    with pytest.raises(ValueError, match='changed routing configuration'):
        manager._restore(row, trace)
    assert_no_calls(gateway, primary, backup, traces)


def test_nondefault_minimum_remains_in_every_endpoint_snapshot():
    gateway, trace, primary, backup, traces, _, _ = pending()
    endpoints = {name: endpoint.model_copy(update={'min_output_tokens': 16})
                 for name, endpoint in gateway.model_registry.endpoints.items()}
    definition = replace(gateway.model_registry, endpoints=endpoints)
    from services.routing import Router
    router = Router(definition, gateway.prompt_registry, gateway.token_budget)
    snapshot = router.route(request(streaming=True), trace, streaming=True).snapshot()
    assert snapshot['model']['min_output_tokens'] == 16
    assert all(entry['model']['min_output_tokens'] == 16 for entry in snapshot['candidates'])
    assert all(endpoint['min_output_tokens'] == 16
               for endpoint in snapshot['routing_configuration']['endpoints'].values())
    assert_no_calls(gateway, primary, backup, traces)
