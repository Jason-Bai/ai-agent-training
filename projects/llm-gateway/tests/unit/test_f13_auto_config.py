"""B01: actual YAML parsing, reference safety and frozen automatic routing."""
from copy import deepcopy
from dataclasses import replace

import pytest
from pydantic import ValidationError

from services.models import ConfigurationError, ModelRegistry
from test_f12_config import document, load


def auto_document():
    data=document()
    base=data['model_groups'].pop('general-primary')
    for name in ('light','general','analysis'):
        data['model_groups'][name]=deepcopy(base)|{'routing_description':f'{name}受控用途'}
    data['auto_routes']={'auto-general':{'classifier_group':'light',
        'candidate_groups':['light','general','analysis'],'default_group':'general',
        'classifier_prompt':{'name':'task_router','version':'1.0.0'}}}
    return data


def test_valid_automatic_alias_and_defaults_are_loaded_without_calls(tmp_path):
    registry=load(tmp_path,auto_document())
    auto=registry.get_auto_route('auto-general')
    assert auto.classifier_group=='light' and auto.default_group=='general'
    assert auto.candidate_groups==('light','general','analysis')
    assert auto.classifier_prompt.name=='task_router' and auto.classifier_prompt.version=='1.0.0'
    assert (auto.classifier_timeout_seconds,auto.classifier_max_output_tokens)==(5,128)
    assert (auto.default_output_tokens,auto.max_output_tokens)==(1024,4096)
    assert (auto.default_timeout_seconds,auto.max_timeout_seconds)==(30,120)
    assert registry.get_auto_route('general') is None and registry.get_auto_route('missing') is None


def test_old_configuration_has_empty_immutable_auto_mapping(tmp_path):
    registry=load(tmp_path,document())
    assert not registry.auto_routes
    with pytest.raises(TypeError):registry.auto_routes['new']=None
    positional=ModelRegistry(registry.endpoints,registry.providers,registry.model_groups,
                             registry.concurrency,registry.recovery)
    assert not positional.auto_routes


@pytest.mark.parametrize('field',['classifier_timeout_seconds','classifier_max_output_tokens',
    'default_output_tokens','max_output_tokens','default_timeout_seconds','max_timeout_seconds'])
@pytest.mark.parametrize('value',[0,-1,True,'5',1.5,None])
def test_auto_budgets_are_strict_positive_integers(tmp_path,field,value):
    data=auto_document();data['auto_routes']['auto-general'][field]=value
    with pytest.raises(ConfigurationError):load(tmp_path,data)


@pytest.mark.parametrize('change',[{'classifier_timeout_seconds':6},
    {'classifier_max_output_tokens':129},{'max_timeout_seconds':121},{'max_output_tokens':4097},
    {'default_output_tokens':4097},{'default_timeout_seconds':121},
    {'max_output_tokens':100},{'max_timeout_seconds':10}])
def test_auto_caps_and_default_relationships_are_enforced(tmp_path,change):
    data=auto_document();data['auto_routes']['auto-general'].update(change)
    with pytest.raises(ConfigurationError):load(tmp_path,data)


@pytest.mark.parametrize('candidates',[[],['general','general'],['missing'],['auto-general'],
    'general',None,[1],[''],[' ']])
def test_candidate_groups_must_be_nonempty_distinct_ordinary_references(tmp_path,candidates):
    data=auto_document();data['auto_routes']['auto-general']['candidate_groups']=candidates
    with pytest.raises(ConfigurationError):load(tmp_path,data)


@pytest.mark.parametrize('field,value',[('classifier_group','auto-general'),('classifier_group','missing'),
    ('default_group','auto-general'),('default_group','missing'),('classifier_group',''),
    ('default_group',' ')])
def test_classifier_and_default_are_real_group_references(tmp_path,field,value):
    data=auto_document();data['auto_routes']['auto-general'][field]=value
    with pytest.raises(ConfigurationError):load(tmp_path,data)


def test_default_group_must_also_be_a_candidate(tmp_path):
    data=auto_document();data['auto_routes']['auto-general']['candidate_groups']=['light','analysis']
    with pytest.raises(ConfigurationError):load(tmp_path,data)


@pytest.mark.parametrize('name',['light','general','analysis'])
@pytest.mark.parametrize('description',[None,'',' ',17,{},[]])
def test_every_candidate_has_valid_controlled_description(tmp_path,name,description):
    data=auto_document();data['model_groups'][name]['routing_description']=description
    with pytest.raises(ConfigurationError):load(tmp_path,data)


def test_classifier_only_group_may_have_no_description(tmp_path):
    data=auto_document();data['auto_routes']['auto-general']['candidate_groups']=['general','analysis']
    data['model_groups']['light'].pop('routing_description')
    assert load(tmp_path,data).get_auto_route('auto-general').classifier_group=='light'


@pytest.mark.parametrize('value',[{},None,[],True,'task_router',
    {'name':'task_router'},{'version':'1.0.0'},
    {'name':'task_router','version':'1.0.0','variables':{}},
    {'name':'','version':'1.0.0'},{'name':'task_router','version':'latest'}])
def test_classifier_prompt_is_explicit_strict_name_and_version_only(tmp_path,value):
    data=auto_document();data['auto_routes']['auto-general']['classifier_prompt']=value
    with pytest.raises(ConfigurationError):load(tmp_path,data)


@pytest.mark.parametrize('name',['light','', ' ',1])
def test_auto_alias_cannot_collide_or_have_invalid_name(tmp_path,name):
    data=auto_document();data['auto_routes']={name:data['auto_routes']['auto-general']}
    with pytest.raises(ConfigurationError):load(tmp_path,data)


@pytest.mark.parametrize('value',[None,[],False,'auto-general',1])
def test_auto_routes_requires_a_mapping(tmp_path,value):
    data=auto_document();data['auto_routes']=value
    with pytest.raises(ConfigurationError):load(tmp_path,data)


@pytest.mark.parametrize('change',[{'max_output_tokens':127,'default_output_tokens':100},
    {'max_timeout_seconds':4,'default_timeout_seconds':4}])
def test_fixed_classifier_budget_cannot_exceed_group_limit(tmp_path,change):
    data=auto_document();data['model_groups']['light'].update(change)
    with pytest.raises(ConfigurationError):load(tmp_path,data)


@pytest.mark.parametrize('change',[{'max_output_tokens':127},{'min_output_tokens':129},
    {'max_timeout_seconds':4},{'structured_output_mode':'none'},
    {'context_window_tokens':1152}])
def test_classifier_requires_at_least_one_physically_compatible_endpoint(tmp_path,change):
    data=auto_document();data['endpoints']['chat'].update(change)
    with pytest.raises(ConfigurationError):load(tmp_path,data)


def test_disabled_but_physically_compatible_classifier_is_allowed_at_startup(tmp_path):
    data=auto_document();data['endpoints']['chat']['enabled']=False
    assert load(tmp_path,data).get_auto_route('auto-general') is not None


def test_different_endpoints_cannot_collectively_fake_one_compatible_endpoint(tmp_path):
    data=auto_document();data['endpoints']['second']=deepcopy(data['endpoints']['chat'])
    data['endpoints']['chat'].update(max_timeout_seconds=4)
    data['endpoints']['second'].update(max_output_tokens=127)
    data['model_groups']['light']['candidates']=['chat','second']
    with pytest.raises(ConfigurationError):load(tmp_path,data)


def test_one_compatible_endpoint_is_sufficient_even_if_another_is_not(tmp_path):
    data=auto_document();data['endpoints']['second']=deepcopy(data['endpoints']['chat'])
    data['endpoints']['chat'].update(max_timeout_seconds=4)
    data['model_groups']['light']['candidates']=['chat','second']
    assert load(tmp_path,data).get_auto_route('auto-general') is not None


def test_registry_copies_inputs_and_profiles_are_deeply_frozen(tmp_path):
    data=auto_document();registry=load(tmp_path,data);auto=registry.get_auto_route('auto-general')
    with pytest.raises(ValidationError):auto.classifier_prompt.name='override'
    with pytest.raises(ValidationError):auto.default_group='analysis'
    with pytest.raises(TypeError):registry.auto_routes['other']=auto
    mapping={'auto-general':auto};copy=replace(registry,auto_routes=mapping);mapping.clear()
    assert copy.get_auto_route('auto-general') is auto


def test_mutually_referencing_automatic_aliases_are_rejected(tmp_path):
    data=auto_document();second=deepcopy(data['auto_routes']['auto-general'])
    data['auto_routes']['other']=second
    data['auto_routes']['auto-general']['classifier_group']='other'
    second['classifier_group']='auto-general'
    with pytest.raises(ConfigurationError):load(tmp_path,data)


def test_classifier_budget_equal_to_group_and_endpoint_limits_is_valid(tmp_path):
    data=auto_document()
    data['model_groups']['light'].update(default_output_tokens=128,max_output_tokens=128,
        default_timeout_seconds=5,max_timeout_seconds=5)
    data['endpoints']['chat'].update(min_output_tokens=128,max_output_tokens=128,max_timeout_seconds=5,
        context_window_tokens=1153)
    assert load(tmp_path,data).get_auto_route('auto-general').classifier_max_output_tokens==128


def test_missing_optional_descriptions_are_allowed_for_unreferenced_groups(tmp_path):
    data=auto_document();data['model_groups']['unused']=deepcopy(data['model_groups']['general'])
    data['model_groups']['unused'].pop('routing_description')
    assert load(tmp_path,data).get_group('unused').routing_description is None
