from services.models import EndpointConfig as ModelConfig
"""F5-C01/C02/C03/C04/C09/C10/E06/E07: strict stream boundaries."""
import pytest
from pydantic import ValidationError

import contracts
import providers.base as provider_types

from f3_cases import MODEL_VALUES as HISTORICAL_MODEL_VALUES

MODEL_VALUES = {key: value for key, value in HISTORICAL_MODEL_VALUES.items()
                if key not in {"default_output_tokens", "default_timeout_seconds"}} | {"enabled": True}


def stream_request():
    cls = getattr(contracts, 'StreamRequest', None)
    assert cls is not None, 'F5 requires its own stream request contract'
    return cls


def stream_event():
    cls = getattr(provider_types, 'ProviderStreamEvent', None)
    assert cls is not None, 'F5 requires normalized stream events'
    return cls


def test_stream_request_has_only_supported_fields(payload):
    cls = stream_request()
    assert set(cls.model_json_schema()['properties']) == {'model','prompt','messages','max_output_tokens','initial_output_tokens','timeout_seconds'}
    assert cls.model_validate(payload).model == 'general-primary'


@pytest.mark.parametrize('field,value', [('response_schema',None),('response_schema',{}),('stream',False),('stream',True),('api_protocol','responses'),('unknown',1)])
def test_extra_stream_fields_are_rejected(payload,field,value):
    payload[field]=value
    with pytest.raises(ValidationError):stream_request().model_validate(payload)


@pytest.mark.parametrize('field',['max_output_tokens','timeout_seconds'])
@pytest.mark.parametrize('value',[None,True,'1',0,-1,1.5])
def test_stream_budget_types(payload,field,value):
    payload[field]=value
    with pytest.raises(ValidationError):stream_request().model_validate(payload)


@pytest.mark.parametrize('value',[None,'true',0,1])
def test_model_streaming_capability_requires_strict_boolean(value):
    values=dict(MODEL_VALUES,supports_streaming=value)
    with pytest.raises(ValidationError):ModelConfig(**values)


def test_model_streaming_capability_is_required():
    values=dict(MODEL_VALUES);values.pop('supports_streaming',None)
    with pytest.raises(ValidationError):ModelConfig(**values)


@pytest.mark.parametrize('value',[True,False])
def test_model_streaming_capability_accepts_boolean(value):
    assert ModelConfig(**dict(MODEL_VALUES,supports_streaming=value)).supports_streaming is value


@pytest.mark.parametrize('data',[
 {'status':'running'},
 {'status':'completed','delta':'last','upstream_terminal_received':True},
 {'status':'failed','error_code':'provider_timeout'},
 {'status':'failed','error_code':'output_truncated','upstream_terminal_received':True,'usage':{'input_tokens':10,'output_tokens':5,'total_tokens':15}},
])
def test_valid_stream_events(data):
    assert stream_event().model_validate(data).status == data['status']


@pytest.mark.parametrize('data',[
 {'status':'unknown'}, {'delta':123}, {'status':'running','error_code':'provider_error'},
 {'status':'running','usage':{'input_tokens':0,'output_tokens':0,'total_tokens':0}},
 {'status':'running','upstream_terminal_received':True},
 {'status':'completed'}, {'status':'completed','upstream_terminal_received':True,'error_code':'provider_error'},
 {'status':'failed'}, {'status':'failed','error_code':'request_timeout'},
 {'status':'failed','error_code':'provider_error','upstream_terminal_received':1},
 {'actual_model':False}, {'actual_model':'  '},
 {'usage':{'input_tokens':True,'output_tokens':0,'total_tokens':1}},
])
def test_invalid_stream_events(data):
    with pytest.raises(ValidationError):stream_event().model_validate(data)


def test_declared_streaming_rejects_incomplete_injection():
    from f3_cases import registry
    from providers.factory import ProviderClients
    from services.models import ConfigurationError
    with pytest.raises(ConfigurationError, match='stream'):
        ProviderClients(registry(), provider=object())


def test_nonstreaming_model_allows_nonstreaming_injection():
    from f3_cases import registry
    from providers.factory import ProviderClients
    instance=object()
    clients=ProviderClients(registry({'general-primary':{'supports_streaming':False},'general-backup':{'supports_streaming':False}}), provider=instance)
    assert clients.instances['mock-main'] is instance


def test_capable_injection_needs_callable_stream():
    from f3_cases import registry
    from providers.factory import ProviderClients
    from services.models import ConfigurationError
    class NotCallable:
        stream=True
    with pytest.raises(ConfigurationError, match='stream'):
        ProviderClients(registry(),provider=NotCallable())


def test_real_binding_is_checked_before_credentials(monkeypatch):
    from f3_cases import registry
    import providers.factory as factory
    from services.models import ConfigurationError
    monkeypatch.setattr(factory,'MockProvider',type('Incomplete',(),{}))
    with pytest.raises(ConfigurationError, match='stream'):
        factory.ProviderClients(registry())


@pytest.mark.parametrize('field',['upstream_status','upstream_finish_reason'])
def test_upstream_diagnostic_classifications_are_controlled(field):
    with pytest.raises(ValidationError):
        stream_event().model_validate({field:'PRIVATE_RAW_PROVIDER_ERROR'})
