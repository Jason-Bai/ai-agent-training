import builtins
from pathlib import Path

import pytest
from pydantic import ValidationError

import settings as config
from settings import Settings, load_settings


def test_missing_file_uses_defaults(tmp_path):
    loaded = load_settings(tmp_path / ".env")
    assert loaded.env == "development"


def test_env_file_is_loaded(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ENV=test\n")  # F3 .env contains runtime settings only.
    loaded = load_settings(env_file)
    assert loaded.env == "test"


@pytest.mark.parametrize("file_env,process_env", [("test", "production"), ("production", "test")])
def test_process_environment_overrides_file(tmp_path, monkeypatch, file_env, process_env):
    env_file = tmp_path / ".env"
    env_file.write_text(f"ENV={file_env}\n")
    monkeypatch.setenv("ENV", process_env)
    assert load_settings(env_file).env == process_env


@pytest.mark.parametrize("file_provider,process_provider", [
    ("not-supported", "mock"), ("mock", "not-supported"),
])
def test_invalid_provider_does_not_fall_back(tmp_path, monkeypatch, file_provider, process_provider):
    env_file = tmp_path / ".env"
    env_file.write_text(f"ENV=test\nLLM_GATEWAY_PROVIDER={file_provider}\n")
    monkeypatch.setenv("LLM_GATEWAY_PROVIDER", process_provider)
    # The removed global selector is invalid even when another source overrides it.
    with pytest.raises(ValueError, match="LLM_GATEWAY_PROVIDER"):
        load_settings(env_file)


@pytest.mark.parametrize("name,value", [
    ("ENV", "develop"), ("ENV", "TEST"), ("ENV", ""), ("ENV", "   "),
    ("LLM_GATEWAY_PROVIDER", "deepseek"), ("LLM_GATEWAY_PROVIDER", "openai_compatible"),
    ("LLM_GATEWAY_PROVIDER", ""), ("LLM_GATEWAY_PROVIDER", "   "),
])
def test_invalid_values_are_rejected(tmp_path, name, value):
    env_file = tmp_path / ".env"
    env_file.write_text(f'{name}="{value}"\n')
    with pytest.raises(ValidationError if name == "ENV" else ValueError, match=name):
        load_settings(env_file)


@pytest.mark.parametrize("content,expected", [
    ("ENV=test\n", "test"),
    ("", "development"),
])
def test_partial_file_uses_defaults_for_missing_fields(tmp_path, content, expected):
    env_file = tmp_path / ".env"
    env_file.write_text(content)
    loaded = load_settings(env_file)
    assert loaded.env == expected


def test_default_path_does_not_depend_on_cwd(tmp_path, monkeypatch):
    expected = Path(__file__).resolve().parents[2] / ".env"
    monkeypatch.chdir(tmp_path)
    assert config.DEFAULT_ENV_FILE == expected
    assert load_settings.__defaults__ == (expected,)
    for index, env in enumerate(("test", "production")):
        directory = tmp_path / str(index)
        directory.mkdir()
        env_file = directory / ".env"
        env_file.write_text(f"ENV={env}\n")
        assert load_settings(env_file).env == env


def test_only_selected_env_file_is_read(tmp_path, monkeypatch):
    for name in (".env.example", ".env.test"):
        (tmp_path / name).write_text("ENV=invalid\nLLM_GATEWAY_PROVIDER=invalid\n")
    original = builtins.open
    paths = []

    def record(path, *args, **kwargs):
        paths.append(Path(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", record)
    loaded = load_settings(tmp_path / ".env")
    assert loaded.env == "development"
    selected = tmp_path / ".env"
    selected.write_text("ENV=test\n")
    assert load_settings(selected).env == "test"
    assert selected in paths
    assert not any(path.name in {".env.example", ".env.test"} for path in paths)


def test_explicit_settings_ignore_invalid_external_values(monkeypatch):
    monkeypatch.setenv("ENV", "invalid")
    monkeypatch.setenv("LLM_GATEWAY_PROVIDER", "invalid")
    loaded = Settings(env="test", _env_file=None)
    assert loaded.env == "test"
