"""Real calls only after explicit --run-live; no credentials read at import/collection."""
from services.models import ConcurrencyPolicy
import asyncio
from copy import deepcopy
from hashlib import sha256
import json
import socket

import httpx2
from legacy_identity import HEADERS
import pytest
import pytest_asyncio
import uvicorn
from sqlalchemy import text

from main import create_app
from f4_cases import SCHEMA, CONTEXT, PARSED, registry as fixture_registry
from services.models import DEFAULT_CONFIG_DIR, ProviderConfig, EndpointConfig, ModelRegistry
from settings import Settings


pytestmark = pytest.mark.live


def real_registry():
    baseline = fixture_registry()
    providers = {
        "deepseek-main": ProviderConfig(adapter="deepseek", api_protocol="chat_completions", base_url="https://api.deepseek.com", credential_ref="deepseek-main-key"),
        "openai-main": ProviderConfig(adapter="openai_compatible", api_protocol="responses", base_url="https://api.deepseek.com", credential_ref="deepseek-main-key"),
    }
    models = {}
    for name, provider in [("general-primary", "deepseek-main"), ("general-backup", "openai-main")]:
        values = baseline.get_endpoint(name).model_dump()
        values["provider"] = provider
        models[name] = EndpointConfig.model_validate(values)
    return ModelRegistry(endpoints=models, providers=providers, model_groups=baseline.model_groups, concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))


@pytest_asyncio.fixture
async def live_server(request, tmp_path, monkeypatch):
    if not request.config.getoption("--run-live"):
        pytest.fail("live fixture must never run without explicit opt-in")
    # Actual secrets are read only here. No dotenv or secret environment fallback.
    if not (DEFAULT_CONFIG_DIR / "credentials.yaml").is_file():
        pytest.fail("真实接入前置条件未满足：请在本机 config/credentials.yaml 配置 deepseek-main-key；不要把 Key 发到聊天中")
    app = create_app(settings=Settings(env="test", _env_file=None), model_registry=real_registry())
    from upstream_observation import install, snapshot_failures
    install(app,tmp_path,monkeypatch)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    pytest.fail("真实测试应用未完成启动")
                await asyncio.sleep(0.01)
        async with httpx2.AsyncClient(headers=HEADERS, base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=40) as client:
            yield app, client
    finally:
        await snapshot_failures(app,tmp_path)
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()


@pytest.mark.parametrize("model,version,is_probe,is_structured", [
    pytest.param("general-primary", "1.0.0", False, False, id="F4-L01-primary-completion"),
    pytest.param("general-backup", "1.1.0", False, False, id="F4-L02-backup-completion"),
    pytest.param("general-primary", "1.2.0", False, True, id="F4-L03-primary-structured"),
    pytest.param("general-backup", "1.2.0", False, True, id="F4-L04-backup-structured"),
    pytest.param("general-primary", None, True, False, id="F4-L05-primary-probe"),
    pytest.param("general-backup", None, True, False, id="F4-L06-backup-probe"),
])
async def test_real_provider_case(live_server, model, version, is_probe, is_structured, record_property):
    app, client = live_server
    if is_probe:
        response = await client.post(f"/v1/models/{model}/health")
    else:
        payload = {
            "model": model,
            "prompt": {"name": "context_answer", "version": version,
                       "variables": {"context": CONTEXT if is_structured else "调用方提供的资料：订单 A123 的商品为充电器。"}},
            "messages": [{"role": "user", "content": "订单 A123 的商品是什么？"}],
            "max_output_tokens": 128 if is_structured else 64,
            "timeout_seconds": 30,
        }
        if is_structured:
            payload['response_schema'] = deepcopy(SCHEMA)
            payload['messages'][0]['content'] = '提取订单号、商品信息和备注；备注未知时使用 null。'
        response = await client.post("/v1/llm", json=payload)
    assert response.status_code == 200, response.json().get("code")
    body = response.json()
    assert set(body) == {"success", "code", "data", "message"}
    assert body["success"] is True and body["code"] == "ok"
    request_id = response.headers["X-Request-ID"]
    trace = app.state.traces.requests[request_id]
    attempts = [a for a in app.state.traces.attempts if a.request_id == request_id]
    assert trace.status == "completed" and trace.attempts == len(attempts) == 1
    attempt = attempts[0]
    route = trace.routing
    assert route['version'] == 1
    assert route['strategy'] == ('probe_first_enabled' if is_probe else 'ordered')
    assert route['selected_endpoint'] == attempt.model
    endpoint = app.state.model_registry.get_endpoint(attempt.model)
    definition = app.state.model_registry.providers[endpoint.provider]
    assert attempt.provider == endpoint.provider
    assert attempt.configured_model == endpoint.model_id
    assert attempt.api_protocol == definition.api_protocol
    assert attempt.adapter == definition.adapter
    assert attempt.status == "completed" and attempt.finish_reason == "stop"
    assert attempt.actual_model and attempt.usage is not None
    assert attempt.adapter == ("deepseek" if model == "general-primary" else "openai_compatible")
    assert attempt.api_protocol == ("chat_completions" if model == "general-primary" else "responses")
    if model == 'general-primary':
        assert attempt.upstream_finish_reason == 'stop'
        assert attempt.upstream_status is None
    else:
        assert attempt.upstream_status == 'completed'
        assert attempt.upstream_finish_reason is None
    assert trace.operation == ("provider_probe" if is_probe else "completion")
    assert trace.counter_version and trace.prompt_fingerprint and trace.input_token_count > 0
    delta = attempt.usage.input_tokens - trace.input_token_count
    # Record safe measurements before checking equality so a failed first real
    # run still provides evidence for diagnosis without another Provider call.
    for name, value in {
        "request_id": request_id, "provider": attempt.provider, "adapter": attempt.adapter,
        "api_protocol": attempt.api_protocol, "upstream_status": attempt.upstream_status,
        "upstream_finish_reason": attempt.upstream_finish_reason,
        "actual_model": attempt.actual_model, "counter_version": trace.counter_version,
        "prompt_fingerprint": trace.prompt_fingerprint, "schema_fingerprint": trace.schema_fingerprint,
        "structured_output_mode": trace.structured_output_mode,
        "local_input_tokens": trace.input_token_count, "input_delta": delta,
        "input_tokens": attempt.usage.input_tokens, "output_tokens": attempt.usage.output_tokens,
        "total_tokens": attempt.usage.total_tokens, "latency_ms": attempt.latency_ms,
    }.items():
        record_property(name, value)
    assert delta == 0
    if is_structured:
        expected_fingerprint=sha256(json.dumps(SCHEMA,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        assert trace.schema_fingerprint == expected_fingerprint
        assert trace.structured_output_mode == ('json_object' if model == 'general-primary' else 'json_schema')
    else:
        assert trace.schema_fingerprint is None and trace.structured_output_mode == 'none'
    if is_probe:
        diagnostic = await client.get(f'/v1/traces/{request_id}')
        assert diagnostic.status_code == 200, diagnostic.text
        data = diagnostic.json()['data']
        async with app.state.storage.database.transaction() as session:
            row = (await session.execute(text(
                'SELECT r.operation, r.status, a.attempt_no, a.api_protocol, a.actual_model, a.total_tokens '
                'FROM tb_llm_request r JOIN tb_llm_attempt a ON a.request_pk = r.id WHERE r.request_id = :rid'
            ), {'rid': request_id})).mappings().one()
        assert data['routing'] == route
        assert data['operation'] == row['operation'] == 'provider_probe'
        assert data['status'] == row['status'] == 'completed'
        saved, = data['attempts']
        assert saved['attempt_no'] == row['attempt_no'] == 1
        assert saved['api_protocol'] == row['api_protocol'] == attempt.api_protocol
        assert saved['actual_model'] == row['actual_model'] == attempt.actual_model
        assert data['usage']['known_subtotal']['total_tokens'] == row['total_tokens'] == attempt.usage.total_tokens
        assert set(body["data"]) == {"latency_ms"}
        assert body["data"]["latency_ms"] == attempt.latency_ms
        assert (trace.max_output_tokens, trace.timeout_seconds) == (16, 10)
    else:
        assert set(body["data"]) == ({"text", "parsed", "usage"} if is_structured else {"text", "usage"})
        assert body["data"]["text"].strip()
        if is_structured and body['data']['parsed'] != PARSED:
            pytest.fail('结构化结果与固定业务夹具不符')
        assert body["data"]["usage"] == attempt.usage.model_dump()
        assert (trace.max_output_tokens, trace.timeout_seconds) == (128 if is_structured else 64, 30)
