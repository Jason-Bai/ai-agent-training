"""F12 real-YAML contract checks; no runtime configuration or Provider access."""
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from errors import GatewayError
from services.models import ConfigurationError, load_registry


def document():
    endpoint = dict(provider='mock-main', model_id='deepseek-flash', thinking_mode='disabled',
                    enabled=True, token_counter='deepseek_v41', supports_streaming=True,
                    structured_output_mode='json_object', context_window_tokens=1048576,
                    provider_max_output_tokens=393216, max_output_tokens=4096,
                    safety_margin_tokens=1024, max_timeout_seconds=120)
    group = dict(candidates=['chat'], default_output_tokens=1024, max_output_tokens=4096,
                 default_timeout_seconds=30, max_timeout_seconds=120)
    return dict(providers={'mock-main': dict(adapter='mock', api_protocol='chat_completions')},
                endpoints={'chat': endpoint}, model_groups={'general-primary': group},
                concurrency=dict(max_requests=20, max_requests_per_agent=5))


def load(tmp_path, data):
    (tmp_path / 'gateway.yaml').write_text(yaml.safe_dump(data), encoding='utf-8')
    return load_registry(tmp_path)


def test_valid_three_layer_yaml_resolves_group_to_different_endpoint(tmp_path):
    registry = load(tmp_path, document())
    assert registry.candidates('general-primary') == ('chat',)
    assert registry.get_endpoint('chat').enabled is True
    assert registry.get_group('general-primary').default_output_tokens == 1024
    assert registry.recovery.max_attempts_per_endpoint == 2


@pytest.mark.parametrize('section', ['providers', 'endpoints', 'model_groups', 'concurrency'])
def test_required_sections(tmp_path, section):
    data = document(); del data[section]
    with pytest.raises(ConfigurationError): load(tmp_path, data)


@pytest.mark.parametrize('section', ['providers', 'endpoints', 'model_groups'])
def test_empty_definitions_rejected(tmp_path, section):
    data = document(); data[section] = {}
    with pytest.raises(ConfigurationError): load(tmp_path, data)


@pytest.mark.parametrize('section', ['providers', 'endpoints', 'model_groups', 'concurrency'])
def test_wrong_mapping_types_are_safe_errors(tmp_path, section):
    for value in (None, True, [], 'private-marker', 1):
        data = document(); data[section] = value
        with pytest.raises(ConfigurationError) as error: load(tmp_path, data)
        assert 'private-marker' not in str(error.value)


@pytest.mark.parametrize('field', ['enabled','supports_streaming'])
@pytest.mark.parametrize('value', [None, 0, 1, 'true', 1.5])
def test_boolean_fields_strict(tmp_path, field, value):
    data = document(); data['endpoints']['chat'][field] = value
    with pytest.raises(ConfigurationError): load(tmp_path, data)


def test_enabled_is_required_even_for_disabled_endpoints(tmp_path):
    data=document(); del data['endpoints']['chat']['enabled']
    with pytest.raises(ConfigurationError): load(tmp_path,data)


@pytest.mark.parametrize('field', ['context_window_tokens','provider_max_output_tokens','max_output_tokens',
                                  'safety_margin_tokens','max_timeout_seconds'])
@pytest.mark.parametrize('value', [0, -1, True, '1', 1.5, None])
def test_endpoint_positive_integer_fields(tmp_path, field, value):
    data=document(); data['endpoints']['chat'][field]=value
    with pytest.raises(ConfigurationError): load(tmp_path,data)


@pytest.mark.parametrize('field', ['default_output_tokens','max_output_tokens','default_timeout_seconds','max_timeout_seconds'])
@pytest.mark.parametrize('value', [0, -1, True, '1', 1.5, None])
def test_group_positive_integer_fields(tmp_path, field, value):
    data=document(); data['model_groups']['general-primary'][field]=value
    with pytest.raises(ConfigurationError): load(tmp_path,data)


@pytest.mark.parametrize('candidates', [[], ['chat','chat'], ['missing'], ['general-primary'], 'chat', None, [1]])
def test_invalid_group_candidates(tmp_path,candidates):
    data=document(); data['model_groups']['general-primary']['candidates']=candidates
    with pytest.raises(ConfigurationError): load(tmp_path,data)


@pytest.mark.parametrize('section,key', [('endpoints','provider'),('endpoints','token_counter')])
def test_nonblank_reference_values(tmp_path,section,key):
    data=document(); data[section]['chat'][key]=' '
    with pytest.raises(ConfigurationError): load(tmp_path,data)


def test_unknown_provider_and_protocol_mode_are_rejected(tmp_path):
    data=document(); data['endpoints']['chat']['provider']='missing'
    with pytest.raises(ConfigurationError): load(tmp_path,data)
    data=document(); data['endpoints']['chat'].update(enabled=False, structured_output_mode='json_schema')
    with pytest.raises(ConfigurationError): load(tmp_path,data)


@pytest.mark.parametrize('section', ['providers','endpoints','model_groups'])
def test_definition_names_are_nonblank_strings(tmp_path,section):
    for name in ('', ' ', 123):
        data=document(); value=next(iter(data[section].values()));data[section]={name:value}
        with pytest.raises(ConfigurationError):load(tmp_path,data)


def test_names_are_not_silently_trimmed(tmp_path):
    data=document();data['endpoints'][' chat ']=data['endpoints'].pop('chat')
    data['model_groups']['general-primary']['candidates']=[' chat ']
    r=load(tmp_path,data);assert r.candidates('general-primary')==(' chat ',)
    with pytest.raises(GatewayError) as e:r.get_group(' general-primary ')
    assert e.value.code=='unknown_model'


@pytest.mark.parametrize('change', ['endpoint_max','group_output','group_timeout'])
def test_budget_relationships(tmp_path,change):
    data=document()
    if change=='endpoint_max':data['endpoints']['chat']['max_output_tokens']=393217
    elif change=='group_output':data['model_groups']['general-primary']['default_output_tokens']=4097
    else:data['model_groups']['general-primary']['default_timeout_seconds']=121
    with pytest.raises(ConfigurationError):load(tmp_path,data)


def test_group_can_exceed_one_endpoint_and_all_disabled_can_start(tmp_path):
    data=document();data['endpoints']['chat'].update(max_output_tokens=64,enabled=False)
    data['model_groups']['another']=deepcopy(data['model_groups']['general-primary'])
    r=load(tmp_path,data)
    assert r.get_group('another').max_output_tokens==4096
    assert r.get_endpoint('chat').max_output_tokens==64
    assert not r.get_endpoint('chat').enabled


@pytest.mark.parametrize('change', ['models','mixed','endpoint_default','endpoint_fallback','old_recovery','extra'])
def test_old_or_unknown_fields_are_rejected(tmp_path,change):
    data=document()
    if change=='models':data['models']=data.pop('endpoints')
    elif change=='mixed':data['models']={}
    elif change=='endpoint_default':data['endpoints']['chat']['default_output_tokens']=16
    elif change=='endpoint_fallback':data['endpoints']['chat']['fallback_models']=[]
    elif change=='old_recovery':data['recovery']={'max_attempts_per_model':2}
    else:data['private-marker']='secret-marker'
    with pytest.raises(ConfigurationError) as e:load(tmp_path,data)
    assert 'secret-marker' not in str(e.value)


@pytest.mark.parametrize('value',[0,3,True,'2',1.5,None])
def test_endpoint_recovery_limit_is_strict(tmp_path,value):
    data=document();data['recovery']={'max_attempts_per_endpoint':value}
    with pytest.raises(ConfigurationError):load(tmp_path,data)


def test_recovery_limit_relationship(tmp_path):
    data=document();data['recovery']={'max_attempts':1,'max_attempts_per_endpoint':2}
    with pytest.raises(ConfigurationError):load(tmp_path,data)


def test_registry_and_group_candidates_are_immutable(tmp_path):
    data=document();r=load(tmp_path,data)
    for mapping in (r.endpoints,r.providers,r.model_groups):
        with pytest.raises(TypeError):mapping['injected']=None
    with pytest.raises((ValidationError,AttributeError)):r.get_endpoint('chat').enabled=False
    with pytest.raises((ValidationError,AttributeError)):r.get_group('general-primary').candidates=('other',)
    data['model_groups']['general-primary']['candidates'].append('other')
    assert r.candidates('general-primary')==('chat',)


def test_duplicate_yaml_keys_are_rejected(tmp_path):
    raw=yaml.safe_dump(document())+'\nmodel_groups: {}\n'
    (tmp_path/'gateway.yaml').write_text(raw)
    with pytest.raises(ConfigurationError):load_registry(tmp_path)


@pytest.mark.parametrize('field', ['candidates','default_output_tokens','max_output_tokens',
                                  'default_timeout_seconds','max_timeout_seconds'])
def test_group_fields_are_required(tmp_path,field):
    data=document();del data['model_groups']['general-primary'][field]
    with pytest.raises(ConfigurationError):load(tmp_path,data)


@pytest.mark.parametrize('section', ['endpoints','model_groups'])
def test_nested_unknown_fields_are_rejected(tmp_path,section):
    data=document();next(iter(data[section].values()))['unknown']='private-marker'
    with pytest.raises(ConfigurationError) as e:load(tmp_path,data)
    assert 'private-marker' not in str(e.value)


def test_example_loads_with_distinct_endpoint_ids(tmp_path):
    source=Path(__file__).resolve().parents[2]/'config/gateway.example.yaml'
    (tmp_path/'gateway.yaml').write_bytes(source.read_bytes())
    r=load_registry(tmp_path)
    assert r.candidates('general-primary')==('primary-chat','backup-responses')
    assert r.candidates('general-backup')==('backup-responses',)
    assert r.recovery.max_attempts_per_endpoint==2


def test_counter_validation_includes_disabled_endpoint(tmp_path):
    from services.token_budget import TokenBudget
    data=document();data['endpoints']['chat'].update(enabled=False,token_counter='unregistered')
    r=load(tmp_path,data)
    with pytest.raises(ConfigurationError):TokenBudget.from_models(r.endpoints.values())
