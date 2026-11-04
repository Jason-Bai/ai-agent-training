from services.models import EndpointConfig as ModelConfig
"""Explicit strategy configuration and provider-independent counting contracts."""
import pytest
from pydantic import ValidationError

from f3_cases import MODEL_VALUES as HISTORICAL_MODEL_VALUES

MODEL_VALUES = {key: value for key, value in HISTORICAL_MODEL_VALUES.items()
                if key not in {"default_output_tokens", "default_timeout_seconds"}} | {"enabled": True}



@pytest.mark.parametrize('value', ['omitted', '', ' ', None, True, False, 1, 1.5, [], {}])
def test_counter_reference_is_explicit_nonblank_string(value):
    values = dict(MODEL_VALUES)
    if value == 'omitted':
        del values['token_counter']
    else:
        values['token_counter'] = value
    with pytest.raises(ValidationError):
        ModelConfig(**values)

from dataclasses import dataclass
from types import SimpleNamespace

from contracts import Message
from services.token_budget import TokenBudget


@dataclass
class FixtureCounter:
    version: str = 'fixture-v1'
    tokens: int = 24

    def supports(self, model):
        return model.model_id.startswith('fixture-') and model.thinking_mode == 'disabled'

    def count(self, messages):
        return self.tokens


def test_generic_budget_selects_registered_strategy_and_copies_mapping():
    counter = FixtureCounter()
    counters = {'fixture': counter}
    budget = TokenBudget(counters)
    counters.clear()
    model = SimpleNamespace(model_id='fixture-a', thinking_mode='disabled', token_counter='fixture')
    budget.validate_models([model])
    measured = budget.count(model, [Message(role='user', content='anything')])
    assert (measured.input_tokens, measured.counter_version) == (24, 'fixture-v1')
    assert budget.counter_for(model) is counter


@pytest.mark.parametrize('changes', [dict(token_counter='missing'), dict(model_id='other-model'), dict(thinking_mode='enabled')])
def test_unknown_or_incompatible_strategy_is_rejected(changes):
    budget = TokenBudget({'fixture': FixtureCounter()})
    values = dict(model_id='fixture-a', thinking_mode='disabled', token_counter='fixture')
    values.update(changes)
    with pytest.raises(ValueError, match='计数'):
        budget.validate_models([SimpleNamespace(**values)])


def test_unknown_production_strategy_is_rejected_before_resource_loading(monkeypatch):
    # A resource load should never be needed to discover an unknown key.
    from services import token_counters
    calls = []
    monkeypatch.setattr(token_counters, 'COUNTER_FACTORIES', {'fixture': lambda: calls.append(1)})
    models = [SimpleNamespace(token_counter='fixture'), SimpleNamespace(token_counter='missing')]
    with pytest.raises(ValueError, match='计数'):
        token_counters.build_counters(models)
    assert calls == []


def test_only_referenced_strategy_is_loaded_once(monkeypatch):
    from services import token_counters
    created=[]
    counter=FixtureCounter()
    def make():
        created.append(1)
        return counter
    def forbidden():
        pytest.fail('unreferenced strategy must not load resources')
    monkeypatch.setattr(token_counters,'COUNTER_FACTORIES',{'fixture':make,'unused':forbidden})
    models=[SimpleNamespace(token_counter='fixture',model_id=name,thinking_mode='disabled') for name in ['fixture-a','fixture-b']]
    budget=TokenBudget.from_models(models)
    assert created==[1]
    assert budget.counter_for(models[0]) is budget.counter_for(models[1]) is counter


@pytest.mark.parametrize('change',[{'token_counter':'unknown'},{'model_id':'unsupported-model'}])
def test_invalid_binding_stops_before_provider_preparation(monkeypatch,change):
    import main
    from f3_cases import registry
    from settings import Settings
    prepared=[]
    monkeypatch.setattr(main,'ProviderClients',lambda *args,**kwargs:prepared.append(1))
    with pytest.raises(ValueError,match='计数'):
        main.create_app(settings=Settings(env='test',_env_file=None),model_registry=registry({'general-primary':change}))
    assert prepared==[]


def test_injected_budget_also_validates_bindings_before_provider_preparation(monkeypatch):
    import main
    from f3_cases import registry
    from settings import Settings
    prepared=[]
    monkeypatch.setattr(main,'ProviderClients',lambda *args,**kwargs:prepared.append(1))
    with pytest.raises(ValueError,match='计数'):
        main.create_app(settings=Settings(env='test',_env_file=None),model_registry=registry(),token_budget=TokenBudget({'deepseek_v41':FixtureCounter()}))
    assert prepared==[]


@pytest.mark.parametrize('field',['revision','model','mode','digest'])
def test_valid_manifest_with_incompatible_metadata_is_rejected(tmp_path,field):
    import json,shutil
    from services.token_counters.deepseek_v41 import DEFAULT_ASSET_DIR,DeepSeekV41Counter
    assets=tmp_path/'assets'
    shutil.copytree(DEFAULT_ASSET_DIR,assets)
    path=assets/'manifest.json';manifest=json.loads(path.read_text())
    if field=='digest':
        manifest['files']['tokenizer.json']['sha256']='invalid'
    else:
        manifest[field]='incompatible'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='计数资源'):
        DeepSeekV41Counter(assets)
