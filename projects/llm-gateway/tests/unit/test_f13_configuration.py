"""F13 A01/A03: real registry and local minimum-output enforcement."""
import pytest

from errors import GatewayError
from services.models import load_registry
from test_f12_config import document, load
from f12_cases import registry, request, service, FixedCounter
from services.token_budget import TokenBudget


def test_bailian_responses_registry_accepts_real_model_definition(tmp_path):
    definition=document()
    definition['providers']['mock-main']={'adapter':'bailian','api_protocol':'responses',
        'base_url':'https://dashscope.aliyuncs.com/compatible-mode/v1',
        'credential_ref':'bailian-key'}
    definition['endpoints']['chat'].update(model_id='qwen3.8-flash',
        token_counter='qwen_text_estimate_v1',structured_output_mode='json_schema',
        min_output_tokens=16)
    result=load(tmp_path,definition)
    assert result.get_endpoint('chat').min_output_tokens==16
    assert result.providers['mock-main'].adapter=='bailian'


@pytest.mark.parametrize('changes',[{'max_output_tokens':15},
    {'max_output_tokens':128,'initial_output_tokens':15}])
async def test_generation_below_endpoint_minimum_is_rejected_without_call(changes):
    definitions=registry({n:{'min_output_tokens':16} for n in ('chat-a','responses-b')})
    gateway,trace,a,b,traces=service(definitions)
    with pytest.raises(GatewayError) as caught:
        await gateway.complete(request(**changes),trace)
    assert caught.value.code=='invalid_output_budget'
    assert not a.calls and not b.calls and not traces.attempts


async def test_estimation_kind_is_preserved_in_request_and_attempt():
    counter=FixedCounter(100,'example-estimate-v1');counter.kind='estimated'
    gateway,trace,a,b,traces=service(budget=TokenBudget({'deepseek_v41':counter}))
    await gateway.complete(request(),trace)
    assert trace.input_token_kind=='estimated'
    assert traces.attempts[0].input_token_kind=='estimated'
