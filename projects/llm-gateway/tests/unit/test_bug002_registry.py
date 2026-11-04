"""Preserve real loader/default-path coverage without maintainer YAML inputs."""
import importlib.util
from pathlib import Path
import shutil
import sys

import pytest
import yaml

from bug002_cases import write_registry
from services import models


@pytest.mark.parametrize('real', [False, True], ids=['mock', 'real-definition'])
def test_default_loader_uses_its_module_directory_and_gateway_filename(tmp_path, monkeypatch, real):
    source = tmp_path / 'project' / 'src' / 'services' / 'models.py'
    source.parent.mkdir(parents=True)
    shutil.copyfile(models.__file__, source)
    config_dir = tmp_path / 'project' / 'config'
    write_registry(config_dir, real=real)
    for env in ['test', 'production']:
        (config_dir / f'gateway.{env}.yaml').write_text('INVALID: [')
    outside = tmp_path / 'outside'
    outside.mkdir()
    monkeypatch.chdir(outside)
    spec = importlib.util.spec_from_file_location('_bug002_models', source)
    isolated = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, isolated)
    spec.loader.exec_module(isolated)
    assert isolated.DEFAULT_CONFIG_DIR == config_dir
    assert models.DEFAULT_CONFIG_DIR == Path(models.__file__).resolve().parents[2] / 'config'
    loaded = isolated.load_registry()
    assert set(loaded.endpoints) == {'general-primary', 'general-backup'}
    assert loaded.get_group('general-primary').default_output_tokens == 1024
    assert {p.adapter for p in loaded.providers.values()} == ({'deepseek', 'openai_compatible'} if real else {'mock'})
    (config_dir / 'gateway.yaml').unlink()
    with pytest.raises(ValueError, match='gateway.yaml'):
        isolated.load_registry()


@pytest.mark.parametrize('damage', ['unknown_provider', 'strict_type', 'budget'])
def test_yaml_model_validation_remains_real(tmp_path, damage):
    path = write_registry(tmp_path)
    data = yaml.safe_load(path.read_text())
    field, value = {
        'unknown_provider': ('provider', 'absent'),
        'strict_type': ('max_output_tokens', '4096'),
        'budget': ('default_output_tokens', 4097),
    }[damage]
    data['model_groups' if field=='default_output_tokens' else 'endpoints']['general-primary'][field] = value
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(models.ConfigurationError):
        models.load_registry(tmp_path)
