"""Application resource ownership and local configuration isolation."""
from fastapi.testclient import TestClient
import pytest

import main
from providers import factory as provider_factory
from f3_cases import CountingFixture, registry
from main import create_app
from providers.mock import MockProvider
from settings import Settings


def test_incompatible_model_is_rejected_before_provider_factory(monkeypatch):
    calls = []
    monkeypatch.setattr(provider_factory, 'create_provider', lambda *args: calls.append(args))
    with pytest.raises(ValueError, match='计数'):
        create_app(settings=Settings(env='test', _env_file=None),
                   model_registry=registry({'general-primary': {'model_id': 'other-model'}}))
    assert calls == []


def test_later_factory_failure_closes_first_owned_client(monkeypatch):
    created = MockProvider()
    calls = []
    def factory(config, credential=None):
        calls.append(config)
        if len(calls) == 2:
            raise RuntimeError('SECOND_CONSTRUCTION_FAILED')
        return created
    monkeypatch.setattr(provider_factory, 'create_provider', factory)
    app = create_app(settings=Settings(env='test', _env_file=None), model_registry=registry())
    with pytest.raises(RuntimeError, match='SECOND_CONSTRUCTION_FAILED'):
        with TestClient(app):
            pytest.fail('partially built provider set must not serve')
    assert len(calls) == 2 and created.close_calls == 1 and created.calls == []
    assert app.state.traces.requests == {} and app.state.traces.attempts == []


def test_one_close_failure_does_not_skip_other_owned_clients(monkeypatch):
    class BrokenClose(MockProvider):
        async def aclose(self):
            await super().aclose()
            raise RuntimeError('PRIVATE_CLOSE_ERROR')
    first, second = MockProvider(), BrokenClose()
    pending = iter([first, second])
    monkeypatch.setattr(provider_factory, 'create_provider', lambda *args: next(pending))
    app = create_app(settings=Settings(env='test', _env_file=None), model_registry=registry())
    with pytest.raises(RuntimeError, match='Provider 客户端关闭失败') as exc:
        with TestClient(app):
            pass
    assert 'PRIVATE_CLOSE_ERROR' not in str(exc.value)
    assert (first.close_calls, second.close_calls) == (1, 1)


def test_explicit_dependencies_do_not_load_env_registry_or_credentials(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('explicit test dependencies must not access local runtime files')
    for name in ['load_settings', 'load_registry']:
        monkeypatch.setattr(main, name, forbidden)
    for name in ['load_credentials', 'create_provider']:
        monkeypatch.setattr(provider_factory, name, forbidden)
    first, second = MockProvider(), MockProvider()
    app = create_app(settings=Settings(env='test', _env_file=None), model_registry=registry(),
                     providers={'mock-main': first, 'mock-backup': second}, token_budget=CountingFixture().as_budget())
    with TestClient(app) as client:
        response = client.get('/healthz')
    assert response.status_code == 200
    assert first.calls == second.calls == []
    assert (first.close_calls, second.close_calls) == (0, 0)
    assert app.state.traces.requests == {} and app.state.traces.attempts == []


@pytest.mark.parametrize('provided', [{}, {'unexpected': MockProvider()}])
def test_explicit_provider_set_must_match_registry(provided):
    with pytest.raises(ValueError, match='Provider 集合'):
        create_app(settings=Settings(env='test', _env_file=None), model_registry=registry(), providers=provided)


def test_two_injection_forms_are_not_silently_combined():
    with pytest.raises(ValueError, match='一种显式注入'):
        create_app(settings=Settings(env='test', _env_file=None), model_registry=registry(),
                   provider=MockProvider(), providers={'mock-main': MockProvider(), 'mock-backup': MockProvider()})
