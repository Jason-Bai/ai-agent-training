from copy import deepcopy
import json

import pytest

from tests.upstream_availability import classify_upstream_failure


def frame(payload, named=False):
    return ('event: ' + payload['type'] + '\n' if named else '') + 'data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n'


def evidence(protocol='responses', named=False, text='订单'):
    if protocol == 'responses':
        raw = frame({'type': 'response.created', 'response': {'object': 'response', 'status': 'in_progress', 'model': 'model'}}, named)
        if text:
            raw += frame({'type': 'response.output_text.delta', 'delta': text}, named)
    else:
        raw = frame({'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})
        if text:
            raw += frame({'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'content': text}, 'finish_reason': None}]})
    raw += ': keep-alive\n\n'
    trace = {'request_id': 'r1', 'operation': 'streaming', 'status': 'failed', 'error_code': 'request_timeout', 'error_stage': 'provider', 'timeout_seconds': 30, 'delivery_error_code': None,
        'cancellation': {'requested_at': None, 'cleanup_error_code': None}, 'timing': {'request_duration_ms': 30010},
        'attempts': [{'attempt_no': 1, 'api_protocol': protocol, 'status': 'failed', 'error_code': 'request_timeout', 'provider_error_code': None, 'cleanup_error_code': None, 'provider_entered': True, 'upstream_status': 'in_progress' if protocol == 'responses' else None, 'finish_reason': None, 'upstream_finish_reason': None, 'usage': None}],
        'usage': {'known_subtotal': None, 'unknown_attempts': [1]}}
    obs = {'request_id': 'r1', 'attempts': [{'attempt_no': 1, 'protocol': protocol, 'started_ms': 0, 'ended_ms': 29950, 'http_status': 200, 'http_headers_ms': 100, 'read_closed': True, 'read_cancelled': True, 'raw_text': raw, 'typed_text': text, 'provider_error_code': None, 'upstream_http_status': None, 'transient': None}],
        'drained': {'active_requests': 0, 'agent_requests': {}, 'active_generations': 0, 'cleanup_tasks': 0, 'cancellations': 0, 'subscriptions': 0}, 'accounting_verified': True, 'lifecycle_verified': True}
    return trace, obs


@pytest.mark.parametrize('protocol,named', [('responses', False), ('responses', True), ('chat_completions', False)])
@pytest.mark.parametrize('text', ['订单', ''])
def test_complete_stall(protocol, named, text):
    trace, obs = evidence(protocol, named, text)
    result = classify_upstream_failure(trace, obs, downstream_text=text)
    assert result is not None
    assert result['kind'] == 'upstream_stream_stall'
    assert result['request_id'] == 'r1'


def replace(target, path, value):
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


@pytest.mark.parametrize('side,path,value', [
    ('t', ['status'], 'completed'), ('t', ['operation'], 'provider_probe'), ('t', ['error_stage'], 'finish_attempt'), ('t', ['error_code'], 'internal_error'),
    ('t', ['delivery_error_code'], 'storage_unavailable'), ('t', ['attempts', 0, 'cleanup_error_code'], 'provider_cleanup_failed'),
    ('t', ['attempts', 0, 'provider_error_code'], 'provider_invalid_response'), ('t', ['attempts', 0, 'provider_entered'], None),
    ('t', ['attempts', 0, 'status'], 'completed'), ('t', ['attempts', 0, 'upstream_status'], 'completed'), ('t', ['attempts', 0, 'upstream_finish_reason'], 'stop'),
    ('t', ['attempts', 0, 'usage'], {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}), ('t', ['usage', 'unknown_attempts'], []),
    ('t', ['usage', 'known_subtotal'], {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}),
    ('t', ['cancellation', 'requested_at'], '2026-11-03T00:00:00Z'), ('t', ['cancellation', 'cleanup_error_code'], 'cancel_cleanup_failed'),
    ('t', ['timeout_seconds'], 60), ('t', ['timing', 'request_duration_ms'], 200), ('o', ['request_id'], 'other'),
    ('o', ['accounting_verified'], False), ('o', ['lifecycle_verified'], False), ('o', ['drained', 'active_requests'], 1),
    ('o', ['drained', 'agent_requests'], {'agent': 1}), ('o', ['drained', 'cleanup_tasks'], 1), ('o', ['drained', 'active_generations'], 1),
    ('o', ['drained', 'cancellations'], 1), ('o', ['drained', 'subscriptions'], 1),
    ('o', ['attempts', 0, 'attempt_no'], 2), ('o', ['attempts', 0, 'protocol'], 'chat_completions'), ('o', ['attempts', 0, 'http_status'], 504),
    ('o', ['attempts', 0, 'http_headers_ms'], 31000), ('o', ['attempts', 0, 'ended_ms'], 1000),
    ('o', ['attempts', 0, 'read_closed'], False), ('o', ['attempts', 0, 'read_cancelled'], False), ('o', ['attempts', 0, 'typed_text'], ''),
    ('o', ['attempts', 0, 'provider_error_code'], 'provider_invalid_response'), ('o', ['attempts', 0, 'raw_text'], ''),
    ('o', ['attempts', 0, 'raw_text'], ': keep-alive\n\n'),
])
def test_incomplete_or_local_failure_stays_blocked(side, path, value):
    trace, obs = evidence()
    replace(trace if side == 't' else obs, path, value)
    assert classify_upstream_failure(trace, obs) is None


@pytest.mark.parametrize('extra', [
    frame({'type': 'response.completed', 'response': {'status': 'completed'}}),
    frame({'type': 'response.incomplete', 'response': {'status': 'incomplete'}}),
    frame({'type': 'response.failed', 'response': {'status': 'failed'}}), frame({'type': 'response.output_text.done', 'text': '订单'}),
    frame({'type': 'response.output_text.delta', 'delta': '漏读'}), frame({'type': 'response.output_text.delta', 'delta': 1}),
    frame({'type': 'response.output_text.delta', 'delta': '', 'usage': {}}), 'data: [DONE]\n\n', 'data: {broken}\n\n',
    'data: {"type": "response.completed"}', 'event: response.completed\ndata: {"type":"response.output_text.delta","delta":""}\n\n', frame({'type': 'unknown.event'}),
])
def test_every_raw_frame_checked(extra):
    trace, obs = evidence()
    obs['attempts'][0]['raw_text'] += extra
    assert classify_upstream_failure(trace, obs) is None


def test_downstream_must_match():
    trace, obs = evidence()
    assert classify_upstream_failure(trace, obs, downstream_text='') is None


def test_multiple_attempts_not_ignored():
    trace, obs = evidence()
    trace['attempts'].append(deepcopy(trace['attempts'][0]))
    obs['attempts'].append(deepcopy(obs['attempts'][0]))
    assert classify_upstream_failure(trace, obs) is None


def http_evidence(status):
    trace, obs = evidence()
    code = 'provider_rate_limited' if status == 429 else 'provider_unavailable'
    trace.update(operation='completion', error_code=code)
    trace['attempts'][0].update(error_code=code, provider_error_code=None, upstream_status=None)
    obs['attempts'][0].update(http_status=status, ended_ms=250, http_headers_ms=200, raw_text='{"error":{"message":"unavailable"}}', typed_text='', provider_error_code=code, upstream_http_status=status, transient=status != 429, read_cancelled=False)
    return trace, obs


@pytest.mark.parametrize('status', [429, 500, 502, 503, 504])
def test_http_error_metadata_corresponds(status):
    trace, obs = http_evidence(status)
    result = classify_upstream_failure(trace, obs, downstream_text='')
    assert result is not None
    assert result['kind'] == 'upstream_http_error'
    assert result['upstream_http_status'] == status


@pytest.mark.parametrize('path,value', [(['http_status'], 401), (['upstream_http_status'], None), (['provider_error_code'], 'provider_timeout'), (['transient'], False), (['typed_text'], 'unexpected'), (['read_closed'], False), (['raw_text'], '{"status":"completed","output":[]}'), (['raw_text'], 'data: {"type":"response.completed"}\n\n')])
def test_http_code_alone_insufficient(path, value):
    trace, obs = http_evidence(503)
    replace(obs['attempts'][0], path, value)
    assert classify_upstream_failure(trace, obs) is None


@pytest.mark.parametrize('trace,obs', [({}, {}), (None, None), ({'attempts': None}, {})])
def test_missing_evidence(trace, obs):
    assert classify_upstream_failure(trace, obs) is None


def test_chat_usage_null_is_not_reported_usage():
    trace, obs = evidence('chat_completions')
    obs['attempts'][0]['raw_text'] = obs['attempts'][0]['raw_text'].replace('"object":', '"usage": null, "object":')
    assert classify_upstream_failure(trace, obs) is not None


@pytest.mark.parametrize('payload', [
    {'type': 'response.output_item.added', 'item': {'type': 'message', 'status': 'in_progress', 'content': [{'type': 'output_text', 'text': '漏读'}]}},
    {'type': 'response.content_part.added', 'part': {'type': 'output_text', 'text': '漏读'}},
    {'type': 'response.created', 'response': {'object': 'response', 'status': 'in_progress', 'model': 'model', 'output': [{'text': '漏读'}]}},
])
def test_hidden_content_cannot_evade_raw_correspondence(payload):
    trace, obs = evidence()
    obs['attempts'][0]['raw_text'] += frame(payload)
    assert classify_upstream_failure(trace, obs) is None


def test_sse_multiline_json_and_unicode_escape_match_decoded_deltas():
    trace, obs = evidence()
    obs['attempts'][0]['raw_text'] = obs['attempts'][0]['raw_text'].replace('"delta": "订单"}', '"delta":\ndata: "\\u8ba2\\u5355"}')
    assert classify_upstream_failure(trace, obs, downstream_text='订单') is not None


@pytest.mark.parametrize('extra', [
    frame({'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}),
    frame({'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': None}], 'usage': {'total_tokens': 2}}),
    'data: [DONE]\n\n',
])
def test_chat_raw_terminal_or_usage_blocks_timeout_exemption(extra):
    trace, obs = evidence('chat_completions')
    obs['attempts'][0]['raw_text'] += extra
    assert classify_upstream_failure(trace, obs) is None


def test_timeout_is_derived_from_trace_configuration():
    trace, obs = evidence()
    trace['timeout_seconds'] = 45
    trace['timing']['request_duration_ms'] = 45010
    obs['attempts'][0]['ended_ms'] = 44950
    assert classify_upstream_failure(trace, obs) is not None


def http_chain(statuses):
    trace, obs = http_evidence(statuses[-1])
    trace['attempts'], obs['attempts'] = [], []
    for number, status in enumerate(statuses, 1):
        one_trace, one_obs = http_evidence(status)
        attempt, wire = one_trace['attempts'][0], one_obs['attempts'][0]
        attempt.update(attempt_no=number, reason='initial' if number == 1 else 'network_retry')
        wire['attempt_no'] = number
        trace['attempts'].append(attempt)
        obs['attempts'].append(wire)
    trace['usage']['unknown_attempts'] = list(range(1, len(statuses) + 1))
    return trace, obs


@pytest.mark.parametrize('statuses', [[503, 503], [503, 502, 504], [429, 503], [503, 429]])
def test_certifies_complete_multi_attempt_http_failure_chain(statuses):
    trace, obs = http_chain(statuses)
    result = classify_upstream_failure(trace, obs, downstream_text='')
    assert result is not None
    assert result['kind'] == 'upstream_http_error'
    assert result['attempt_count'] == len(statuses)
    assert [item['upstream_http_status'] for item in result['attempts']] == statuses
    assert [item['attempt_no'] for item in result['attempts']] == list(range(1, len(statuses) + 1))


@pytest.mark.parametrize('side,path,value', [
    ('t', ['attempts', 0, 'error_code'], 'request_timeout'),
    ('t', ['attempts', 0, 'provider_error_code'], 'provider_invalid_response'),
    ('t', ['attempts', 0, 'usage'], {'total_tokens': 0}),
    ('t', ['attempts', 0, 'cleanup_error_code'], 'provider_cleanup_failed'),
    ('t', ['attempts', 0, 'status'], 'completed'),
    ('t', ['attempts', 1, 'attempt_no'], 1),
    ('t', ['usage', 'unknown_attempts'], [2]),
    ('t', ['error_code'], 'request_timeout'),
    ('o', ['attempts', 0, 'read_closed'], False),
    ('o', ['attempts', 0, 'http_status'], 200),
    ('o', ['attempts', 0, 'upstream_http_status'], 502),
    ('o', ['attempts', 0, 'typed_text'], '漏发'),
    ('o', ['attempts', 0, 'provider_error_code'], 'provider_timeout'),
    ('o', ['attempts', 1, 'attempt_no'], 3),
    ('o', ['attempts', 0, 'raw_text'], '{"status":"completed","output":[]}'),
])
def test_incomplete_chain_is_never_certified(side, path, value):
    trace, obs = http_chain([503, 503])
    replace(trace if side == 't' else obs, path, value)
    assert classify_upstream_failure(trace, obs) is None


def test_http_chain_requires_same_attempt_count():
    trace, obs = http_chain([503, 503])
    obs['attempts'].pop()
    assert classify_upstream_failure(trace, obs) is None


def test_http_chain_rejects_more_than_f7_attempt_bound():
    trace, obs = http_chain([503] * 4)
    assert classify_upstream_failure(trace, obs) is None
