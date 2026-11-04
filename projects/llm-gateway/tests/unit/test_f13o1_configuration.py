"""Unified pricing configuration, without private files or remote calls."""
from datetime import datetime
from pathlib import Path
from decimal import Decimal

import pytest
import yaml

from contracts import Usage
from errors import GatewayError
from services.models import ConfigurationError, load_registry
from services.prices import PriceBook, PriceQuote
from test_f12_config import document


def catalog():
    rate = {'hit': '0.000000001', 'miss': '2', 'output': '8'}
    return {'bindings': {'mock-main': 'fixture'}, 'versions': [{
        'supplier': 'fixture', 'model': 'deepseek-flash', 'version': 'v1',
        'effective_from': '2020-01-01T00:00:00Z', 'peak': rate, 'off_peak': rate}]}


def write(tmp_path, pricing):
    data = document()
    data['pricing'] = pricing
    (tmp_path/'gateway.yaml').write_text(yaml.safe_dump(data))


def test_registry_accepts_pricing_but_pricebook_owns_validation(tmp_path):
    write(tmp_path, catalog())
    assert load_registry(tmp_path).candidates('general-primary') == ('chat',)
    book = PriceBook.load(tmp_path)
    quote = book.quote('mock-main', 'deepseek-flash', datetime.fromisoformat('2026-11-03T00:00:00Z'), 100, 20)
    assert quote.cost(Usage(input_tokens=100, output_tokens=20, total_tokens=120), 50) == Decimal('0.00026000000005')
    assert PriceQuote.from_snapshot(quote.snapshot()) == quote


@pytest.mark.parametrize('pricing', [None, [], 'secret-marker', {}, {'bindings': {}, 'versions': [], 'extra': 1}])
def test_invalid_pricing_is_rejected_without_leaking_values(tmp_path, pricing):
    write(tmp_path, pricing)
    with pytest.raises(ConfigurationError) as caught:
        PriceBook.load(tmp_path)
    assert 'secret-marker' not in str(caught.value)


def test_missing_pricing_does_not_fall_back_to_legacy_file(tmp_path):
    (tmp_path/'gateway.yaml').write_text(yaml.safe_dump(document()))
    (tmp_path/'prices.yaml').write_text(yaml.safe_dump(catalog()))
    book = PriceBook.load(tmp_path)
    with pytest.raises(GatewayError) as caught:
        book.quote('mock-main', 'deepseek-flash', datetime.fromisoformat('2026-11-03T00:00:00Z'), 10, 20)
    assert caught.value.code == 'price_unavailable'


def test_conflicting_legacy_price_file_is_never_read(tmp_path):
    write(tmp_path, catalog())
    (tmp_path/'prices.yaml').write_text('malformed: [')
    assert PriceBook.load(tmp_path).catalog.bindings == {'mock-main': 'fixture'}


def test_complete_example_loads_models_budget_and_prices_from_one_file(tmp_path):
    from services.token_budget import TokenBudget
    root = Path(__file__).resolve().parents[2]
    (tmp_path/'gateway.yaml').write_bytes((root/'config/gateway.example.yaml').read_bytes())
    registry = load_registry(tmp_path)
    budget = TokenBudget.from_models(registry.endpoints.values())
    budget.validate_models(registry.endpoints.values())
    assert set(registry.auto_routes) == {'auto-general'}
    book = PriceBook.load(tmp_path)
    assert book.catalog.bindings['bailian-main'] == 'bailian-beijing'
    assert {v.model for v in book.catalog.versions} >= {'qwen3.8-flash', 'qwen3.7-plus', 'qwen3.8-max'}
    assert not list((root/'config').glob('gateway.*.example.yaml'))
    assert not list((root/'config').glob('prices*.example.yaml'))


@pytest.mark.parametrize('marked_live,enabled', [(False, False), (False, True), (True, False), (True, True)])
def test_historical_price_fixture_only_reads_real_defaults_with_explicit_live_opt_in(tmp_path, monkeypatch, marked_live, enabled):
    from types import SimpleNamespace
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gateway_root_conftest", Path(__file__).parents[1] / "conftest.py")
    conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conftest)
    import main

    default = tmp_path/'defaults'
    default.mkdir()
    write(default, catalog())
    request = SimpleNamespace(
        path=Path('test_historical_price_wrapper.py'),
        node=SimpleNamespace(get_closest_marker=lambda name: marked_live),
        config=SimpleNamespace(getoption=lambda name: enabled),
        getfixturevalue=lambda name: tmp_path,
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(main, 'DEFAULT_CONFIG_DIR', default)
        fixture = conftest.explicit_database_for_historical_tests.__wrapped__(request, scoped)
        next(fixture)
        try:
            bindings = PriceBook.load(default).catalog.bindings
            assert bindings == ({'mock-main': 'fixture'} if marked_live and enabled else {})
        finally:
            with pytest.raises(StopIteration):
                next(fixture)
