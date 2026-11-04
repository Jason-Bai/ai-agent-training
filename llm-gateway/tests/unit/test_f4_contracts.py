from registry_helpers import ModelRegistry
from services.models import EndpointConfig as ModelConfig
from services.models import ConcurrencyPolicy
from copy import deepcopy

import pytest
from pydantic import ValidationError

from contracts import LLMRequest
from services.models import ProviderConfig
from f3_cases import MODEL_VALUES as HISTORICAL_MODEL_VALUES

MODEL_VALUES = {key: value for key, value in HISTORICAL_MODEL_VALUES.items()
                if key not in {"default_output_tokens", "default_timeout_seconds"}} | {"enabled": True}


def test_schema_object_is_an_optional_request_field(payload):
    assert LLMRequest.model_validate(payload).model_dump().get('response_schema') is None
    payload['response_schema'] = {'type': 'object'}
    request = LLMRequest.model_validate(payload)
    assert request.response_schema == {'type': 'object'}


@pytest.mark.parametrize('value', [None, True, False, [], 'schema', 1])
def test_schema_field_does_not_accept_non_objects(payload, value):
    payload['response_schema'] = value
    with pytest.raises(ValidationError):
        LLMRequest.model_validate(payload)


def test_protocol_and_output_mode_must_be_explicit():
    with pytest.raises(ValidationError):
        ProviderConfig(adapter='mock')
    values = deepcopy(MODEL_VALUES)
    values.pop('structured_output_mode', None)
    with pytest.raises(ValidationError):
        ModelConfig(**values)


@pytest.mark.parametrize('adapter,protocol,mode', [
    ('mock', 'chat_completions', 'none'), ('mock', 'chat_completions', 'json_object'),
    ('mock', 'responses', 'none'), ('mock', 'responses', 'json_schema'),
    ('deepseek', 'chat_completions', 'none'), ('deepseek', 'chat_completions', 'json_object'),
    ('openai_compatible', 'responses', 'none'), ('openai_compatible', 'responses', 'json_schema'),
])
def test_supported_configuration_matrix(adapter, protocol, mode):
    connection = {} if adapter == 'mock' else dict(base_url='https://example.invalid', credential_ref='fixture')
    provider = ProviderConfig(adapter=adapter, api_protocol=protocol, **connection)
    model = ModelConfig(**dict(MODEL_VALUES, structured_output_mode=mode))
    registry = ModelRegistry({'model': model}, {'mock-main': provider}, concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))
    assert registry.endpoints['model'].structured_output_mode == mode


@pytest.mark.parametrize('adapter,protocol,mode', [
    ('deepseek', 'responses', 'json_schema'),
    ('openai_compatible', 'chat_completions', 'json_object'),
    ('mock', 'chat_completions', 'json_schema'),
    ('mock', 'responses', 'json_object'),
])
def test_unsupported_combinations_fail_during_configuration(adapter, protocol, mode):
    connection = {} if adapter == 'mock' else dict(base_url='https://example.invalid', credential_ref='fixture')
    with pytest.raises(ValueError):
        provider = ProviderConfig(adapter=adapter, api_protocol=protocol, **connection)
        model = ModelConfig(**dict(MODEL_VALUES, structured_output_mode=mode))
        ModelRegistry({'model': model}, {'mock-main': provider}, concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))
