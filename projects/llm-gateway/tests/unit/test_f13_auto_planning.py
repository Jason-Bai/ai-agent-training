"""B03/B06/C07: local group plans and relevant configuration snapshots."""
from dataclasses import replace

import pytest

from errors import GatewayError
from f12_cases import registry, request, ENDPOINT
from services.auto_routing import plan_auto_route
from services.classification import CLASSIFIER_TEMPLATE
from services.models import AutoRouteConfig, EndpointConfig, ModelGroupConfig
from services.prompts import DEFAULT_TEMPLATES, PromptRegistry
from services.routing import Router
from services.token_budget import TokenBudget
from services.trace import RequestTrace
from utils.json_codec import dumps, loads


def setup(changes=None,profile_changes=None):
    base=registry();endpoints={name:EndpointConfig(**(ENDPOINT|{'provider':'mock-main'}|
        (changes or {}).get(name,{}))) for name in ('light','general','analysis')}
    groups={name:ModelGroupConfig(candidates=[name],default_output_tokens=128,
        max_output_tokens=4096,default_timeout_seconds=30,max_timeout_seconds=120,
        routing_description=f'{name}受控用途') for name in endpoints}
    profile=AutoRouteConfig(classifier_group='light',candidate_groups=list(groups),default_group='general',
        classifier_prompt={'name':'task_router','version':'1.0.0'},**(profile_changes or {}))
    definition=replace(base,endpoints=endpoints,model_groups=groups,auto_routes={'auto-general':profile})
    prompts=PromptRegistry([*DEFAULT_TEMPLATES,CLASSIFIER_TEMPLATE])
    router=Router(definition,prompts,TokenBudget.from_models(endpoints.values()))
    return router,request(model='auto-general'),RequestTrace('f13-local-plan')


def test_multiple_groups_produce_local_plan_not_a_provider_call():
    router,req,trace=setup()
    result=plan_auto_route(router,req,trace)
    assert result is not None,'automatic request needs a usable group plan before execution'
    assert tuple(result.plans)==('light','general','analysis')
    assert len(result.classifier.candidates)==1
    assert result.classifier.max_output_tokens==128 and result.classifier.timeout_seconds==5
    assert trace.requested_model=='auto-general'
    assert trace.auto_routing['resolved_group'] is None


@pytest.mark.parametrize('streaming',[False,True])
def test_no_eligible_group_is_local_failure_with_each_reason(streaming):
    router,req,trace=setup({n:{'enabled':False} for n in ('light','general','analysis')})
    with pytest.raises(GatewayError) as caught:plan_auto_route(router,req,trace,streaming=streaming)
    assert caught.value.code=='no_available_group' and caught.value.status_code==400
    assert trace.auto_routing['resolved_group'] is None
    assert all(row['code']=='no_available_endpoint' for row in trace.auto_routing['candidate_groups'])


def test_one_eligible_group_does_not_assemble_classifier(monkeypatch):
    router,req,trace=setup({'light':{'enabled':False},'analysis':{'enabled':False}})
    def forbidden(*args,**kwargs):pytest.fail('one eligible group must not prepare classifier')
    monkeypatch.setattr('services.auto_routing.prepare_classification',forbidden)
    result=plan_auto_route(router,req,trace)
    assert tuple(result.plans)==('general',) and result.classifier is None
    selected=result.select('general',trace,'single_eligible_group')
    assert selected.group_name=='general' and trace.requested_model=='auto-general'
    assert trace.auto_routing['source']=='single_eligible_group'


def test_auto_defaults_are_used_for_every_candidate_group():
    router,req,trace=setup(profile_changes={'default_output_tokens':321,'default_timeout_seconds':17})
    result=plan_auto_route(router,req,trace)
    assert result.max_output_tokens==321 and result.initial_output_tokens==321
    assert all(p.max_output_tokens==321 and p.timeout_seconds==17 for p in result.plans.values())


@pytest.mark.parametrize('changes,code',[({'max_output_tokens':4097},'invalid_output_budget'),
    ({'timeout_seconds':121},'invalid_timeout'),
    ({'max_output_tokens':128,'initial_output_tokens':129},'invalid_output_budget')])
def test_auto_authorization_cannot_be_bypassed_by_a_larger_group(changes,code):
    router,req,trace=setup()
    with pytest.raises(GatewayError) as caught:plan_auto_route(router,req.model_copy(update=changes),trace)
    assert caught.value.code==code


def test_candidate_filtering_and_selection_use_request_capability_and_own_metadata():
    router,req,trace=setup({'light':{'supports_streaming':False}})
    result=plan_auto_route(router,req,trace,streaming=True)
    assert tuple(result.plans)==('general','analysis')
    assert result.decisions[0]==('light','unsupported_capability')
    selected=result.select('analysis',trace,'classifier')
    assert trace.routing==selected.metadata()
    assert trace.input_token_count==selected.candidates[0].measured.input_tokens
    assert trace.counter_version==selected.candidates[0].measured.counter_version
    assert trace.auto_routing['resolved_group']=='analysis'


def test_shared_input_errors_are_not_relabelled_as_all_groups_unavailable():
    router,req,trace=setup()
    req=request(model='auto-general',prompt={'name':'context_answer','version':'1.1.0'})
    with pytest.raises(GatewayError) as caught:plan_auto_route(router,req,trace)
    assert caught.value.code=='missing_prompt_variable'


def test_unknown_classifier_template_does_not_silently_become_default_fallback():
    router,req,trace=setup()
    router.prompts=PromptRegistry(DEFAULT_TEMPLATES)
    with pytest.raises(GatewayError) as caught:plan_auto_route(router,req,trace)
    assert caught.value.code=='unknown_prompt_template'


def test_local_classifier_budget_failure_keeps_generation_plans():
    router,req,trace=setup({'light':{'enabled':False}})
    result=plan_auto_route(router,req,trace)
    assert tuple(result.plans)==('general','analysis')
    assert result.classifier is None and result.classifier_error=='no_available_endpoint'
    selected=result.select('general',trace,'default_fallback',result.classifier_error)
    assert selected.group_name=='general' and trace.auto_routing['fallback_reason']=='no_available_endpoint'


def test_snapshot_is_exact_json_and_contains_only_related_definitions():
    from services.auto_routing import auto_configuration_snapshot
    router,req,trace=setup();result=plan_auto_route(router,req,trace)
    snapshot=loads(dumps(result.snapshot()))
    assert snapshot['snapshot_version']==2 and snapshot['auto_request']['model']=='auto-general'
    assert set(snapshot['candidate_plans'])==set(result.plans)
    before=auto_configuration_snapshot(router,'auto-general',req)
    groups=dict(router.registry.model_groups);groups['unused']=groups['general']
    router.registry=replace(router.registry,model_groups=groups)
    assert auto_configuration_snapshot(router,'auto-general',req)==before
    groups['general']=groups['general'].model_copy(update={'routing_description':'changed'})
    router.registry=replace(router.registry,model_groups=groups)
    assert auto_configuration_snapshot(router,'auto-general',req)!=before


def test_plans_are_immutable_and_invalid_selection_is_not_accepted():
    router,req,trace=setup();result=plan_auto_route(router,req,trace)
    with pytest.raises(TypeError):result.plans['other']=result.plans['general']
    with pytest.raises(GatewayError):result.select('not-eligible',trace,'classifier')


def test_shared_render_overflow_keeps_prompt_error_before_any_count(monkeypatch):
    router,req,trace=setup()
    req=request(model='auto-general',
        prompt={'name':'context_answer','version':'1.1.0','variables':{'context':'C'*10000}},
        messages=[{'role':'user','content':'M'*10000}])
    def forbidden(*args,**kwargs):pytest.fail('invalid shared text must not reach token counting')
    monkeypatch.setattr(router.budget,'count',forbidden)
    with pytest.raises(GatewayError) as caught:plan_auto_route(router,req,trace)
    assert caught.value.code=='prompt_too_long'


def test_endpoint_schema_render_overflow_only_excludes_affected_groups():
    router,req,trace=setup({'general':{'provider':'mock-backup','structured_output_mode':'json_schema'}})
    req=request(model='auto-general',structured=True,
        response_schema={'type':'object','properties':{'order_id':{'type':'string','description':'S'*3000}},
                         'required':['order_id'],'additionalProperties':False},
        prompt={'name':'context_answer','version':'1.2.0','variables':{'context':'C'*9000}},
        messages=[{'role':'user','content':'M'*9000}])
    result=plan_auto_route(router,req,trace)
    assert tuple(result.plans)==('general',)
    assert result.decisions==(('light','prompt_too_long'),('general',None),('analysis','prompt_too_long'))
    assert result.classifier is None
