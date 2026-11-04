"""C01: adding database configuration must not break model registration."""
import yaml

from f4_cases import registry
from services.models import load_registry


def test_database_section_does_not_enter_model_registry(tmp_path):
    baseline = registry()
    document = {
        'providers': {name: value.model_dump(exclude_none=True) for name, value in baseline.providers.items()},
        'endpoints': {name: value.model_dump() for name, value in baseline.endpoints.items()},
        'model_groups': {key: value.model_dump(mode='json') for key, value in baseline.model_groups.items()},
        'concurrency': baseline.concurrency.model_dump(),
        'database': {
            'host': '127.0.0.1', 'port': 15432, 'name': 'gateway_test',
            'username': 'gateway_test', 'password_file': 'postgres.password',
        },
    }
    (tmp_path / 'gateway.yaml').write_text(yaml.safe_dump(document), encoding='utf-8')
    actual = load_registry(tmp_path)
    assert set(actual.endpoints) == set(baseline.endpoints)
    assert set(actual.providers) == set(baseline.providers)


def test_invalid_registry_root_explains_optional_database_section(tmp_path):
    document = {'providers': {}, 'endpoints': {}, 'databases': {}}
    (tmp_path/'gateway.yaml').write_text(yaml.safe_dump(document))
    with pytest.raises(ConfigurationError) as failure:
        load_registry(tmp_path)
    assert '可包含 database' in str(failure.value)


import pytest
from services.models import ConfigurationError
from persistence.database import load_database_config, database_url


VALID_DATABASE = {'host':'127.0.0.1','port':15432,'name':'gateway_test',
                  'username':'gateway_test','password_file':'postgres.password'}


@pytest.mark.parametrize('field', ['database','host','port','name','username','password_file'])
def test_required_database_fields(tmp_path, field):
    document = {'database': dict(VALID_DATABASE)}
    if field == 'database':
        document.clear()
    else:
        del document['database'][field]
    (tmp_path/'gateway.yaml').write_text(yaml.safe_dump(document))
    with pytest.raises(ConfigurationError):
        load_database_config(tmp_path)


@pytest.mark.parametrize('key,value', [('host',''),('host',' '),('port','15432'),('port',True),('port',0),('port',65536),('extra','value'),('password','synthetic-password')])
def test_strict_database_fields_safe_errors(tmp_path, key, value):
    document = {'database': {**VALID_DATABASE, key:value}}
    (tmp_path/'gateway.yaml').write_text(yaml.safe_dump(document))
    with pytest.raises(ConfigurationError) as failure:
        load_database_config(tmp_path)
    assert 'synthetic-password' not in str(failure.value)


@pytest.mark.parametrize('content', [None,'','  \n'])
def test_password_must_be_present_and_nonblank(tmp_path, content):
    (tmp_path/'gateway.yaml').write_text(yaml.safe_dump({'database':VALID_DATABASE}))
    if content is not None:
        (tmp_path/'postgres.password').write_text(content)
    with pytest.raises(ConfigurationError):
        database_url(load_database_config(tmp_path), tmp_path)


def test_password_path_independent_of_cwd_and_url_escaping(tmp_path, monkeypatch):
    (tmp_path/'gateway.yaml').write_text(yaml.safe_dump({'database':VALID_DATABASE}))
    password = 'a:@/?# %特殊字符'
    (tmp_path/'postgres.password').write_text(password+'\n')
    monkeypatch.chdir(tmp_path.parent)
    url = database_url(load_database_config(tmp_path), tmp_path)
    assert url.password == password
    assert password not in str(url)


def test_unreadable_password_is_safe_configuration_error(tmp_path, monkeypatch):
    from pathlib import Path
    (tmp_path/'gateway.yaml').write_text(yaml.safe_dump({'database': VALID_DATABASE}))
    password_file = tmp_path/'postgres.password'
    password_file.write_text('synthetic-private-password')
    original = Path.read_bytes

    def unreadable(path, *args, **kwargs):
        if path == password_file:
            raise PermissionError('synthetic-private-password')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_bytes', unreadable)
    with pytest.raises(ConfigurationError) as failure:
        database_url(load_database_config(tmp_path), tmp_path)
    assert 'synthetic-private-password' not in str(failure.value)
