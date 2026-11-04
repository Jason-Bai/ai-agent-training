"""Two request limits are explicit, strict startup configuration."""
from copy import deepcopy

import pytest
import yaml

from bug002_cases import write_registry
from services.models import ConfigurationError, load_registry


def test_yaml_requires_concurrency(tmp_path):
    path = write_registry(tmp_path)
    data = yaml.safe_load(path.read_text())
    data.pop('concurrency', None)
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigurationError):
        load_registry(tmp_path)


@pytest.mark.parametrize('limits', [
    {'max_requests': 20, 'max_requests_per_agent': 5},
    {'max_requests': 2, 'max_requests_per_agent': 2},
])
def test_valid_limits_do_not_change_providers(tmp_path, limits):
    path = write_registry(tmp_path)
    data = yaml.safe_load(path.read_text())
    providers = deepcopy(data['providers'])
    data['concurrency'] = limits
    path.write_text(yaml.safe_dump(data))
    loaded = load_registry(tmp_path)
    assert loaded.concurrency.model_dump() == limits
    assert {key:value.model_dump(exclude_none=True) for key,value in loaded.providers.items()} == providers


@pytest.mark.parametrize('field', ['max_requests', 'max_requests_per_agent'])
@pytest.mark.parametrize('value', [0, -1, True, '2', 1.5, None])
def test_invalid_limit_types_and_values_fail(tmp_path, field, value):
    path = write_registry(tmp_path)
    data = yaml.safe_load(path.read_text())
    data['concurrency'] = {'max_requests':20, 'max_requests_per_agent':5, field:value}
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigurationError):
        load_registry(tmp_path)


@pytest.mark.parametrize('limits', [None, [], {}, {'max_requests':2},
    {'max_requests_per_agent':1}, {'max_requests':1,'max_requests_per_agent':2},
    {'max_requests':2,'max_requests_per_agent':1,'PRIVATE_UNKNOWN':1}])
def test_incomplete_or_unknown_configuration_fails(tmp_path, limits):
    path = write_registry(tmp_path)
    data = yaml.safe_load(path.read_text())
    data['concurrency'] = limits
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigurationError) as exc:
        load_registry(tmp_path)
    assert 'PRIVATE_UNKNOWN' not in str(exc.value)


@pytest.mark.parametrize('duplicate', ['block', 'limit'])
def test_duplicate_concurrency_limit_is_rejected(tmp_path, duplicate):
    path = write_registry(tmp_path)
    data = yaml.safe_load(path.read_text())
    if duplicate == 'limit':
        data.pop('concurrency')
    path.write_text(yaml.safe_dump(data))
    with path.open('a') as stream:
        stream.write('\nconcurrency: {max_requests: 2, max_requests: 3, max_requests_per_agent: 1}\n')
    with pytest.raises(ConfigurationError):
        load_registry(tmp_path)
