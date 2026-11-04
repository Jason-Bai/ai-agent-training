"""The confirmed capacity boundary accepts correct handling, not hidden failure."""
from copy import deepcopy

import pytest


def report(code='request_timeout'):
    success=dict(request_id='ok',status='completed',error_code=None,elapsed_ms=100)
    failure=dict(request_id='late',status='failed',error_code=code,elapsed_ms=30005)
    def observed(row):
        return dict(status=row['status'],error_code=row['error_code'],attempts=1,
                    bill_states=['pending' if row['status']=='failed' else 'settled'],
                    server_timing=dict(error_stage='provider' if row['status']=='failed' else None,
                                       cleanup_error_code=None,delivery_error_code=None),
                    external_failure_evidence=None)
    return dict(records=[success,failure],verified={r['request_id']:observed(r) for r in [success,failure]},
                after=dict(active_requests=0,active_generations=0,subscriptions=0,
                           cancellations=0,cleanup_tasks=0,agent_requests={},observation_errors=0))


def validate(value):
    import f13_capacity_metrics
    checker=getattr(f13_capacity_metrics,'validate_gateway_outcomes',None)
    assert callable(checker), 'missing confirmed layered capacity acceptance'
    checker(value,timeout_seconds=30)


@pytest.mark.parametrize('code',['request_timeout','provider_timeout','provider_unavailable'])
def test_correct_terminal_accounting_and_cleanup_accepts_reported_call_failure(code):
    value=report(code);original=deepcopy(value)
    validate(value)
    assert value==original, 'acceptance must not rewrite failures or attribute supplier fault'


@pytest.mark.parametrize('code',['storage_unavailable','internal_error','provider_auth_error'])
def test_local_or_unexpected_failure_still_blocks(code):
    with pytest.raises(AssertionError):validate(report(code))


@pytest.mark.parametrize('elapsed_ms',[5000,33000])
def test_premature_or_overrun_global_deadline_still_blocks(elapsed_ms):
    value=report();value['records'][1]['elapsed_ms']=elapsed_ms
    with pytest.raises(AssertionError):validate(value)


@pytest.mark.parametrize('field,value',[
    ('active_requests',1),('cleanup_tasks',1),('agent_requests',{'a':1}),('observation_errors',1)])
def test_unreleased_resource_or_missing_observation_still_blocks(field,value):
    evidence=report();evidence['after'][field]=value
    with pytest.raises(AssertionError):validate(evidence)


@pytest.mark.parametrize('mutation',['terminal','error_code','error_stage','cleanup','delivery','reserved','missing_bill'])
def test_corrupt_terminal_or_billing_evidence_still_blocks(mutation):
    value=report();observed=value['verified']['late']
    if mutation=='terminal':observed['status']='running'
    elif mutation=='error_code':observed['error_code']='different'
    elif mutation=='error_stage':observed['server_timing']['error_stage']='admission'
    elif mutation=='cleanup':observed['server_timing']['cleanup_error_code']='close_failed'
    elif mutation=='delivery':observed['server_timing']['delivery_error_code']='send_failed'
    elif mutation=='reserved':observed['bill_states']=['reserved']
    else:observed['bill_states']=[]
    with pytest.raises(AssertionError):validate(value)


def test_total_outage_cannot_be_reported_as_usable_capacity():
    value=report();value['records']=value['records'][1:];value['verified'].pop('ok')
    with pytest.raises(AssertionError):validate(value)
