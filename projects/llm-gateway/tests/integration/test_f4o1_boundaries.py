"""Fixed contract boundaries, independent of the implementation's constants."""
import json

import pytest
from fastapi.testclient import TestClient

from contracts import PromptSelection, UserMessage
from f3_cases import MOCK_USAGE, CountingFixture, make_app, primary_provider
from f4_cases import make_app as make_structured_app
from providers.mock import MockProvider
from services.prompts import build_messages, fingerprint_messages


def submit(body, expected_status, expected_code):
    structured = 'response_schema' in body
    app = make_structured_app(MockProvider(text='{}')) if structured else make_app(counter=CountingFixture())
    provider = primary_provider(app)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post('/v1/llm', json=body)
    assert response.status_code == expected_status
    result = response.json()
    assert set(result) == {'success', 'code', 'data', 'message'}
    assert result['code'] == expected_code
    trace = app.state.traces.requests[response.headers['X-Request-ID']]
    attempts = [a for a in app.state.traces.attempts if a.request_id == trace.request_id]
    expected_calls = int(expected_status == 200)
    assert len(provider.calls) == trace.attempts == len(attempts) == expected_calls
    assert trace.status == ('completed' if expected_calls else 'failed')
    assert trace.error_code == (None if expected_calls else expected_code)
    if expected_calls:
        expected = {'text': '{}', 'parsed': {}, 'usage': MOCK_USAGE} if structured else {'text': 'MOCK_REPLY', 'usage': MOCK_USAGE}
        assert result['data'] == expected
        assert attempts[0].status == 'completed'
        assert trace.prompt_fingerprint == fingerprint_messages(provider.calls[0][1])
        return provider.calls[0][1][-1].content
    assert result['data'] is None


@pytest.mark.parametrize('content', ['问' * 19999, '问' * 20000, '问' * 20001, '', ' \n\t'])
def test_raw_user_http_boundary(payload, content):
    payload['messages'][0]['content'] = content
    valid = bool(content.strip()) and len(content) <= 20000
    actual = submit(payload, 200 if valid else 422, 'ok' if valid else 'invalid_request')
    if valid:
        assert actual == content


@pytest.mark.parametrize('version', ['1.0.0', '1.1.0', '1.2.0'])
@pytest.mark.parametrize('size', [9999, 10000, 10001])
def test_context_limit_for_every_template_version(payload, version, size):
    context = '中' * size
    payload['prompt'] = {'name': 'context_answer', 'version': version, 'variables': {'context': context}}
    valid = size <= 10000
    actual = submit(payload, 200 if valid else 400, 'ok' if valid else 'invalid_prompt_variables')
    if valid:
        assert context in actual


@pytest.mark.parametrize('version', ['1.0.0', '1.1.0', '1.2.0'])
@pytest.mark.parametrize('size', [19999, 20000, 20001])
def test_rendered_user_limit_includes_template_and_contract(payload, version, size):
    payload['prompt'] = {'name': 'context_answer', 'version': version, 'variables': {'context': '中'}}
    overhead = len(build_messages(PromptSelection.model_validate(payload['prompt']),
                                 [UserMessage(role='user', content='问')])[-1].content) - 1
    payload['messages'][0]['content'] = '问' * (size - overhead)
    valid = size <= 20000
    actual = submit(payload, 200 if valid else 400, 'ok' if valid else 'prompt_too_long')
    if valid:
        assert len(actual) == size


@pytest.mark.parametrize('kind,values', [
    ('bytes', (16383, 16384, 16385)),
    ('raw_depth', (31, 32, 33)),
    ('expanded_depth', (7, 8, 9)),
])
@pytest.mark.parametrize('index', [0, 1, 2])
def test_schema_resource_boundaries_reach_the_http_boundary(payload, kind, values, index):
    value = values[index]
    schema = {'type': 'object'}
    if kind == 'bytes':
        schema['description'] = '中'
        overhead = len(json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
        schema['description'] += 'a' * (value - overhead)
    elif kind == 'raw_depth':
        data = 0
        for _ in range(value - 1):
            data = [data]
        schema['default'] = data
    else:
        for _ in range(value - 1):
            schema = {'type': 'object', 'properties': {'x': schema}}
    payload['prompt'] = {'name': 'context_answer', 'version': '1.2.0', 'variables': {'context': '订单 A123'}}
    payload['response_schema'] = schema
    valid = index < 2
    submit(payload, 200 if valid else 400, 'ok' if valid else 'invalid_response_schema')
