from services.models import ModelRegistry, EndpointConfig, ModelGroupConfig
from services.models import ConcurrencyPolicy
import pytest
import yaml

from f3_cases import MODEL_VALUES
from services.credentials import load_credentials
from services.models import ConfigurationError, load_registry


@pytest.mark.parametrize("models,providers", [
    ([], {"mock-main": {"adapter": "mock", "api_protocol": "chat_completions"}}), ([1], {"mock-main": {"adapter": "mock", "api_protocol": "chat_completions"}}),
    ({"x": MODEL_VALUES}, []), ({"x": MODEL_VALUES}, [1]),
    ({"x": None}, {"mock-main": {"adapter": "mock", "api_protocol": "chat_completions"}}),
    ({"x": MODEL_VALUES}, {"mock-main": None}),
    ({1: MODEL_VALUES}, {"mock-main": {"adapter": "mock", "api_protocol": "chat_completions"}}),
])
def test_registry_bad_containers_are_safe(models, providers):
    if isinstance(models,dict):
        models={k:({key:v for key,v in value.items() if key not in {'default_output_tokens','default_timeout_seconds'}} | {'enabled':True} if isinstance(value,dict) else value) for k,value in models.items()}
    groups={'x':dict(candidates=['x'],default_output_tokens=1024,max_output_tokens=4096,default_timeout_seconds=30,max_timeout_seconds=120)}
    with pytest.raises(ConfigurationError) as exc:
        ModelRegistry(endpoints=models, providers=providers, model_groups=groups, concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))
    assert "PRIVATE" not in str(exc.value)


@pytest.mark.parametrize("text", [
    "- PRIVATE_INPUT_MARKER", "null", "providers: {x: {adapter: mock, api_protocol: chat_completions}}\nmodels: {}\nunknown: PRIVATE_INPUT_MARKER",
    "providers: {x: {adapter: mock, api_protocol: chat_completions}}\nmodels: {x: {model_id: PRIVATE_INPUT_MARKER}}",
    "providers: {x: {adapter: mock, api_protocol: chat_completions}}\nmodels: {}\nmodels: {}",
])
def test_raw_yaml_wrong_shapes_and_duplicate_models_are_rejected(tmp_path, text):
    (tmp_path / "gateway.yaml").write_text(text)
    with pytest.raises(ConfigurationError) as exc:
        load_registry(tmp_path)
    assert "PRIVATE_INPUT_MARKER" not in str(exc.value)


@pytest.mark.parametrize("value", [None, [], "PRIVATE_INPUT_MARKER", {"x": {}}, {"x": {"api_key": True}},
                                    {"x": {"api_key": 1}}, {"x": {"api_key": "FAKE_CREDENTIAL_MARKER", "unknown": 1}}])
def test_credential_shapes_and_types_are_rejected_without_echo(tmp_path, value):
    path = tmp_path / "credentials.yaml"
    path.write_text(yaml.safe_dump({"credentials": value}))
    with pytest.raises(ConfigurationError) as exc:
        load_credentials(path, {"x"})
    assert "PRIVATE_INPUT_MARKER" not in str(exc.value)
    assert "FAKE_CREDENTIAL_MARKER" not in str(exc.value)


def test_duplicate_credential_key_is_rejected(tmp_path):
    path = tmp_path / "credentials.yaml"
    path.write_text("credentials:\n  x: {api_key: FAKE_CREDENTIAL_MARKER}\n  x: {api_key: other}\n")
    with pytest.raises(ConfigurationError) as exc:
        load_credentials(path, {"x"})
    assert "FAKE_CREDENTIAL_MARKER" not in str(exc.value)
