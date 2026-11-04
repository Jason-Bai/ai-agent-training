"""Test-owned YAML inputs for configuration isolation; never read runtime config."""
import yaml

from f3_cases import registry


def write_registry(config_dir, *, real=False):
    models = registry()
    data = {
        'providers': {name: provider.model_dump(exclude_none=True) for name, provider in models.providers.items()},
        'endpoints': {name: model.model_dump(mode='json') for name, model in models.endpoints.items()},
        'model_groups': {key: value.model_dump(mode='json') for key, value in models.model_groups.items()},
        'concurrency': models.concurrency.model_dump(),
        'recovery': models.recovery.model_dump(),
    }
    if real:
        data['providers'] = {
            'mock-main': {'adapter':'deepseek', 'api_protocol':'chat_completions',
                          'base_url':'https://provider.invalid', 'credential_ref':'test-primary'},
            'mock-backup': {'adapter':'openai_compatible', 'api_protocol':'responses',
                            'base_url':'https://provider.invalid/v1', 'credential_ref':'test-backup'},
        }
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / 'gateway.yaml'
    path.write_text(yaml.safe_dump(data), encoding='utf-8')
    return path
