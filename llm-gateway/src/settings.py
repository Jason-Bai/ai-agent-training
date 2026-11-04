import os
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_ENV_FILE = Path(__file__).resolve().parents[1] / '.env'


class Settings(BaseSettings):
    model_config = SettingsConfigDict(populate_by_name=True, frozen=True, hide_input_in_errors=True)

    env: Literal['development', 'test', 'production'] = Field(default='development', validation_alias='ENV')


def load_settings(env_file: Path = DEFAULT_ENV_FILE) -> Settings:
    file_keys = dotenv_values(env_file).keys() if env_file.is_file() else ()
    if any(key.casefold() == 'llm_gateway_provider' for key in [*os.environ, *file_keys]):
        raise ValueError('LLM_GATEWAY_PROVIDER 已移至 gateway.yaml，请迁移旧配置')
    return Settings(_env_file=env_file)
