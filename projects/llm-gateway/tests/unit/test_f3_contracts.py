import pytest
from pydantic import ValidationError

from contracts import LLMRequest, Usage


@pytest.mark.parametrize("field", ["max_output_tokens", "timeout_seconds"])
@pytest.mark.parametrize("value", [1, 16, 4096])
def test_positive_request_budgets_are_accepted(payload, field, value):
    payload[field] = value
    request = LLMRequest.model_validate(payload)
    assert getattr(request, field) == value


@pytest.mark.parametrize("field", ["max_output_tokens", "timeout_seconds"])
@pytest.mark.parametrize("value", [None, True, False, "16", 1.5, 0, -1, [], {}])
def test_explicit_request_budgets_must_be_positive_integers(payload, field, value):
    payload[field] = value
    with pytest.raises(ValidationError):
        LLMRequest.model_validate(payload)


def test_usage_total_must_match_actual_parts():
    with pytest.raises(ValidationError):
        Usage(input_tokens=120, output_tokens=30, total_tokens=151)


def test_omitted_request_budgets_are_resolved_by_model_not_schema(payload):
    request = LLMRequest.model_validate(payload)
    assert request.max_output_tokens is None
    assert request.timeout_seconds is None


def test_request_openapi_schema_does_not_advertise_null_budgets():
    schema = LLMRequest.model_json_schema()
    for name in ["max_output_tokens", "timeout_seconds"]:
        assert name in schema["properties"]
        assert schema["properties"][name]["type"] == "integer"
        assert name not in schema["required"]
