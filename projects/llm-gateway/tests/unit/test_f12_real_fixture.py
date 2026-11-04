"""Real-provider fixture definitions are validated offline before any paid call."""
from pathlib import Path

from f4_cases import registry as mock_registry
from live.test_real_providers import real_registry
from services.models import EndpointConfig, ModelRegistry, RecoveryPolicy


def test_real_fixture_builds_complete_three_layer_registry():
    result=real_registry()
    baseline=mock_registry()
    assert isinstance(result,ModelRegistry)
    assert result.model_groups==baseline.model_groups
    assert set(result.endpoints)=={'general-primary','general-backup'}
    for name,provider,protocol in [('general-primary','deepseek-main','chat_completions'),
                                   ('general-backup','openai-main','responses')]:
        endpoint=result.get_endpoint(name)
        assert isinstance(endpoint,EndpointConfig)
        assert endpoint.provider==provider
        assert result.providers[provider].api_protocol==protocol
        assert endpoint.model_dump(exclude={'provider'})==baseline.get_endpoint(name).model_dump(exclude={'provider'})


def test_real_fixture_uses_default_recovery_instead_of_one_shot_mock_policy():
    result=real_registry()
    assert result.recovery==RecoveryPolicy()
    assert result.recovery.max_attempts==3
    assert result.recovery.max_attempts_per_endpoint==2
    assert mock_registry().recovery.max_attempts==1


def test_real_fixture_building_does_not_read_runtime_files_or_credentials(monkeypatch):
    original=Path.open
    def guarded(path,*args,**kwargs):
        if path.name in {'gateway.yaml','credentials.yaml','postgres.password'}:
            raise AssertionError('fixture construction must not read private runtime files')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',guarded)
    assert real_registry().providers['deepseek-main'].credential_ref=='deepseek-main-key'
