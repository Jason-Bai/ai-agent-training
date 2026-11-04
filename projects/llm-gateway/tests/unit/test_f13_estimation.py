"""A06: local Qwen estimates retain complete inputs and explicit provenance."""
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from contracts import Message
from services.token_counters import build_counters
from services.token_counters.base import TokenCount


def model(name='qwen3.8-flash', **changes):
    return SimpleNamespace(model_id=name, thinking_mode='disabled',
                           token_counter='qwen_text_estimate_v1', **changes)


def counter():
    return build_counters([model()])['qwen_text_estimate_v1']


def test_qwen_strategy_is_registered_and_explicitly_estimated():
    result=counter()
    assert result.kind=='estimated'
    assert result.supports_generation_constraints is True
    assert result.version=='qwen_text_estimate_v1:utf8-json:fixed512:percent10'


def test_legacy_token_count_positional_construction_defaults_to_exact():
    assert TokenCount(24,'existing-counter').kind=='exact'
    assert TokenCount(24,'new-counter',kind='estimated').kind=='estimated'


@pytest.mark.parametrize('name',['qwen3.8-flash','qwen3.7-plus','qwen3.8-max'])
def test_three_selected_models_share_provider_independent_strategy(name):
    result=counter()
    assert result.supports(model(name))
    assert result.supports(SimpleNamespace(model_id=name,thinking_mode='disabled',
                                         provider='a-different-provider'))


@pytest.mark.parametrize('name,mode',[
    ('deepseek-flash','disabled'),('qwen3.8-max-unknown','disabled'),
    ('qwen3.8-flash','enabled'),('qwen3.7-plus','enabled'),('qwen3.8-max','enabled')])
def test_unverified_models_and_thinking_modes_are_not_claimed_supported(name,mode):
    assert not counter().supports(SimpleNamespace(model_id=name,thinking_mode=mode))


def test_utf8_json_representation_and_margin_have_independent_small_golden():
    # A declared counting representation, not a claim to reproduce Qwen's chat template.
    # 45 ASCII bytes, plus 512 fixed tokens, then 10%, rounded up = 613.
    assert counter().count([Message(role='user',content='Hi')])==613


@pytest.mark.parametrize('text',['订单A123','Hello','中文 English A123','x = 3\nprint(x)',
                                 '🧑🏽‍💻 café e\u0301','a\x00b','x'*20000])
def test_unicode_and_long_content_are_retained_and_estimates_are_positive_integers(text):
    result=counter();messages=[Message(role='system',content='fixed rules'),
                               Message(role='user',content=text)]
    encoded=result.encode(messages)
    assert json.loads(encoded)['messages'][-1]['content']==text
    assert type(result.count(messages)) is int and result.count(messages)>512
    assert result.count(messages)>result.count([messages[0]])


def test_utf8_bytes_are_used_instead_of_python_character_length():
    result=counter()
    assert result.count([Message(role='user',content='中')])-result.count(
        [Message(role='user',content='a')])==2


def test_system_few_shots_and_role_boundaries_are_all_counted_without_mutation():
    result=counter()
    messages=[Message(role='system',content='固定规则'),Message(role='user',content='例子问题'),
              Message(role='assistant',content='例子回答'),Message(role='user',content='真实问题')]
    before=[m.model_dump() for m in messages]
    assert result.count(messages)>result.count([messages[-1]])
    assert result.count(messages)>result.count([messages[0],messages[-1]])
    encoded=result.encode(messages)
    assert all(m.content in encoded for m in messages)
    assert '"role":"assistant"' in encoded
    assert [m.model_dump() for m in messages]==before


def test_full_schema_and_exact_decimals_are_counted_as_generation_constraints():
    result=counter();messages=[Message(role='system',content='rules'),Message(role='user',content='A123')]
    schema={'type':'object','properties':{'number':{'type':'number','minimum':Decimal('1.0000000000000001')}},
            'required':['number'],'additionalProperties':False}
    encoded=result.encode(messages,generation_constraint=schema)
    assert '1.0000000000000001' in encoded and 'additionalProperties' in encoded
    assert result.count(messages,generation_constraint=schema)>result.count(messages)
    assert result.count(messages,generation_constraint={'type':'json_object'})>result.count(messages)
    assert result.count(messages,generation_constraint=schema)==result.count(
        messages,generation_constraint=dict(reversed(list(schema.items()))))


def test_only_requested_estimator_loads_once_without_deepseek_resources(monkeypatch):
    from services import token_counters
    def forbidden():
        pytest.fail('Qwen estimation must not load DeepSeek resources')
    monkeypatch.setitem(token_counters.COUNTER_FACTORIES,'deepseek_v41',forbidden)
    models=[model(name) for name in ('qwen3.8-flash','qwen3.7-plus','qwen3.8-max')]
    result=build_counters(models)
    assert list(result)==['qwen_text_estimate_v1']


def test_version_includes_the_effective_margin_settings():
    result=counter()
    assert f'fixed{result.fixed_overhead_tokens}' in result.version
    assert f'percent{result.safety_percent}' in result.version
    previous=result.version
    result.fixed_overhead_tokens+=1
    assert result.version!=previous
