from copy import deepcopy

import pytest

from errors import ProviderCallError
from providers.base import parse_response, validate_result


RAW = {"model": "deepseek-flash", "choices": [{"message": {"role": "assistant", "content": "answer"}, "finish_reason": "stop"}],
       "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}}


@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens", "total_tokens"])
@pytest.mark.parametrize("damage", ["missing", True, "30", 1.5, -1])
def test_each_usage_component_is_required_and_strict_when_present(field, damage):
    raw = deepcopy(RAW)
    if damage == "missing":
        del raw["usage"][field]
    else:
        raw["usage"][field] = damage
    with pytest.raises(ProviderCallError) as exc:
        parse_response(raw)
    assert exc.value.code == "provider_invalid_response"


@pytest.mark.parametrize("usage", ["omitted", None, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                                   {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150,
                                    "prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 20}])
def test_unknown_zero_and_cached_usage_do_not_invent_or_duplicate_tokens(usage):
    raw = deepcopy(RAW)
    if usage == "omitted":
        del raw["usage"]
    else:
        raw["usage"] = usage
    result = parse_response(raw)
    validate_result(result)
    if usage is None or usage == "omitted":
        assert result.usage is None
    else:
        assert result.usage.input_tokens == usage["prompt_tokens"]
        assert result.usage.total_tokens == usage["total_tokens"]


@pytest.mark.parametrize("damage", ["two_choices", "no_model", "no_content", "no_reason", "blank_content", "length_bad_usage"])
def test_response_ambiguity_and_structural_damage_precede_completion_classification(damage):
    raw = deepcopy(RAW)
    if damage == "two_choices":
        raw["choices"] *= 2
    elif damage == "no_model":
        del raw["model"]
    elif damage == "no_content":
        del raw["choices"][0]["message"]["content"]
    elif damage == "no_reason":
        del raw["choices"][0]["finish_reason"]
    elif damage == "blank_content":
        raw["choices"][0]["message"]["content"] = " \n "
    else:
        raw["choices"][0]["finish_reason"] = "length"
        raw["usage"]["total_tokens"] = 151
    with pytest.raises(ProviderCallError) as exc:
        validate_result(parse_response(raw))
    assert exc.value.code == "provider_invalid_response"
