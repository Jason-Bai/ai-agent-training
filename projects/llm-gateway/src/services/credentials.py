from pathlib import Path

from pydantic import SecretStr

from services.models import ConfigurationError, read_yaml


def load_credentials(path: Path, refs: set[str]) -> dict[str, SecretStr]:
    if not refs:
        return {}
    data = read_yaml(path)
    try:
        if set(data) != {'credentials'} or not isinstance(data['credentials'], dict):
            raise ValueError
        values = data['credentials']
        if not values or not refs.issubset(values):
            raise ValueError
        result = {}
        for name, raw in values.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError
            # SecretStr protects representation; strict raw validation avoids coercion.
            if not isinstance(raw, dict) or set(raw) != {'api_key'}:
                raise ValueError
            value = raw['api_key']
            if not isinstance(value, str) or not value.strip():
                raise ValueError
            result[name] = SecretStr(value)
        return {name: result[name] for name in refs}
    except (ValueError, TypeError):
        raise ConfigurationError('credentials.yaml 配置或凭据引用不合法') from None
