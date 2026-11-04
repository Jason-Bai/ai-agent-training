from copy import deepcopy
import json
from pathlib import Path
from decimal import Decimal

import pytest

from contracts import Message
from f4_cases import registry
from services.token_budget import TokenBudget
from services.token_counters.deepseek_v41 import DeepSeekV41Counter


GOLDENS=json.loads((Path(__file__).parents[1]/'fixtures/f4_token_goldens.json').read_text())


@pytest.mark.parametrize('case',GOLDENS['cases'],ids=lambda case:case['name'])
def test_native_constraints_match_independent_reference_goldens(case):
    counter=DeepSeekV41Counter()
    messages=[Message(**m) for m in case['messages']]
    original=deepcopy(messages)
    assert counter.encode(messages,generation_constraint=case['generation_constraint'])==case['encoded']
    assert counter.count(messages,generation_constraint=case['generation_constraint'])==case['count']
    assert messages==original
    assert counter.resource_revision==GOLDENS['resource_revision']
    assert counter.version!=counter.resource_revision


def test_budget_selects_strategy_and_native_count_without_double_counting():
    model=registry().get_endpoint('general-backup')
    budget=TokenBudget.from_models([model])
    case=GOLDENS['cases'][0]
    messages=[Message(**m) for m in case['messages']]
    assert budget.count(model,messages,response_schema=case['generation_constraint']).input_tokens==case['count']
    assert budget.count(model,messages).input_tokens < case['count']


def test_native_mode_cannot_use_a_text_only_counter():
    class TextOnly:
        version='text-only'
        def supports(self,model):return True
        def count(self,messages):return 1
    with pytest.raises(ValueError,match='计数'):
        TokenBudget({'deepseek_v41':TextOnly()}).validate_models(registry().endpoints.values())


def test_counter_preserves_decimal_literals_in_reference_format():
    messages=[Message(role='system',content='Fixed'),Message(role='user',content='JSON')]
    schema={'type':'object','properties':{'n':{'minimum':Decimal('1.0000000000000001')}}}
    encoded=DeepSeekV41Counter.encode(messages,generation_constraint=schema)
    expected=('<｜begin▁of▁sentence｜><｜System｜>Fixed\n\n## Response Format:\n\n'
              'You MUST strictly adhere to the following schema to reply:\n'
              '{"type": "object", "properties": {"n": {"minimum": 1.0000000000000001}}}'
              '<｜User｜>JSON<｜Assistant｜></think>')
    assert encoded==expected
