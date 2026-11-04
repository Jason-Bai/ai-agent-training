from dataclasses import FrozenInstanceError, replace

import pytest

from contracts import Message, PromptSelection, UserMessage
from prompt_cases import (
    CONTEXT_SYSTEM, CONTEXT_USER_TEMPLATE, CONTEXT, QUESTION, GOLDEN_HASHES,
    EXAMPLE_ONE_USER, EXAMPLE_ONE_ASSISTANT, EXAMPLE_TWO_USER, EXAMPLE_TWO_ASSISTANT,
    expected_context_messages,
)
from services.prompts import (
    DEFAULT_TEMPLATES, SYSTEM_PROMPT, FewShot, PromptRegistry, PromptTemplate,
    VariableSpec, build_messages, fingerprint_messages,
)


def fixture_template(**changes):
    template = PromptTemplate(
        name="fixture_case", version="1.0.0", system_template="固定规则",
        user_template="${message}\n${context}", change_note="测试资产",
        variable_specs={"context": VariableSpec(10_000)},
    )
    return replace(template, **changes)


def assemble(template, message="问题", **values):
    return build_messages(
        PromptSelection(name=template.name, version=template.version, variables=values),
        [UserMessage(role="user", content=message)], PromptRegistry([template]),
    )


def test_registry_rejects_duplicate_key():
    template = fixture_template()
    for duplicate in [template, replace(template, system_template="另一个正文")]:
        with pytest.raises(ValueError):
            PromptRegistry([template, duplicate])


def test_fingerprint_matches_independent_golden():
    messages = [Message(role="system", content=SYSTEM_PROMPT), Message(role="user", content="Explain TTFT")]
    assert fingerprint_messages(messages) == GOLDEN_HASHES["basic_text"]


@pytest.mark.parametrize("version", ["1.0.0", "1.1.0"])
def test_context_assets_and_messages_match_approved_goldens(version):
    registry = PromptRegistry(DEFAULT_TEMPLATES)
    template = registry.templates[("context_answer", version)]
    assert template.system_template == CONTEXT_SYSTEM
    assert template.user_template == CONTEXT_USER_TEMPLATE
    assert template.variable_specs == {"context": VariableSpec(10_000)}
    assert template.change_note == ("首版受控资料问答模板" if version == "1.0.0" else "增加资料充分与不足的边界示例，输入协议不变")
    expected_shots = () if version == "1.0.0" else (
        FewShot(EXAMPLE_ONE_USER, EXAMPLE_ONE_ASSISTANT), FewShot(EXAMPLE_TWO_USER, EXAMPLE_TWO_ASSISTANT),
    )
    assert template.few_shots == expected_shots
    messages = build_messages(
        PromptSelection(name="context_answer", version=version, variables={"context": CONTEXT}),
        [UserMessage(role="user", content=QUESTION)], registry,
    )
    assert [m.model_dump() for m in messages] == expected_context_messages(version)
    assert fingerprint_messages(messages) == GOLDEN_HASHES[version]


def test_basic_asset_and_default_registry_are_exact():
    registry = PromptRegistry(DEFAULT_TEMPLATES)
    assert set(registry.templates) == {("basic_text", "1.0.0"), ("context_answer", "1.0.0"), ("context_answer", "1.1.0"), ("context_answer", "1.2.0")}
    basic = registry.templates[("basic_text", "1.0.0")]
    assert basic.system_template == SYSTEM_PROMPT
    assert basic.user_template == "${message}"
    assert basic.variable_specs == {}
    assert basic.few_shots == ()
    assert basic.change_note == "保留 F1 单轮文本回答行为，使用受控模板对象装配"


@pytest.mark.parametrize("changes", [
    {"name": ""}, {"name": "   "}, {"version": "v1"}, {"version": "latest"},
    {"system_template": " "}, {"system_template": 1},
    {"user_template": ""}, {"user_template": None},
    {"change_note": " "}, {"change_note": False},
    {"variable_specs": {"message": VariableSpec(10)}},
    {"variable_specs": {"bad.name": VariableSpec(10)}},
    {"variable_specs": {"context": VariableSpec(0)}},
    {"variable_specs": {"context": VariableSpec(-1)}},
    {"variable_specs": {"context": VariableSpec("10")}},
    {"variable_specs": {"context": VariableSpec(True)}},
    {"variable_specs": {"context": 10}},
    {"user_template": "${context}"},
    {"user_template": "${message}"},
    {"user_template": "${message} ${context} ${undeclared}"},
    {"user_template": "${message} ${context} ${}"},
    {"user_template": "${message} ${context.attr}"},
    {"user_template": "${message} ${context[0]}"},
    {"user_template": "${message} ${context:10}"},
    {"user_template": "${message} ${context} $"},
    {"few_shots": (FewShot(" ", "answer"),)},
    {"few_shots": (FewShot("question", ""),)},
    {"few_shots": (FewShot(1, "answer"),)},
    {"few_shots": (FewShot("question", None),)},
    {"few_shots": ({"role": "system", "content": "rule"},)},
    {"few_shots": (("question", "answer"),)},
])
def test_invalid_template_definition_is_rejected(changes):
    with pytest.raises((ValueError, TypeError)):
        PromptRegistry([fixture_template(**changes)])


def test_simple_substitution_is_single_pass_and_static_text_stays_literal():
    template = fixture_template(
        system_template="固定 ${context}", user_template="$message\n${context}\n$$",
        few_shots=(FewShot("静态 ${message}", "静态 ${context}"),),
    )
    messages = assemble(template, message="  ${context}\n", context=' {"a": "${message}"} ')
    assert [(m.role, m.content) for m in messages] == [
        ("system", "固定 ${context}"), ("user", "静态 ${message}"), ("assistant", "静态 ${context}"),
        ("user", '  ${context}\n\n {"a": "${message}"} \n$'),
    ]


def test_registry_and_templates_cannot_be_modified_or_share_caller_containers():
    specs = {"context": VariableSpec(10)}
    shots = [FewShot("example", "answer")]
    template = fixture_template(variable_specs=specs, few_shots=shots)
    registry = PromptRegistry([template])
    specs["context"] = VariableSpec(1)
    shots.clear()
    assert template.variable_specs["context"].max_chars == 10
    assert template.few_shots == (FewShot("example", "answer"),)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        template.system_template = "changed"
    with pytest.raises(TypeError):
        template.variable_specs["context"] = VariableSpec(1)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        template.variable_specs["context"].max_chars = 1
    with pytest.raises((FrozenInstanceError, AttributeError)):
        template.few_shots[0].assistant = "changed"
    with pytest.raises(TypeError):
        registry.templates[("fixture_case", "1.0.0")] = fixture_template()
    with pytest.raises((FrozenInstanceError, AttributeError)):
        registry.templates = {}


@pytest.mark.parametrize("changes", [
    {"system_template": "新规则"},
    {"few_shots": (FewShot("example", "answer"),)},
    {"few_shots": (FewShot("second", "2"), FewShot("first", "1"))},
])
def test_visible_asset_changes_change_fingerprint(changes):
    original = fixture_template(few_shots=(FewShot("first", "1"), FewShot("second", "2")))
    assert fingerprint_messages(assemble(original, context="资料")) != fingerprint_messages(assemble(replace(original, **changes), context="资料"))


def test_runtime_content_changes_change_fingerprint():
    template = fixture_template()
    baseline = fingerprint_messages(assemble(template, message="问题 A", context="资料 A"))
    assert baseline != fingerprint_messages(assemble(template, message="问题 B", context="资料 A"))
    assert baseline != fingerprint_messages(assemble(template, message="问题 A", context="资料 B"))


def test_management_metadata_does_not_enter_message_fingerprint():
    original = fixture_template()
    renamed = replace(original, name="another_fixture", version="2.0.0", change_note="新的说明")
    assert fingerprint_messages(assemble(original, context="资料")) == fingerprint_messages(assemble(renamed, context="资料"))


def test_unicode_and_newlines_are_not_normalized():
    template = fixture_template()
    assert fingerprint_messages(assemble(template, context="é")) != fingerprint_messages(assemble(template, context="e\u0301"))
    assert fingerprint_messages(assemble(template, context="a\nb")) != fingerprint_messages(assemble(template, context="a\r\nb"))
