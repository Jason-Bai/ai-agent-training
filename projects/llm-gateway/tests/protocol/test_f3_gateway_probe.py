from registry_helpers import ModelConfig, ModelRegistry
"""Gateway-to-real-adapter integration; only the external HTTP service is a fixture."""
from services.models import ConcurrencyPolicy
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Event, Thread
from time import monotonic

import httpx
import pytest

from f3_cases import MODEL_VALUES
from main import create_app
from services.models import ProviderConfig
from settings import Settings
from test_providers import SUCCESS, adapter_type, server, sent_messages, assert_sent_budget


def application(adapter_type, state):
    adapter = adapter_type(state.url, 'FAKE_CREDENTIAL_MARKER')
    kind = 'deepseek' if adapter_type.__name__ == 'DeepSeekProvider' else 'openai_compatible'
    values = dict(MODEL_VALUES, provider='wire')
    registry = ModelRegistry({'general-primary': ModelConfig(**values)}, {
        'wire': ProviderConfig(adapter=kind, api_protocol='chat_completions' if kind=='deepseek' else 'responses', base_url=state.url, credential_ref='fixture-key'),
    }, concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))
    app = create_app(settings=Settings(env='test', _env_file=None), model_registry=registry,
                     providers={'wire': adapter})
    return app, adapter, kind


def assert_failed_attempt(app, response, code, status, kind, *, usage=False):
    assert response.status_code == status
    body = response.json()
    assert set(body) == {'success', 'code', 'data', 'message'}
    assert body['success'] is False and body['code'] == code and body['data'] is None
    assert 'PRIVATE' not in response.text and 'FAKE_CREDENTIAL_MARKER' not in response.text
    request_id = response.headers['X-Request-ID']
    trace = app.state.traces.requests[request_id]
    attempts = [a for a in app.state.traces.attempts if a.request_id == request_id]
    assert trace.status == 'failed' and trace.error_code == code and trace.attempts == 1
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.provider == 'wire' and attempt.adapter == kind
    assert attempt.status == 'failed' and attempt.error_code == code
    assert attempt.latency_ms >= 0 and trace.prompt_fingerprint
    if usage:
        assert attempt.usage.total_tokens == 150
        assert attempt.actual_model == 'deepseek-flash'
    else:
        assert attempt.usage is None
    return trace, attempt


@pytest.mark.parametrize('failure,code,status', [
    ('auth', 'provider_auth_error', 502),
    ('rate', 'provider_rate_limited', 503),
    ('length', 'output_truncated', 502),
    ('malformed', 'provider_invalid_response', 502),
])
async def test_probe_real_adapters_share_gateway_error_and_trace(adapter_type, server, failure, code, status):
    if failure in {'auth', 'rate'}:
        server.status = 401 if failure == 'auth' else 429
        server.body = {'error': {'message': 'PRIVATE_ERROR_MARKER'}}
    elif failure == 'length':
        server.body['choices'][0]['finish_reason'] = 'length'
    else:
        server.raw = b'PRIVATE_ERROR_MARKER'
    app, adapter, kind = application(adapter_type, server)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.post('/v1/models/general-primary/health')
        trace, attempt = assert_failed_attempt(app, response, code, status, kind, usage=failure == 'length')
        assert trace.operation == 'provider_probe'
        assert (trace.max_output_tokens, trace.timeout_seconds) == (16, 10)
        assert len(server.calls) == 1
        sent = server.calls[0]['body']
        assert_sent_budget(server.calls[0],16)
        assert sent_messages(server.calls[0])[-1] == {'role': 'user', 'content': '请仅回复 OK'}
        if failure == 'length':
            assert attempt.finish_reason == 'length'
    finally:
        await adapter.aclose()


async def test_probe_keeps_fixed_ten_second_deadline(adapter_type, dripping_server):
    # This intentionally takes ten seconds per adapter. It does not patch the
    # Gateway's timeout or silently substitute a shorter ordinary-call budget.
    dripping_server.hold = True
    app, adapter, kind = application(adapter_type, dripping_server)
    started = monotonic()
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.post('/v1/models/general-primary/health')
        elapsed = monotonic() - started
        trace, attempt = assert_failed_attempt(app, response, 'provider_timeout', 504, kind)
        assert trace.operation == 'provider_probe'
        assert trace.timeout_seconds == 10 and trace.max_output_tokens == 16
        assert len(dripping_server.calls) == 1
        assert 9 <= elapsed < 14
        assert 9000 <= attempt.latency_ms < 14000
    finally:
        await adapter.aclose()


@pytest.fixture
def dripping_server():
    class State:
        calls = []
        chunks = 0
        hold = False
    state = State()
    stop = Event()
    finished = Event()
    data = json.dumps(deepcopy(SUCCESS)).encode()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = self.rfile.read(int(self.headers['content-length']))
            state.calls.append(json.loads(payload))
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                if state.hold:
                    stop.wait(30)
                    return
                for i in range(0, len(data), 4):
                    if stop.wait(.1):
                        break
                    self.wfile.write(data[i:i + 4])
                    self.wfile.flush()
                    state.chunks += 1
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                finished.set()

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    http.daemon_threads = True
    worker = Thread(target=lambda: http.serve_forever(poll_interval=.01), daemon=True)
    worker.start()
    state.url = f'http://127.0.0.1:{http.server_port}'
    try:
        yield state
    finally:
        stop.set()
        http.shutdown()
        http.server_close()
        worker.join(timeout=3)
        assert finished.wait(3), 'dripping response handler was not released'
        assert not worker.is_alive()


async def test_total_deadline_wins_over_continued_partial_reads(adapter_type, dripping_server):
    app, adapter, kind = application(adapter_type, dripping_server)
    started = monotonic()
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.post('/v1/llm', json={
                    'model': 'general-primary',
                    'prompt': {'name': 'basic_text', 'version': '1.0.0'},
                    'messages': [{'role': 'user', 'content': 'Question'}],
                    'timeout_seconds': 1,
                })
        elapsed = monotonic() - started
        trace, attempt = assert_failed_attempt(app, response, 'request_timeout', 504, kind)
        assert trace.operation == 'completion' and trace.timeout_seconds == 1
        assert len(dripping_server.calls) == 1
        assert dripping_server.chunks >= 3  # read activity continued well within each idle timeout
        assert .8 <= elapsed < 2.5
        assert 800 <= attempt.latency_ms < 2500
    finally:
        await adapter.aclose()
