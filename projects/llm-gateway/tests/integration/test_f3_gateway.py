import asyncio
from copy import deepcopy

import httpx
import pytest

from contracts import Usage
from errors import ProviderCallError
from f3_cases import MOCK_USAGE, CountingFixture, make_app, primary_provider
from providers.base import ProviderResult


from fastapi.testclient import TestClient

from main import create_app
from providers.mock import MockProvider
from settings import Settings


def assert_options(options, output, timeout, count=1):
    assert len(options) == count
    for item in options:
        assert set(item) == {"max_output_tokens", "timeout_seconds"}
        assert item["max_output_tokens"] == output
        assert 0 < item["timeout_seconds"] <= timeout


def test_f3_public_result_only_contains_answer_and_usage():
    app = create_app(settings=Settings(env="test", _env_file=None), provider=MockProvider())
    with TestClient(app) as client:
        response = client.post("/v1/llm", json={
            "model": "general-primary",
            "prompt": {"name": "basic_text", "version": "1.0.0"},
            "messages": [{"role": "user", "content": "Explain TTFT"}],
        })
    assert response.status_code == 200
    assert response.json()["data"] == {"text": "MOCK_REPLY", "usage": MOCK_USAGE}


def test_f3_probe_has_its_own_trace_operation():
    app = create_app(settings=Settings(env="test", _env_file=None), provider=MockProvider())
    with TestClient(app) as client:
        response = client.post("/v1/models/general-primary/health")
    assert response.status_code == 200
    assert set(response.json()["data"]) == {"latency_ms"}
    trace = app.state.traces.requests[response.headers["X-Request-ID"]]
    assert trace.operation == "provider_probe"
    assert trace.attempts == 1


def record(app, response):
    return app.state.traces.requests[response.headers["X-Request-ID"]]


def assert_error(app, response, code, status, attempts):
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"success", "code", "data", "message"}
    assert body["success"] is False and body["data"] is None and body["code"] == code
    assert record(app, response).status == "failed"
    assert record(app, response).attempts == attempts
    assert record(app, response).error_code == code
    assert "PRIVATE" not in response.text


@pytest.mark.parametrize("name", ["general-primary", "general-backup"])
@pytest.mark.parametrize("budgets", [{}, {"max_output_tokens": 1, "timeout_seconds": 1},
                                     {"max_output_tokens": 4096, "timeout_seconds": 120}])
def test_f3_selects_exact_provider_and_budget_defaults(payload, name, budgets):
    app = make_app()
    payload.update(model=name, **budgets)
    with TestClient(app) as client:
        response = client.post("/v1/llm", json=payload)
    assert response.status_code == 200
    selected = "mock-main" if name == "general-primary" else "mock-backup"
    other = "mock-backup" if selected == "mock-main" else "mock-main"
    assert len(app.state.providers[selected].calls) == 1
    assert not app.state.providers[other].calls
    assert_options(app.state.providers[selected].call_options,
                   budgets.get("max_output_tokens", 1024), budgets.get("timeout_seconds", 30))
    trace = record(app, response)
    assert trace.operation == "completion" and trace.input_token_count == 236
    assert trace.counter_version and trace.prompt_fingerprint
    attempt = app.state.traces.attempts[0]
    assert (attempt.provider, attempt.adapter, attempt.configured_model) == (selected, "mock", "deepseek-flash")
    assert attempt.latency_ms >= 0 and attempt.finish_reason == "stop"
    assert attempt.actual_model is None
    assert attempt.usage.model_dump() == MOCK_USAGE


@pytest.mark.parametrize("field", ["max_output_tokens", "timeout_seconds"])
@pytest.mark.parametrize("value", [None, True, False, "16", 1.5, 0, -1, [], {}])
def test_f3_rejects_bad_budget_types_before_fingerprinting(payload, field, value):
    app = make_app()
    payload[field] = value
    with TestClient(app) as client:
        response = client.post("/v1/llm", json=payload)
    assert_error(app, response, "invalid_request", 422, 0)
    assert record(app, response).prompt_fingerprint is None
    assert not primary_provider(app).calls


@pytest.mark.parametrize("fields,code", [
    ({"max_output_tokens": 4097}, "invalid_output_budget"),
    ({"timeout_seconds": 121}, "invalid_timeout"),
    ({"max_output_tokens": 4097, "timeout_seconds": 121}, "invalid_output_budget"),
    ({"model": "absent", "max_output_tokens": 4097}, "unknown_model"),
])
def test_f3_per_model_rejections_precede_token_count(payload, fields, code):
    counter = CountingFixture(error=RuntimeError("PRIVATE_ERROR_MARKER"))
    app = make_app(counter=counter)
    payload.update(fields)
    with TestClient(app) as client:
        response = client.post("/v1/llm", json=payload)
    assert_error(app, response, code, 400, 0)
    assert record(app, response).prompt_fingerprint
    assert counter.calls == 0 and not primary_provider(app).calls


@pytest.mark.parametrize("tokens,status", [(24, 200), (25, 400)])
@pytest.mark.parametrize("explicit", [True, False])
def test_f3_window_equal_and_one_over(payload, tokens, status, explicit):
    counter = CountingFixture(tokens)
    app = make_app(counter=counter, changes={"general-primary": {
        "context_window_tokens": 48, "default_output_tokens": 16, "max_output_tokens": 32, "safety_margin_tokens": 8,
    }})
    if explicit:
        payload["max_output_tokens"] = 16
    with TestClient(app) as client:
        response = client.post("/v1/llm", json=payload)
    assert response.status_code == status
    trace = record(app, response)
    assert trace.input_token_count == tokens and trace.max_output_tokens == 16
    assert counter.calls == 1
    assert len(primary_provider(app).calls) == (1 if status == 200 else 0)
    if status != 200:
        assert_error(app, response, "context_length_exceeded", 400, 0)
        assert trace.prompt_fingerprint


def test_f3_count_failure_does_not_bypass_preflight(payload):
    app = make_app(counter=CountingFixture(error=RuntimeError("PRIVATE_ERROR_MARKER")))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/v1/llm", json=payload)
    assert_error(app, response, "internal_error", 500, 0)
    assert record(app, response).input_token_count is None
    assert not primary_provider(app).calls


@pytest.mark.parametrize("reason,code,status", [
    ("length", "output_truncated", 502), ("content_filter", "provider_content_filtered", 502),
    ("aborted", "provider_unavailable", 503), ("insufficient_system_resource", "provider_unavailable", 503),
    ("tool_calls", "provider_invalid_response", 502), ("unknown", "provider_invalid_response", 502),
])
def test_f3_failed_completion_keeps_only_confirmed_metadata(payload, reason, code, status):
    usage = Usage(input_tokens=120, output_tokens=30, total_tokens=150)
    provider = MockProvider(result=ProviderResult(text="PRIVATE_PARTIAL_TEXT", actual_model="deepseek-flash",
                                                finish_reason=reason, usage=usage))
    app = make_app(primary=provider)
    with TestClient(app) as client:
        response = client.post("/v1/llm", json=payload)
    assert_error(app, response, code, status, 1)
    attempt = app.state.traces.attempts[0]
    assert attempt.usage == usage and attempt.actual_model == "deepseek-flash"
    assert attempt.finish_reason == reason and attempt.status == "failed"
    assert attempt.error_code == code and attempt.latency_ms >= 0
    assert len(provider.calls) == 1 and not app.state.providers["mock-backup"].calls
    assert "PRIVATE_PARTIAL_TEXT" not in repr(app.state.traces.__dict__)


@pytest.mark.parametrize("code,status", [("provider_auth_error", 502), ("provider_quota_exceeded", 503),
                                        ("provider_rate_limited", 503), ("provider_connection_error", 502),
                                        ("provider_timeout", 504), ("provider_unavailable", 503)])
def test_f3_classified_error_survives_http_boundary(payload, code, status):
    app = make_app(primary=MockProvider(error=ProviderCallError("PRIVATE_ERROR_MARKER", code=code, status_code=status)))
    with TestClient(app) as client:
        response = client.post("/v1/llm", json=payload)
    assert_error(app, response, code, status, 1)
    assert app.state.traces.attempts[0].error_code == code
    assert len(primary_provider(app).calls) == 1


def test_f3_total_deadline_stops_a_waiting_provider(payload):
    provider = MockProvider(delay=5)
    app = make_app(primary=provider)
    payload["timeout_seconds"] = 1
    with TestClient(app) as client:
        response = client.post("/v1/llm", json=payload)
    assert_error(app, response, "request_timeout", 504, 1)
    assert 800 <= app.state.traces.attempts[0].latency_ms < 2500
    assert app.state.traces.attempts[0].usage is None


@pytest.mark.parametrize("model", ["general-primary", "general-backup"])
def test_f3_probe_fixed_parameters_latency_and_usage_are_internal(model):
    usage = Usage(input_tokens=8, output_tokens=1, total_tokens=9)
    provider = MockProvider(result=ProviderResult(text="TEST_REPLY", actual_model="fixture", usage=usage))
    app = make_app(primary=provider, backup=provider)
    with TestClient(app) as client:
        response = client.post(f"/v1/models/{model}/health")
        second = client.post(f"/v1/models/{model}/health")
        liveness = client.get("/healthz")
    assert response.status_code == second.status_code == liveness.status_code == 200
    assert len(provider.calls) == 2
    assert_options(provider.call_options, 16, 10, count=2)
    assert provider.calls[0][1][-1].content == "请仅回复 OK"
    assert len(provider.calls[0][1]) == 2
    assert set(response.json()["data"]) == {"latency_ms"}
    attempt = app.state.traces.attempts[0]
    assert response.json()["data"]["latency_ms"] == attempt.latency_ms
    assert attempt.usage == usage
    assert record(app, response).operation == "provider_probe"
    assert len(app.state.traces.requests) == 2


@pytest.mark.parametrize("body", [b"{}", b'{"max_output_tokens":100}', b"PRIVATE_INPUT_MARKER"])
def test_f3_probe_rejects_body_before_prompt(body):
    app = make_app()
    with TestClient(app) as client:
        response = client.post("/v1/models/general-primary/health", content=body)
    assert_error(app, response, "invalid_request", 422, 0)
    assert record(app, response).operation == "provider_probe"
    assert record(app, response).prompt_fingerprint is None


@pytest.mark.parametrize("changes,model,code", [
    ({}, "absent", "unknown_model"),
    ({"default_output_tokens": 8, "max_output_tokens": 8}, "general-primary", "invalid_output_budget"),
    ({"default_timeout_seconds": 5, "max_timeout_seconds": 5}, "general-primary", "invalid_timeout"),
])
def test_f3_probe_obeys_model_limits(changes, model, code):
    app = make_app(changes={"general-primary": changes})
    with TestClient(app) as client:
        response = client.post(f"/v1/models/{model}/health")
    assert_error(app, response, code, 400, 0)
    assert not primary_provider(app).calls


async def test_f3_parallel_models_and_parameters_are_isolated(payload):
    entered = asyncio.Event()

    class First(MockProvider):
        async def complete(self, model, messages, **options):
            await entered.wait()
            return await super().complete(model, messages, **options)

    class Second(MockProvider):
        async def complete(self, model, messages, **options):
            result = await super().complete(model, messages, **options)
            entered.set()
            return result

    first, second = First(text="first"), Second(text="second")
    app = make_app(primary=first, backup=second)
    other = deepcopy(payload)
    other.update(model="general-backup", max_output_tokens=16, timeout_seconds=10)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            a, b = await asyncio.gather(client.post("/v1/llm", json=payload), client.post("/v1/llm", json=other))
    assert [a.json()["data"]["text"], b.json()["data"]["text"]] == ["first", "second"]
    assert_options(first.call_options, 1024, 30)
    assert_options(second.call_options, 16, 10)
    assert record(app, a).prompt_fingerprint == record(app, b).prompt_fingerprint
    for response, provider_id in [(a, "mock-main"), (b, "mock-backup")]:
        attempts = [x for x in app.state.traces.attempts if x.request_id == response.headers["X-Request-ID"]]
        assert len(attempts) == 1 and attempts[0].provider == provider_id


@pytest.mark.parametrize("version,expected_count,status", [("1.0.0", 358, 200), ("1.1.0", 515, 400)])
def test_f3_full_system_and_few_shots_can_exceed_window(payload, version, expected_count, status):
    app = make_app(changes={"general-primary": {
        "context_window_tokens": 500, "default_output_tokens": 16, "max_output_tokens": 32, "safety_margin_tokens": 8,
    }})
    payload["prompt"] = {"name": "context_answer", "version": version,
                         "variables": {"context": "调用方提供的资料：订单 A123 的商品为充电器。"}}
    payload["messages"][0]["content"] = "订单 A123 的商品是什么？"
    with TestClient(app) as client:
        response = client.post("/v1/llm", json=payload)
    assert response.status_code == status
    assert record(app, response).input_token_count == expected_count
    assert len(primary_provider(app).calls) == (1 if status == 200 else 0)
    if status == 400:
        assert_error(app, response, "context_length_exceeded", 400, 0)


def test_f3_missing_variable_precedes_unknown_model_and_count(payload):
    counter = CountingFixture(error=RuntimeError("PRIVATE_ERROR_MARKER"))
    app = make_app(counter=counter)
    payload.update(model="absent", prompt={"name": "context_answer", "version": "1.0.0"})
    with TestClient(app) as client:
        response = client.post("/v1/llm", json=payload)
    assert_error(app, response, "missing_prompt_variable", 400, 0)
    assert counter.calls == 0 and record(app, response).prompt_fingerprint is None
