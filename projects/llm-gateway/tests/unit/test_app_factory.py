from f3_cases import MOCK_USAGE, primary_provider
import os
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import main
from providers import factory as provider_factory
from main import create_app
from providers.factory import create_provider
from providers.mock import MockProvider
from services.models import ProviderConfig
from settings import Settings, load_settings


def test_default_provider_factory_returns_uncalled_mock():
    provider = create_provider(ProviderConfig(adapter="mock", api_protocol="chat_completions"))
    assert isinstance(provider, MockProvider)
    assert provider.calls == []


def test_app_test_fixture_does_not_read_developer_model_yaml(monkeypatch, app_settings):
    runtime_yaml = main.DEFAULT_CONFIG_DIR / 'gateway.yaml'
    original = Path.open

    def forbidden(path, *args, **kwargs):
        if path.resolve() == runtime_yaml.resolve():
            raise AssertionError('test fixture must inject model configuration instead of reading developer YAML')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', forbidden)
    with TestClient(create_app(settings=app_settings, provider=MockProvider())) as client:
        assert client.get('/healthz').status_code == 200


def test_default_test_settings_do_not_load_developer_dotenv(monkeypatch):
    import settings as settings_module
    runtime_env = main.DEFAULT_CONFIG_DIR.parent / '.env'
    original = settings_module.Settings

    def forbidden(*args, **kwargs):
        selected = kwargs.get('_env_file')
        if selected is not None and Path(selected).resolve() == runtime_env.resolve():
            raise AssertionError('test fixture must inject settings instead of reading developer dotenv')
        return original(*args, **kwargs)

    monkeypatch.setattr(settings_module, 'Settings', forbidden)
    with TestClient(create_app(provider=MockProvider())) as client:
        assert client.get('/healthz').status_code == 200


def test_default_app_constructs_provider_once(tmp_path, monkeypatch):
    calls = []
    created = []
    def factory(definition, credential=None):
        calls.append((definition, credential))
        instance = MockProvider()
        created.append(instance)
        return instance
    monkeypatch.setattr(main, "load_settings", lambda: load_settings(tmp_path / ".env"))
    monkeypatch.setattr(provider_factory, "create_provider", factory)
    app = create_app()
    assert calls == []
    assert app.state.settings.env == "development"
    with TestClient(app):
        assert len(calls) == len(app.state.model_registry.providers) == 2
        assert all(definition.adapter == "mock" and key is None for definition, key in calls)
        assert primary_provider(app) is created[0]
        assert primary_provider(app).calls == []
        assert app.state.traces.requests == {}
        assert app.state.traces.attempts == []
    assert all(provider.close_calls == 1 for provider in created)


def test_explicit_settings_skip_external_loading(monkeypatch):
    selected = Settings(env="test", _env_file=None)
    monkeypatch.setenv("ENV", "invalid")
    monkeypatch.setenv("LLM_GATEWAY_PROVIDER", "invalid")
    def forbidden():
        pytest.fail("explicit Settings must skip external loading")
    monkeypatch.setattr(main, "load_settings", forbidden)
    app = create_app(settings=selected)
    assert app.state.settings is selected
    with TestClient(app):
        assert isinstance(primary_provider(app), MockProvider)
        assert primary_provider(app).calls == []
        assert app.state.traces.requests == {}


@pytest.mark.parametrize("name,value", [
    ("ENV", "develop"), ("ENV", "TEST"), ("ENV", ""), ("ENV", "   "),
    ("LLM_GATEWAY_PROVIDER", "deepseek"), ("LLM_GATEWAY_PROVIDER", "openai_compatible"),
    ("LLM_GATEWAY_PROVIDER", ""), ("LLM_GATEWAY_PROVIDER", "   "),
])
def test_invalid_settings_fail_before_provider_construction(tmp_path, monkeypatch, name, value):
    calls = []
    injected = MockProvider()
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(main, "load_settings", lambda: load_settings(tmp_path / ".env"))
    monkeypatch.setattr(provider_factory, "create_provider", lambda settings: calls.append(settings))
    with pytest.raises(ValueError, match=name):
        create_app(provider=injected)
    assert calls == []
    assert injected.calls == []


def test_injected_provider_skips_factory(app_settings, payload, monkeypatch):
    injected = MockProvider(text="INJECTED")

    def forbidden(settings):
        pytest.fail("injected provider must skip factory")

    monkeypatch.setattr(provider_factory, "create_provider", forbidden)
    app = create_app(settings=app_settings, provider=injected)
    with TestClient(app) as client:
        response = client.post("/v1/llm", json=payload)
    assert response.status_code == 200
    assert response.json()["data"]["text"] == "INJECTED"
    assert primary_provider(app) is injected
    assert len(injected.calls) == 1
    assert injected.calls[0][0] == app.state.model_registry.get_endpoint("general-primary")
    assert [m.role for m in injected.calls[0][1]] == ["system", "user"]
    assert injected.calls[0][1][1].content == payload["messages"][0]["content"]
    trace = app.state.traces.requests[response.headers["X-Request-ID"]]
    assert (trace.status, trace.attempts) == ("completed", 1)


def test_factory_error_aborts_application(app_settings, monkeypatch):
    calls = []
    created = MockProvider()
    def broken(definition, credential=None):
        calls.append(definition)
        if len(calls) == 1:
            return created
        raise RuntimeError("PRIVATE_FACTORY_MARKER")
    monkeypatch.setattr(provider_factory, "create_provider", broken)
    app = create_app(settings=app_settings)
    with pytest.raises(RuntimeError, match="PRIVATE_FACTORY_MARKER"):
        with TestClient(app):
            pytest.fail("partially initialized app must not serve")
    assert len(calls) == 2
    assert created.close_calls == 1


def test_applications_keep_independent_settings_and_traces(payload):
    apps = [create_app(settings=Settings(env=env, _env_file=None)) for env in ("test", "production")]
    assert apps[0].state.traces is not apps[1].state.traces
    ids = []
    selected = []
    for app, env in zip(apps, ("test", "production")):
        assert app.state.settings.env == env
        with TestClient(app) as client:
            response = client.post("/v1/llm", json=payload)
            selected.append(primary_provider(app))
        assert response.status_code == 200
        ids.append(response.headers["X-Request-ID"])
        assert len(primary_provider(app).calls) == 1
        assert set(app.state.traces.requests) == {ids[-1]}
        assert app.state.traces.requests[ids[-1]].status == "completed"
        assert len(app.state.traces.attempts) == 1
    assert selected[0] is not selected[1]
    assert ids[0] != ids[1]


@pytest.mark.parametrize("env", ["development", "test", "production"])
def test_environment_does_not_change_provider_or_http_contract(env, payload):
    from services.prompts import SYSTEM_PROMPT

    app = create_app(settings=Settings(env=env, _env_file=None))
    with TestClient(app) as client:
        response = client.post("/v1/llm", json=payload)
    assert response.status_code == 200
    assert set(response.json()) == {"success", "code", "data", "message"}
    assert response.json()["data"] == {
        "text": "MOCK_REPLY", "usage": MOCK_USAGE,
    }
    assert len(primary_provider(app).calls) == 1
    assert primary_provider(app).calls[0][0] == app.state.model_registry.get_endpoint("general-primary")
    messages = primary_provider(app).calls[0][1]
    assert [(m.role, m.content) for m in messages] == [("system", SYSTEM_PROMPT), ("user", payload["messages"][0]["content"])]
    trace = app.state.traces.requests[response.headers["X-Request-ID"]]
    assert (trace.status, trace.attempts) == ("completed", 1)
    assert app.state.traces.attempts[0].status == "completed"


def test_configuration_is_fixed_until_next_application(tmp_path, payload, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("ENV=test\n")
    original = load_settings
    monkeypatch.setattr(main, "load_settings", lambda: original(env_file))
    first = create_app()
    env_file.write_text("ENV=production\n")
    second = create_app()

    def forbidden():
        pytest.fail("requests must not reload configuration")

    monkeypatch.setattr(main, "load_settings", forbidden)
    for app, env in ((first, "test"), (second, "production")):
        with TestClient(app) as client:
            response = client.post("/v1/llm", json=payload)
        assert response.status_code == 200
        assert app.state.settings.env == env
        assert len(primary_provider(app).calls) == 1
        assert app.state.traces.requests[response.headers["X-Request-ID"]].status == "completed"


def test_import_main_does_not_create_provider_or_app(tmp_path):
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
from providers.mock import MockProvider
from services.models import ProviderConfig
calls = []
original = MockProvider.__init__
def record(self, *args, **kwargs):
    calls.append(1)
    original(self, *args, **kwargs)
MockProvider.__init__ = record
import main
from providers import factory as provider_factory
print(len(calls), hasattr(main, 'app'))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path,
        env={"PATH": os.defpath, "PYTHONPATH": str(source),
             "ENV": "invalid", "LLM_GATEWAY_PROVIDER": "invalid"},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0 False"


def test_import_main_does_not_load_settings_or_call_factory(tmp_path):
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import settings
import providers.factory
import services.prompts
def forbidden(*args, **kwargs):
    raise AssertionError('import must not assemble application')
settings.load_settings = forbidden
providers.factory.create_provider = forbidden
services.prompts.PromptRegistry.__init__ = forbidden
import main
from providers import factory as provider_factory
assert callable(main.create_app)
assert not hasattr(main, 'app')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path,
        env={"PATH": os.defpath, "PYTHONPATH": str(source), "ENV": "invalid", "LLM_GATEWAY_PROVIDER": "invalid"},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
