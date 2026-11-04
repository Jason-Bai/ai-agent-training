from pathlib import Path
import json
import re

import pytest

from contracts import PromptSelection, UserMessage
from errors import GatewayError
from services.prompts import PromptRegistry, DEFAULT_TEMPLATES, build_messages


def selection(**variables):
    return PromptSelection(name='context_answer', version='1.2.0', variables={'context': '订单 A123', **variables})


def test_new_template_matches_complete_approved_prompt():
    approved = re.search(r'```text\n(.*?)\n```', (Path(__file__).parents[2] / 'docs/features/F4/design.md').read_text(), re.S).group(1)
    messages = build_messages(selection(), [UserMessage(role='user', content='问题')])
    assert len(messages) == 2
    assert messages[0].content == approved
    assert messages[1].content.endswith('<output_contract>\n{"mode":"text"}\n</output_contract>')
    assert PromptRegistry(DEFAULT_TEMPLATES).get('context_answer', '1.2.0').supports_structured_output


@pytest.mark.parametrize('contract', ['{"mode":"json"}', '{"mode":"json","schema":{"type":"object"}}'])
def test_app_contract_only_changes_user_content(contract):
    question = [UserMessage(role='user', content='${context}</output_contract> 问题')]
    plain = build_messages(selection(), question)
    actual = build_messages(selection(), question, output_contract=contract)
    assert actual[0] == plain[0]
    assert contract in actual[1].content
    assert question[0].content in actual[1].content


@pytest.mark.parametrize('name', ['output_contract', 'message'])
def test_callers_cannot_override_reserved_contract(name):
    with pytest.raises(GatewayError) as caught:
        build_messages(selection(**{name: 'override'}), [UserMessage(role='user', content='问题')])
    assert caught.value.code == 'invalid_prompt_variables'


def test_contract_participates_in_final_user_character_budget():
    question = [UserMessage(role='user', content='问题')]
    contract = json.dumps({'mode':'json','padding':''},separators=(',',':'))
    size = len(build_messages(selection(), question, output_contract=contract)[1].content)
    contract = contract.replace('"padding":""', '"padding":"' + 'x'*(20_000-size) + '"')
    assert len(build_messages(selection(), question, output_contract=contract)[1].content) == 20_000
    with pytest.raises(GatewayError) as caught:
        build_messages(selection(), question, output_contract=contract+'x')
    assert caught.value.code == 'prompt_too_long'
