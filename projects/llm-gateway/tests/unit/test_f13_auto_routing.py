"""B03-B07: deterministic classification through the production coordinator."""
import asyncio
from dataclasses import replace
import pytest

from f12_cases import request
from f13_cases import registry,service
from providers.base import ProviderResult
from errors import GatewayError,ProviderCallError
from services.streaming_manager import StreamingManager
from utils.json_codec import dumps


async def test_no_eligible_group_is_a_local_rejection():
    definitions=registry({name+'-e':{'enabled':False} for name in ('light','general','analysis')})
    gateway,trace,provider,traces=service(definitions)
    with pytest.raises(GatewayError) as caught:await gateway.complete(request(model='auto-general'),trace)
    assert caught.value.code=='no_available_group'
    assert trace.auto_routing['resolved_group'] is None
    assert len(trace.auto_routing['candidate_groups'])==3
    assert not provider.calls and not traces.attempts


async def test_single_eligible_group_skips_classification():
    definitions=registry({'light-e':{'enabled':False},'analysis-e':{'enabled':False}})
    gateway,trace,provider,traces=service(definitions,ProviderResult(text='唯一回答'))
    result=await gateway.complete(request(model='auto-general'),trace)
    assert result.text=='唯一回答' and len(provider.calls)==1
    assert trace.requested_model=='auto-general' and trace.auto_routing['resolved_group']=='general'
    assert trace.auto_routing['source']=='single_eligible_group'
    assert [a.purpose for a in traces.attempts]==['generation']


@pytest.mark.parametrize('choice',['light','general','analysis'])
async def test_classification_and_generation_share_request_and_continuous_numbers(choice):
    gateway,trace,provider,traces=service(None,ProviderResult(text='{"group":"'+choice+'"}'),
                                       ProviderResult(text='最终回答'))
    result=await gateway.complete(request(model='auto-general'),trace)
    assert result.text=='最终回答' and len(provider.calls)==2
    assert trace.auto_routing['resolved_group']==choice and trace.auto_routing['source']=='classifier'
    assert trace.auto_routing['fallback_reason'] is None
    assert [(a.number,a.purpose,a.request_id) for a in traces.attempts]==[
        (1,'classification',trace.request_id),(2,'generation',trace.request_id)]
    assert traces.attempts[1].model==choice+'-e'
    assert provider.calls[0][2]['max_output_tokens']==128
    assert provider.calls[0][2]['timeout_seconds']<=5
    assert gateway.limiter.snapshot()['active_requests']==0


@pytest.mark.parametrize('text',['broken','[]','{}','{"group":"unknown"}',
    '{"group":"auto-general"}','{"group":"general","extra":true}'])
async def test_invalid_classification_uses_checked_default_without_repair(text):
    gateway,trace,provider,traces=service(None,ProviderResult(text=text),ProviderResult(text='默认回答'))
    result=await gateway.complete(request(model='auto-general'),trace)
    assert result.text=='默认回答' and len(provider.calls)==2
    assert trace.auto_routing['resolved_group']=='general'
    assert trace.auto_routing['source']=='default_fallback' and trace.auto_routing['fallback_reason']
    assert traces.attempts[0].status=='failed' and traces.attempts[0].purpose=='classification'
    assert getattr(trace,'_recovery_stop_reason',None) is None


@pytest.mark.parametrize('code',['provider_unavailable','provider_connection_error','provider_rate_limited','provider_timeout'])
async def test_recoverable_classification_failure_is_one_call_then_default(code):
    gateway,trace,provider,traces=service(None,ProviderCallError(code=code),ProviderResult(text='默认回答'))
    assert (await gateway.complete(request(model='auto-general'),trace)).text=='默认回答'
    assert len(provider.calls)==2 and trace.auto_routing['fallback_reason']==code
    assert [a.number for a in traces.attempts]==[1,2]


@pytest.mark.parametrize('code',['provider_auth_error','provider_quota_exceeded'])
async def test_auth_and_quota_never_trigger_default(code):
    gateway,trace,provider,traces=service(None,ProviderCallError(code=code))
    with pytest.raises(ProviderCallError) as caught:await gateway.complete(request(model='auto-general'),trace)
    assert caught.value.code==code and len(provider.calls)==1 and len(traces.attempts)==1


async def test_default_excluded_does_not_silently_choose_a_third_group():
    definitions=registry({'general-e':{'enabled':False}})
    gateway,trace,provider,traces=service(definitions,ProviderResult(text='bad'))
    with pytest.raises(GatewayError) as caught:await gateway.complete(request(model='auto-general'),trace)
    assert caught.value.code=='auto_route_failed' and len(provider.calls)==1


def test_pending_auto_snapshot_restores_omitted_optional_fields_without_calls():
    gateway,trace,provider,traces=service()
    execution=gateway.prepare_stream(request(streaming=True,model='auto-general'),trace)
    manager=StreamingManager(None,traces,gateway.model_registry,gateway.token_budget,
        gateway.providers,None,set(),awaiting_persistence=lambda _:False,
        limiter=gateway.limiter,prompt_registry=gateway.prompt_registry)
    restored=manager._restore({'input_snapshot':dumps(execution.snapshot),
                               'requested_model':'auto-general'},trace)
    assert list(restored.plans)==['light','general','analysis']
    assert not provider.calls and not traces.attempts


async def test_generation_keeps_its_own_retry_budget_after_classification():
    definitions=registry();groups=dict(definitions.model_groups)
    groups['light']=groups['light'].model_copy(update={'candidates':('light-e','general-e')})
    definitions=replace(definitions,model_groups=groups)
    gateway,trace,provider,traces=service(definitions,ProviderResult(text='{"group":"light"}'),
        ProviderCallError(code='provider_connection_error',transient=True),
        ProviderCallError(code='provider_unavailable',transient=True),ProviderResult(text='恢复成功'))
    assert (await gateway.complete(request(model='auto-general'),trace)).text=='恢复成功'
    assert [a.number for a in traces.attempts]==[1,2,3,4]
    assert [a.purpose for a in traces.attempts]==['classification','generation','generation','generation']
    assert [a.model for a in traces.attempts]==['light-e','light-e','light-e','general-e']
    assert trace.auto_routing['resolved_group']=='light'


async def test_unknown_classification_exception_does_not_become_default_success():
    gateway,trace,provider,traces=service(None,RuntimeError('controlled program fault'))
    with pytest.raises(RuntimeError):await gateway.complete(request(model='auto-general'),trace)
    assert len(provider.calls)==1


@pytest.mark.parametrize('changes',[{'max_output_tokens':4097},{'timeout_seconds':121},
    {'initial_output_tokens':129,'max_output_tokens':128}])
async def test_auto_authorization_rejects_before_any_paid_attempt(changes):
    gateway,trace,provider,traces=service()
    with pytest.raises(GatewayError):await gateway.complete(request(model='auto-general',**changes),trace)
    assert not provider.calls and not traces.attempts


async def test_global_timeout_during_classification_never_starts_default_generation():
    gateway,trace,provider,traces=service()
    async def held(*args,**options):
        provider.calls.append(options)
        await asyncio.sleep(10)
    provider.complete=held
    with pytest.raises(GatewayError) as caught:
        await gateway.complete(request(model='auto-general',timeout_seconds=1),trace)
    assert caught.value.code=='request_timeout' and len(provider.calls)==1
    assert traces.attempts[0].purpose=='classification'
    assert gateway.limiter.snapshot()['active_requests']==0
