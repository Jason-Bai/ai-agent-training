"""Provider assembly validates before construction and owns only its clients."""
import pytest

from f3_cases import registry
from providers import factory
from providers.mock import MockProvider


@pytest.mark.parametrize('kind', ['single', 'mapping'])
async def test_injected_clients_are_snapshotted_and_never_owned(monkeypatch, kind):
    first, second = MockProvider(), MockProvider()
    supplied = {'mock-main': first, 'mock-backup': second}
    def forbidden(*args, **kwargs):
        pytest.fail('injection must skip credential loading and construction')
    monkeypatch.setattr(factory, 'create_provider', forbidden)
    monkeypatch.setattr(factory, 'load_credentials', forbidden, raising=False)
    clients = factory.ProviderClients(registry(), **({'provider': first} if kind == 'single' else {'providers': supplied}))
    supplied.clear()
    assert set(clients.instances) == {'mock-main', 'mock-backup'}
    assert clients.instances['mock-main'] is first
    with pytest.raises(TypeError):
        clients.instances['other'] = second
    async with clients.open() as opened:
        assert opened is clients.instances
        assert opened['mock-backup'] is (first if kind == 'single' else second)
    assert first.close_calls == second.close_calls == 0


@pytest.mark.parametrize('kwargs', [
    {'providers': {}},
    {'providers': {'wrong': MockProvider()}},
    {'provider': MockProvider(), 'providers': {}},
])
def test_invalid_injection_is_rejected_before_factory(monkeypatch, kwargs):
    calls = []
    monkeypatch.setattr(factory, 'create_provider', lambda *args: calls.append(args))
    with pytest.raises(ValueError):
        factory.ProviderClients(registry(), **kwargs)
    assert calls == []


async def test_owned_clients_are_lazy_closed_and_rebuilt_per_open(monkeypatch):
    created = []
    def build(*args):
        result = MockProvider()
        created.append(result)
        return result
    monkeypatch.setattr(factory, 'create_provider', build)
    clients = factory.ProviderClients(registry())
    assert created == [] and clients.instances == {}
    for expected in (2, 4):
        async with clients.open() as opened:
            assert len(created) == expected
            assert len(opened) == 2
            assert all(p.close_calls == 0 for p in opened.values())
        assert all(p.close_calls == 1 for p in created)


async def test_partial_construction_closes_already_created_client(monkeypatch):
    first = MockProvider()
    calls = []
    def build(*args):
        calls.append(args)
        if len(calls) == 2:
            raise RuntimeError('construction failed')
        return first
    monkeypatch.setattr(factory, 'create_provider', build)
    clients = factory.ProviderClients(registry())
    with pytest.raises(RuntimeError, match='construction failed'):
        async with clients.open():
            pytest.fail('partially initialized clients must not be yielded')
    assert first.close_calls == 1 and len(calls) == 2
