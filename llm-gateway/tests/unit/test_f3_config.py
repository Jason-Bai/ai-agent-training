from services.models import EndpointConfig, ModelGroupConfig
from services.models import ModelRegistry as RuntimeRegistry
from registry_helpers import ModelRegistry
from services.models import ConcurrencyPolicy
from pathlib import Path

import pytest
from pydantic import ValidationError

from bug002_cases import write_registry
from errors import GatewayError
from services.models import ProviderConfig, load_registry, DEFAULT_CONFIG_DIR
from services.credentials import load_credentials
from settings import Settings


def model_values():
    return dict(provider='mock-main', model_id='deepseek-flash', thinking_mode='disabled',
                token_counter='deepseek_v41', supports_streaming=True, structured_output_mode='none', context_window_tokens=1048576,
                provider_max_output_tokens=393216, default_output_tokens=1024,
                max_output_tokens=4096, safety_margin_tokens=1024,
                default_timeout_seconds=30, max_timeout_seconds=120)


def make_model(**changes):
    v=model_values();v.update(changes)
    group_keys={'default_output_tokens','default_timeout_seconds'}
    endpoint=EndpointConfig(**{k:v for k,v in v.items() if k not in group_keys},enabled=True)
    ModelGroupConfig(candidates=('fixture',),default_output_tokens=v['default_output_tokens'],
                     max_output_tokens=v['max_output_tokens'],default_timeout_seconds=v['default_timeout_seconds'],max_timeout_seconds=v['max_timeout_seconds'])
    return endpoint


def test_settings_has_no_provider_selector():
    assert not hasattr(Settings(env='test', _env_file=None), 'provider')


def test_default_registry_is_explicit_mock_without_credentials(tmp_path):
    write_registry(tmp_path)
    registry=load_registry(tmp_path)
    assert isinstance(registry, RuntimeRegistry)
    assert set(registry.endpoints)=={'general-primary','general-backup'}
    assert all(registry.providers[m.provider].adapter=='mock' for m in registry.endpoints.values())
    assert registry.get_group('general-primary').default_output_tokens==1024
    assert DEFAULT_CONFIG_DIR==Path(__file__).resolve().parents[2]/'config'


def test_unknown_model_is_gateway_error():
    registry=ModelRegistry({'test':make_model()}, {'mock-main':ProviderConfig(adapter='mock', api_protocol='chat_completions')}, concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))
    with pytest.raises(GatewayError) as e: registry.get_group('PRIVATE_INPUT_MARKER')
    assert e.value.code=='unknown_model'
    assert 'PRIVATE_INPUT_MARKER' not in str(e.value)


@pytest.mark.parametrize('field', ['context_window_tokens','provider_max_output_tokens','default_output_tokens','max_output_tokens','safety_margin_tokens','default_timeout_seconds','max_timeout_seconds'])
@pytest.mark.parametrize('value', [0,-1,True,'1024',1.5,None])
def test_model_numbers_are_strict_positive(field,value):
    with pytest.raises((ValueError,ValidationError)):make_model(**{field:value})


@pytest.mark.parametrize('changes', [dict(default_output_tokens=4097),dict(max_output_tokens=393217),dict(default_timeout_seconds=121),dict(context_window_tokens=2048),dict(provider=' '),dict(model_id=' '),dict(thinking_mode='enabled'),dict(extra='PRIVATE_INPUT_MARKER')])
def test_model_cross_constraints(changes):
    if changes==dict(context_window_tokens=2048):
        # F12 group defaults are independent; context fit is now request routing.
        assert make_model(**changes).context_window_tokens==2048
    else:
        with pytest.raises((ValueError,ValidationError)):make_model(**changes)


@pytest.mark.parametrize('values', [dict(adapter='bad'),dict(adapter='deepseek'),dict(adapter='openai_compatible',base_url='https://api.deepseek.com'),dict(adapter='deepseek',base_url='file:///private',credential_ref='key'),dict(adapter='deepseek',base_url='https://api.deepseek.com',credential_ref=' '),dict(adapter='mock',unknown=True)])
def test_provider_definition_rejects_invalid(values):
    with pytest.raises((ValueError,ValidationError)):ProviderConfig(**dict(api_protocol="responses" if values.get("adapter")=="openai_compatible" else "chat_completions", **values))


def test_real_provider_and_equal_budget_boundary():
    p=ProviderConfig(adapter='deepseek',api_protocol='chat_completions',base_url='https://api.deepseek.com',credential_ref='key')
    assert p.credential_ref=='key'
    m=make_model(default_output_tokens=4096,max_output_tokens=4096,provider_max_output_tokens=4096,default_timeout_seconds=120)
    assert m.max_output_tokens==4096


@pytest.mark.parametrize('models,providers', [({},{}),({'x':make_model()},{}),({'x':make_model(provider='absent')},{'mock-main':ProviderConfig(adapter='mock', api_protocol='chat_completions')}),({' ':make_model()},{'mock-main':ProviderConfig(adapter='mock', api_protocol='chat_completions')})])
def test_registry_rejects_invalid_references(models,providers):
    with pytest.raises(ValueError):ModelRegistry(models,providers, concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))


def test_registry_does_not_share_mutable_containers():
    m={'x':make_model()};p={'mock-main':ProviderConfig(adapter='mock', api_protocol='chat_completions')}
    registry=ModelRegistry(m,p, concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64));m.clear();p.clear()
    assert set(registry.endpoints)=={'x'}
    with pytest.raises(TypeError): registry.endpoints['new']=make_model()
    with pytest.raises((ValidationError,AttributeError)): registry.endpoints['x'].max_output_tokens=9
    other=ModelRegistry({'x':make_model(max_output_tokens=2048)},{'mock-main':ProviderConfig(adapter='mock', api_protocol='chat_completions')}, concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))
    assert other.endpoints['x'].max_output_tokens==2048
    assert registry.endpoints['x'].max_output_tokens==4096


@pytest.mark.parametrize('text', ['providers: {}\nmodels: {}','providers:\n  x: {adapter: mock, api_protocol: chat_completions}\n  x: {adapter: mock, api_protocol: chat_completions}\nmodels: {}','providers: [','models: {}\nextra: true','providers: {x: {adapter: mock, api_protocol: chat_completions, adapter: deepseek}}\nmodels: {}'])
def test_yaml_invalid_or_duplicate_is_safe(tmp_path,text):
    (tmp_path/'gateway.yaml').write_text(text)
    with pytest.raises(ValueError) as e:load_registry(tmp_path)
    assert 'PRIVATE' not in str(e.value)


def test_yaml_missing_and_environment_files_not_selected(tmp_path):
    with pytest.raises(ValueError):load_registry(tmp_path)
    write_registry(tmp_path)
    (tmp_path/'gateway.test.yaml').write_text('INVALID: [')
    (tmp_path/'gateway.production.yaml').write_text('INVALID: [')
    assert isinstance(load_registry(tmp_path),RuntimeRegistry)


def test_mock_credentials_need_no_file(tmp_path):
    assert load_credentials(tmp_path/'missing.yaml',set())=={}


def test_credentials_references_and_secret_representation(tmp_path):
    f=tmp_path/'credentials.yaml';f.write_text('credentials:\n  real:\n    api_key: FAKE_CREDENTIAL_MARKER\n')
    loaded=load_credentials(f,{'real'})
    assert isinstance(loaded,dict)
    assert loaded['real'].get_secret_value()=='FAKE_CREDENTIAL_MARKER'
    assert 'FAKE_CREDENTIAL_MARKER' not in repr(loaded)
    with pytest.raises(ValueError):load_credentials(f,{'absent'})


@pytest.mark.parametrize('text', ['credentials: {}','credentials: {real: {api_key: " "}}','credentials: {real: {api_key: FAKE_CREDENTIAL_MARKER, extra: secret}}','credentials: {real: {api_key: FAKE_CREDENTIAL_MARKER}, real: {api_key: b}}','credentials: {real: {api_key: 123}}','credentials: {real: {api_key: null}}'])
def test_invalid_credentials_do_not_echo_secrets(tmp_path,text):
    f=tmp_path/'credentials.yaml';f.write_text(text)
    with pytest.raises(ValueError) as e:load_credentials(f,{'real'})
    assert 'FAKE_CREDENTIAL_MARKER' not in str(e.value)
