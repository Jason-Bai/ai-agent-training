"""F5 HTTP boundary: the real Gateway runs; only its Provider is replaced."""
import asyncio
import json
import httpx

from legacy_identity import ASGI_AUTH
import pytest
from fastapi.testclient import TestClient

from f3_cases import MOCK_USAGE, make_app, primary_provider


def request_body(**changes):
    return dict(model='general-primary', prompt={'name': 'basic_text', 'version': '1.0.0'},
                messages=[{'role': 'user', 'content': '订单 A123 的商品是什么？'}], **changes)


def parse_events(text):
    result = []
    for frame in text.strip().split('\n\n'):
        lines = frame.split('\n')
        event = next(line for line in lines if line.startswith('event: '))
        data = next(line for line in lines if line.startswith('data: '))
        assert event.startswith('event: ') and data.startswith('data: ')
        result.append((event[7:], json.loads(data[6:])))
    return result


def post_then_subscribe(client, **kwargs):
    created = client.post('/v1/llm/stream', **kwargs)
    if created.status_code != 202:
        return created
    assert set(created.json()['data']) == {'request_id', 'status'}
    return client.get('/v1/llm/stream/' + created.json()['data']['request_id'] + '/events')


def test_f5_stream_route_uses_stream_not_complete():
    app = make_app()
    with TestClient(app) as client:
        response = post_then_subscribe(client, json=request_body())
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/event-stream')
    events = parse_events(response.text)
    assert [name for name, _ in events] == ['response.delta', 'response.completed']
    assert events[0][1]['delta'] == 'MOCK_REPLY'
    assert events[-1][1]['usage'] == MOCK_USAGE
    request_id = response.headers['x-request-id']
    assert all(data['request_id'] == request_id for _, data in events)
    assert app.state.traces.requests[request_id].status == 'completed'
    assert primary_provider(app).calls == []


@pytest.mark.parametrize('change,code', [
    ({'model': 'missing'}, 'unknown_model'),
    ({'prompt': {'name': 'missing', 'version': '1'}}, 'unknown_prompt_template'),
    ({'prompt': {'name': 'context_answer', 'version': '1.1.0'}}, 'missing_prompt_variable'),
    ({'prompt': {'name': 'basic_text', 'version': '1.0.0', 'variables': {'message': 'override'}}}, 'invalid_prompt_variables'),
    ({'max_output_tokens': 4097}, 'invalid_output_budget'),
    ({'timeout_seconds': 121}, 'invalid_timeout'),
])
def test_preflight_errors_have_zero_attempts(change, code):
    app = make_app()
    body = request_body()
    body.update(change)
    with TestClient(app) as client:
        response = post_then_subscribe(client, json=body)
    assert response.status_code == 400
    assert response.json() == {'success': False, 'code': code, 'data': None,
                               'message': response.json()['message']}
    trace = app.state.traces.requests[response.headers['x-request-id']]
    assert trace.status == 'failed' and trace.attempts == 0
    assert primary_provider(app).stream_calls == []
    assert trace.stream_started_at is None and trace.ttft_ms is None


@pytest.mark.parametrize('field', ['max_output_tokens', 'timeout_seconds'])
@pytest.mark.parametrize('value', [None, True, '1', 0, -1, 1.5])
def test_http_strict_budget_types(field, value):
    app = make_app()
    with TestClient(app) as client:
        response = post_then_subscribe(client, json=request_body(**{field: value}))
    assert response.status_code == 422 and response.json()['code'] == 'invalid_request'
    trace = app.state.traces.requests[response.headers['x-request-id']]
    assert trace.status == 'failed' and trace.attempts == 0
    assert primary_provider(app).stream_calls == []


def test_capability_false_and_preflight_error_order():
    app = make_app(changes={'general-primary': {'supports_streaming': False}})
    with TestClient(app) as client:
        response = post_then_subscribe(client, json=request_body(max_output_tokens=4097))
        normal = client.post('/v1/llm', json=request_body())
    assert response.status_code == 400 and response.json()['code'] == 'invalid_output_budget'
    assert normal.status_code == 200
    trace = app.state.traces.requests[response.headers['x-request-id']]
    assert trace.attempts == 0 and trace.prompt_fingerprint
    assert primary_provider(app).stream_calls == []


def test_openapi_stream_has_only_supported_request_fields():
    app = make_app()
    with TestClient(app) as client:
        response = client.get('/openapi.json')
    assert response.status_code == 200
    document = response.json()
    operation = document['paths']['/v1/llm/stream']['post']
    ref = operation['requestBody']['content']['application/json']['schema']['$ref'].split('/')[-1]
    schema = document['components']['schemas'][ref]
    assert set(schema['properties']) == {'model', 'prompt', 'messages', 'max_output_tokens', 'initial_output_tokens', 'timeout_seconds'}
    assert schema['additionalProperties'] is False
    assert set(operation['responses']['202']['content']) == {'application/json'}
    subscription = document['paths']['/v1/llm/stream/{request_id}/events']['get']
    assert set(subscription['responses']['200']['content']) == {'text/event-stream'}


@pytest.mark.parametrize('text', ['{"model":', '{"model":"one","model":"two"}'])
def test_invalid_and_duplicate_json_are_rejected_before_stream(text):
    app = make_app()
    with TestClient(app) as client:
        response = post_then_subscribe(client, content=text, headers={'content-type': 'application/json'})
    assert response.status_code == 422
    trace = app.state.traces.requests[response.headers['x-request-id']]
    assert trace.status == 'failed' and trace.attempts == 0


def test_unicode_newlines_and_sse_looking_text_are_escaped():
    from providers.mock import MockProvider
    text = '中文😀\r\n"quote"\\\nevent: response.completed\ndata: {}'
    app = make_app(primary=MockProvider(text=text))
    with TestClient(app) as client:
        response = post_then_subscribe(client, json=request_body(), headers={'x-request-id': 'caller-id'})
    events = parse_events(response.text)
    assert len(events) == 2 and events[0][1]['delta'] == text
    assert response.headers['x-request-id'] != 'caller-id'
    assert events[0][1]['request_id'] == response.headers['x-request-id']


@pytest.mark.parametrize('blocked', ['headers', 'response.delta', 'response.heartbeat',
                                    'response.completed', 'response.failed'])
async def test_actual_asgi_send_wait_is_inside_subscription_timeout(blocked, monkeypatch):
    from providers.base import ProviderStreamEvent
    from providers.mock import MockProvider
    from services.subscriptions import EventSubscription
    original = EventSubscription.__init__
    def short_budget(self, *args, **kwargs):
        kwargs.update(send_timeout_seconds=.04, heartbeat_seconds=.01)
        original(self, *args, **kwargs)
    monkeypatch.setattr(EventSubscription, '__init__', short_budget)
    never = asyncio.Event()
    async def wait_forever():
        await never.wait()
    script = ([wait_forever] if blocked == 'response.heartbeat' else
              [ProviderStreamEvent(status='failed', error_code='provider_auth_error')] if blocked == 'response.failed' else
              [ProviderStreamEvent(delta='正文'), ProviderStreamEvent(status='completed', upstream_terminal_received=True)])
    provider = MockProvider(stream_events=script)
    app = make_app(primary=provider)
    active_send = terminal_attempts = 0
    async def receive():
        await never.wait()
    async def send(message):
        nonlocal active_send, terminal_attempts
        active_send += 1
        assert active_send == 1
        try:
            name = 'headers' if message['type'] == 'http.response.start' else ''
            for line in message.get('body', b'').decode().splitlines():
                if line.startswith('event: '):
                    name = line[7:]
            terminal_attempts += name in {'response.failed', 'response.completed'}
            if name == blocked:
                await never.wait()
        finally:
            active_send -= 1
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            created = await client.post('/v1/llm/stream', json=request_body(timeout_seconds=1))
        assert created.status_code == 202
        request_id = created.json()['data']['request_id']
        path = '/v1/llm/stream/' + request_id + '/events'
        scope = {'type': 'http', 'method': 'GET', 'path': path, 'raw_path': path.encode(),
                 'root_path': '', 'scheme': 'http', 'query_string': b'', 'http_version': '1.1',
                 'headers': [ASGI_AUTH], 'server': ('test', 80), 'client': ('127.0.0.1', 1234),
                 'asgi': {'version': '3.0', 'spec_version': '2.4'}}
        await asyncio.wait_for(app(scope, receive, send), 3)
        trace = app.state.traces.requests[request_id]
        # Subscriber timeout never sets the generation result. Wait for its own result.
        for _ in range(150):
            if trace.status in {'completed', 'failed'}:
                break
            await asyncio.sleep(.02)
        expected = 'failed' if blocked in {'response.heartbeat', 'response.failed'} else 'completed'
        code = 'request_timeout' if blocked == 'response.heartbeat' else 'provider_auth_error' if blocked == 'response.failed' else None
        assert trace.status == expected and trace.error_code == code
        assert len(provider.stream_calls) == trace.attempts == 1
        assert terminal_attempts <= 1 and active_send == 0
        assert provider.stream_close_calls == 1


@pytest.mark.parametrize('length,status', [(20000, 200), (20001, 422)])
def test_raw_message_character_limit(length, status):
    app = make_app()
    body = request_body()
    body['messages'][0]['content'] = '字' * length
    with TestClient(app) as client:
        response = post_then_subscribe(client, json=body)
    assert response.status_code == status
    assert len(primary_provider(app).stream_calls) == (1 if status == 200 else 0)


@pytest.mark.parametrize('length,status', [(10000, 200), (10001, 400)])
def test_context_variable_character_limit(length, status):
    app = make_app()
    body = request_body()
    body['prompt'] = {'name': 'context_answer', 'version': '1.1.0', 'variables': {'context': '字' * length}}
    with TestClient(app) as client:
        response = post_then_subscribe(client, json=body)
    assert response.status_code == status
    assert len(primary_provider(app).stream_calls) == (1 if status == 200 else 0)
    if status == 200:
        messages = primary_provider(app).stream_calls[0][1]
        assert len(messages) == 6 and messages[0].role == 'system'


@pytest.mark.parametrize('rendered,status', [(20000, 200), (20001, 400)])
def test_rendered_user_limit_counts_the_whole_template(rendered, status):
    app = make_app()
    reference = '<user_message>\n\n</user_message>\n<untrusted_context>\nx\n</untrusted_context>'
    body = request_body()
    body['prompt'] = {'name': 'context_answer', 'version': '1.0.0', 'variables': {'context': 'x'}}
    body['messages'][0]['content'] = '字' * (rendered - len(reference))
    with TestClient(app) as client:
        response = post_then_subscribe(client, json=body)
    assert response.status_code == status
    assert len(primary_provider(app).stream_calls) == (1 if status == 200 else 0)
    if status != 200:
        assert response.json()['code'] == 'prompt_too_long'


@pytest.mark.parametrize('window,status', [(41, 200), (40, 400)])
def test_token_window_reserves_output_and_margin(window, status):
    from f3_cases import CountingFixture
    app = make_app(changes={'general-primary': {'context_window_tokens': window,
                   'default_output_tokens': 16, 'max_output_tokens': 32, 'safety_margin_tokens': 1}},
                   counter=CountingFixture(24))
    with TestClient(app) as client:
        response = post_then_subscribe(client, json=request_body())
    assert response.status_code == status
    trace = app.state.traces.requests[response.headers['x-request-id']]
    assert trace.input_token_count == 24 and trace.max_output_tokens == 16
    assert trace.attempts == len(primary_provider(app).stream_calls) == (1 if status == 200 else 0)
    if status != 200:
        assert response.json()['code'] == 'context_length_exceeded'


@pytest.mark.parametrize('messages', [[], [{'role': 'system', 'content': 'x'}],
    [{'role': 'assistant', 'content': 'x'}], [{'role': 'user', 'content': '  \n'}],
    [{'role': 'user', 'content': 'a'}, {'role': 'user', 'content': 'b'}]])
def test_only_one_nonblank_user_message(messages):
    app = make_app()
    body = request_body()
    body['messages'] = messages
    with TestClient(app) as client:
        response = post_then_subscribe(client, json=body)
    assert response.status_code == 422
    trace = app.state.traces.requests[response.headers['x-request-id']]
    assert trace.attempts == 0 and primary_provider(app).stream_calls == []


def test_multiple_preflight_errors_follow_defined_order():
    app = make_app()
    body = request_body()
    body.update(model='missing', max_output_tokens=99999)
    body['prompt'] = {'name': 'context_answer', 'version': '1.1.0'}
    with TestClient(app) as client:
        first = post_then_subscribe(client, json=body)
        body['prompt']['variables'] = {'context': '订单 A123'}
        second = post_then_subscribe(client, json=body)
    assert first.json()['code'] == 'missing_prompt_variable'
    assert second.json()['code'] == 'unknown_model'
    assert primary_provider(app).stream_calls == []


@pytest.mark.parametrize('status', ['completed', 'failed'])
def test_last_delta_and_terminal_in_one_provider_event(status):
    from contracts import Usage
    from providers.base import ProviderStreamEvent
    from providers.mock import MockProvider
    fields = ({'upstream_terminal_received': True,
               'usage': Usage(input_tokens=10, output_tokens=5, total_tokens=15)}
              if status == 'completed' else {'error_code': 'output_truncated'})
    provider = MockProvider(stream_events=[
        ProviderStreamEvent(status=status, delta='最后一段正文', **fields),
        ProviderStreamEvent(delta='终态后禁止消费'),
    ])
    app = make_app(primary=provider)
    with TestClient(app) as client:
        response = post_then_subscribe(client, json=request_body())
    events = parse_events(response.text)
    assert [name for name, _ in events] == ['response.delta', 'response.' + status]
    assert events[0][1]['delta'] == '最后一段正文'
    trace = app.state.traces.requests[response.headers['x-request-id']]
    assert trace.status == app.state.traces.attempts[0].status == status
    assert trace.ttft_ms is not None
    assert provider.stream_next_calls == provider.stream_close_calls == 1
    if status == 'completed':
        assert events[-1][1]['usage'] == {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}
    else:
        assert events[-1][1]['code'] == trace.error_code == 'output_truncated'


@pytest.mark.parametrize('extra', [{'response_schema': {}}, {'response_schema': None},
                                  {'stream': True}, {'stream': False}, {'api_protocol': 'responses'}])
def test_f5_unsupported_fields_rejected_before_provider(extra):
    app = make_app()
    with TestClient(app) as client:
        response = post_then_subscribe(client, json=request_body(**extra))
    assert response.status_code == 422
    assert response.json()['code'] == 'invalid_request'
    trace = app.state.traces.requests[response.headers['x-request-id']]
    assert trace.status == 'failed' and trace.attempts == 0
    assert primary_provider(app).calls == []
