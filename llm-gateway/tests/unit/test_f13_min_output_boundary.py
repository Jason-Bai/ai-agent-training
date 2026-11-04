"""Supplemental minimum-output acceptance through the actual service call."""
import pytest

from f12_cases import request
from f13_cases import registry, service
from providers.base import ProviderResult


@pytest.mark.parametrize('output_budget',[16,17],ids=['at_minimum_16','above_minimum_17'])
async def test_ordinary_call_accepts_minimum_output_without_inflating_budget(output_budget):
    definition = registry({name+'-e':{'min_output_tokens':16}
                           for name in ('light','general','analysis')})
    gateway,trace,provider,traces = service(definition,ProviderResult(text='订单A123充电器'))
    result = await gateway.complete(request(model='general',max_output_tokens=output_budget),trace)
    assert result.text == '订单A123充电器'
    assert len(provider.calls) == 1 and len(traces.attempts) == 1
    model,_,options = provider.calls[0]
    assert model.min_output_tokens == 16
    assert options['max_output_tokens'] == output_budget
    assert trace.max_output_tokens == output_budget
    assert traces.attempts[0].max_output_tokens == output_budget
    assert traces.attempts[0].model == 'general-e'
    assert traces.attempts[0].purpose == 'generation' and traces.attempts[0].status == 'completed'
    assert gateway.limiter.snapshot() == {'active_requests':0,'agents':{}}
