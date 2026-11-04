from registry_helpers import ModelRegistry
"""F7 K/R/T policy tests: no upstream calls or database."""
from services.models import ConcurrencyPolicy
from importlib.util import find_spec
from types import SimpleNamespace
import pytest
import yaml
from pydantic import ValidationError
from f3_cases import MODEL_VALUES
from services import models


def policy(**kw):
    assert hasattr(models,'RecoveryPolicy'), 'F7 policy is missing'
    return models.RecoveryPolicy(**kw)


def state(initial=10, maximum=40, **kw):
    assert find_spec('services.recovery'), 'F7 shared recovery module is missing'
    from services.recovery import RecoveryState
    return RecoveryState(policy(**kw),initial,maximum)


def error(code,**kw): return SimpleNamespace(code=code,**kw)


def registry(links=None,**kw):
    return ModelRegistry({k:dict(MODEL_VALUES,fallback_models=v) for k,v in (links or {'a':[],'b':[]}).items()},
        {'mock-main':dict(adapter='mock',api_protocol='chat_completions')},**kw, concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))


def test_policy_registry_defaults():
    assert policy().model_dump()==dict(max_attempts=3,max_attempts_per_endpoint=2,base_delay_seconds=.1,max_delay_seconds=1.)
    assert registry().recovery==policy()
    assert registry().model_groups['a'].candidates==('a',)


@pytest.mark.parametrize('kw',[{'max_attempts':v} for v in [0,4,True,'3',1.5,None]]+
    [{'max_attempts_per_endpoint':v} for v in [0,3,True,'2',1.5,None]]+
    [dict(max_attempts=1),dict(base_delay_seconds=-1),dict(base_delay_seconds=2),dict(max_delay_seconds=-1),
     dict(base_delay_seconds=True),dict(base_delay_seconds='0.1'),dict(max_delay_seconds=float('nan')),
     dict(base_delay_seconds=float('inf')),dict(attempt_timeout_seconds=10)])
def test_bad_policy(kw):
    with pytest.raises(ValidationError): policy(**kw)


def test_strict_one_attempt_zero_delay():
    assert policy(max_attempts=1,max_attempts_per_endpoint=1,base_delay_seconds=0,max_delay_seconds=0).max_attempts==1


@pytest.mark.parametrize('links',[{'a':['missing']},{'a':['b'],'b':['missing']},{'a':['a']},{'a':['b'],'b':['a']},
    {'a':['b','b'],'b':[]},{'a':['b','c'],'b':[],'c':[]},{'a':None},{'a':'b'},{'a':[1]}])
def test_invalid_fallback(links):
    if links in ({'a':['b'],'b':['a']}, {'a':['b','c'],'b':[],'c':[]}):
        result=registry(links)
        assert result.candidates('a')==('a',*links['a'])
    else:
        with pytest.raises((ValueError, TypeError)): registry(links)


def test_candidates_nonrecursive_immutable():
    source=['b'];r=registry({'a':source,'b':['c'],'c':[]});source.clear()
    assert r.candidates('a')==('a','b')
    assert r.model_groups['a'].candidates==('a','b')


def test_yaml_recovery(tmp_path):
    r=registry({'a':['b'],'b':[]})
    values=dict(model_groups={k:v.model_dump(mode='json') for k,v in r.model_groups.items()},endpoints={k:v.model_dump(mode='json') for k,v in r.endpoints.items()},
        providers={k:v.model_dump() for k,v in r.providers.items()},concurrency=r.concurrency.model_dump(),recovery=dict(max_attempts=1,max_attempts_per_endpoint=1))
    (tmp_path/'gateway.yaml').write_text(yaml.safe_dump(values))
    result=models.load_registry(tmp_path)
    assert result.recovery.max_attempts==1 and result.candidates('a')==('a','b')


@pytest.mark.parametrize('code',['provider_connection_error','provider_timeout','provider_rate_limited'])
def test_shared_attempt_limit(code):
    s=state();s.start('a');assert s.decide(error(code),'a')=='retry'
    s.start('a');assert s.decide(error(code),'a')=='fallback'
    s.start('b');assert s.decide(error(code),'b')=='stop'
    assert s.total==3 and s.counts=={'a':2,'b':1}
    with pytest.raises(ValueError):s.start('b')


@pytest.mark.parametrize('code',['provider_auth_error','provider_quota_exceeded','provider_rejected_request',
    'provider_error','provider_invalid_response','provider_refused','provider_content_filtered',
    'storage_unavailable','internal_error','request_timeout','cancel_cleanup_failed'])
def test_permanent_error(code):
    s=state();s.start('a');assert s.decide(error(code),'a',structured=True)=='stop'


@pytest.mark.parametrize('metadata,expected',[({},'stop'),({'upstream_http_status':501},'stop'),
    ({'upstream_http_status':505},'stop')]+[({'upstream_http_status':n},'retry') for n in [500,502,503,504]]+
    [({'transient':True},'retry'),({'finish_reason':'aborted'},'retry'),({'finish_reason':'insufficient_system_resource'},'retry')])
def test_transient_evidence(metadata,expected):
    s=state();s.start('a');assert s.decide(error('provider_unavailable',**metadata),'a')==expected


@pytest.mark.parametrize('code',['provider_connection_error','output_truncated','invalid_json','schema_validation_failed'])
def test_content_blocks_recovery(code):
    s=state();s.start('a');assert s.decide(error(code),'a',has_content=True,structured=True)=='stop'


@pytest.mark.parametrize('code',['invalid_json','schema_validation_failed'])
def test_one_repair_stops_other_recovery(code):
    s=state();s.start('a');assert s.decide(error(code),'a',structured=True)=='repair';assert s.repair_used
    s.start('a')
    for next_code in [code,'output_truncated','provider_connection_error']:
        assert s.decide(error(next_code),'a',structured=True)=='stop'
    s=state();s.start('a');assert s.decide(error(code),'a')=='stop'


def test_growth_and_fallback_keep_cap():
    s=state();s.start('a');assert s.decide(error('output_truncated'),'a')=='grow';assert s.current_output==40
    s.start('a');assert s.decide(error('provider_connection_error'),'a')=='fallback'
    s.start('b');assert s.current_output==40;assert s.decide(error('output_truncated'),'b')=='stop'


def test_truncation_at_max_or_local_limit_stops():
    s=state(initial=40);s.start('a');assert s.decide(error('output_truncated'),'a')=='stop'
    s=state();s.start('a');s.start('a');assert s.decide(error('output_truncated'),'a')=='stop'


@pytest.mark.parametrize('sample,expected',[(0,0),(.5,.05),(1,.1)])
def test_jitter_and_upstream_wait(sample,expected):
    s=state();s.start('a');assert s.delay(error('provider_connection_error'),'a',random_value=sample)==expected
    assert s.delay(error('provider_rate_limited',retry_after_seconds=15),'a',random_value=sample)==15


def test_backoff_cap():
    s=state(base_delay_seconds=.75,max_delay_seconds=1);s.start('a');s.start('a')
    assert s.delay(error('provider_connection_error'),'a',random_value=1)==1


def test_wait_domain_is_request_local():
    a=models.ProviderConfig(adapter='deepseek',api_protocol='chat_completions',base_url='https://EXAMPLE.com/v1',credential_ref='same')
    b=models.ProviderConfig(adapter='openai_compatible',api_protocol='responses',base_url='https://example.com:443/v2',credential_ref='same')
    c=b.model_copy(update={'credential_ref':'different'})
    s=state();s.note_wait(a,15,now=100)
    assert s.wait_remaining(b,now=105)==10
    assert s.wait_remaining(c,now=105)==0
    assert state().wait_remaining(a,now=105)==0
    assert s.wait_remaining(a,now=120)==0


@pytest.mark.parametrize('case,expected',[
    ('content','content_committed'),('repair','repair_finished'),
    ('global','attempts_exhausted'),('local_growth','model_attempts_exhausted'),
    ('local_repair','model_attempts_exhausted'),('at_max','output_limit_reached'),
    ('permanent','nonrecoverable_error'),('text_json','format_repair_not_applicable')])
def test_recovery_stop_reason_is_explicit(case,expected):
    s=state(initial=40 if case=='at_max' else 10);s.start('a')
    code='provider_connection_error';options={}
    if case=='content':options['has_content']=True
    elif case=='repair':
        assert s.decide(error('invalid_json'),'a',structured=True)=='repair'
    elif case=='global':s.start('a');s.start('b')
    elif case in {'local_growth','local_repair'}:
        s.start('a');code='output_truncated' if case=='local_growth' else 'invalid_json';options['structured']=True
    elif case=='at_max':code='output_truncated'
    elif case=='permanent':code='provider_auth_error'
    else:code='invalid_json'
    assert s.decide(error(code),'a',**options)=='stop'
    assert getattr(s,'stop_reason',None)==expected


def test_recovery_nonstop_decisions_clear_old_stop_reason():
    s=state();s.start('a')
    assert s.decide(error('provider_auth_error'),'a')=='stop'
    assert getattr(s,'stop_reason',None)=='nonrecoverable_error'
    assert s.decide(error('provider_connection_error'),'a')=='retry'
    assert s.stop_reason is None
