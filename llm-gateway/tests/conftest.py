from copy import deepcopy
import os

import pytest
from fastapi.testclient import TestClient

from main import create_app
from providers.mock import MockProvider
from settings import Settings


@pytest.fixture(autouse=True)
def isolate_configuration(monkeypatch):
    for key in list(os.environ):
        if key.casefold() in {"env", "llm_gateway_provider"}:
            monkeypatch.delenv(key)


@pytest.fixture
def app_settings():
    return Settings(env="test", _env_file=None)


@pytest.fixture
def payload():
    return {
        "model": "general-primary",
        "prompt": {"name": "basic_text", "version": "1.0.0"},
        "messages": [{"role": "user", "content": "Explain TTFT"}],
    }


@pytest.fixture
def provider():
    return MockProvider()


@pytest.fixture
def app(provider, app_settings):
    return create_app(settings=app_settings, provider=provider)


@pytest.fixture
def client(app):
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def changed(payload, path, value):
    result = deepcopy(payload)
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return result


def pytest_addoption(parser):
    parser.addoption("--run-live", action="store_true", default=False, help="Explicitly enable real Provider acceptance tests")


def pytest_configure(config):
    config.addinivalue_line("markers", "live: real Provider calls; explicit --run-live and local credentials required")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-live"):
        skip = pytest.mark.skip(reason="real Provider verification requires --run-live")
        for item in items:
            if item.get_closest_marker("live"):
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def isolate_real_provider_access(request, monkeypatch):
    """Ordinary tests may use loopback servers, never real credentials or networks."""
    from pathlib import Path
    import ipaddress
    import socket

    if request.node.get_closest_marker("live") and request.config.getoption("--run-live"):
        return
    config_dir = Path(__file__).resolve().parents[1] / 'config'
    private_paths = {(config_dir / name).resolve()
                     for name in ('gateway.yaml', 'credentials.yaml', 'postgres.password')}
    original_open = Path.open
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def open_file(path, *args, **kwargs):
        if path.resolve() in private_paths:
            raise AssertionError("offline tests must not read maintainer model configuration or credentials")
        return original_open(path, *args, **kwargs)

    def check_address(address):
        if isinstance(address, tuple):
            try:
                local = ipaddress.ip_address(address[0]).is_loopback
            except ValueError:
                local = address[0] == "localhost"
            if not local:
                raise AssertionError("offline tests must not connect to real Providers")

    def connect(sock, address):
        check_address(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check_address(address)
        return original_connect_ex(sock, address)

    monkeypatch.setattr(Path, "open", open_file)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


@pytest.fixture(scope='session')
def legacy_postgres_environment(tmp_path_factory):
    """A migrated template is cloned for each old app, not a runtime fallback."""
    from pathlib import Path
    import subprocess
    import sys
    from f4_cases import registry
    from postgres_support import PostgresServer, write_config
    server = PostgresServer().start()
    template = server.create_database()
    directory = write_config(tmp_path_factory.mktemp('pg-template'), server, template, registry())
    try:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, '-m', 'alembic', '-x', f'config_dir={directory}',
                                 'upgrade', 'head'], cwd=root, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        from legacy_identity import provision
        provision(directory)
        yield server, template
    finally:
        server.close()


@pytest.fixture(autouse=True)
def explicit_database_for_historical_tests(request, monkeypatch):
    """Keep existing fixtures independent from the maintainer's password/YAML."""
    if any(name in request.path.name for name in ('f5o1', 'f5o2', 'f6', 'f7', 'f8', 'f9', 'f10', 'f11', 'f12', 'f13', 'bug002_common')):
        yield
        return
    import main
    from pathlib import Path
    from f4_cases import registry
    from persistence.database import DatabaseConfig
    databases = []
    original_registry = main.load_registry
    original_settings = main.load_settings

    def models(config_dir=main.DEFAULT_CONFIG_DIR):
        if Path(config_dir).resolve() == main.DEFAULT_CONFIG_DIR.resolve():
            return registry()
        return original_registry(config_dir)

    def environment(env_file=None):
        if env_file is None:
            env_file = request.getfixturevalue('tmp_path') / 'isolated-settings' / '.env'
        return original_settings(env_file)

    def configuration(config_dir):
        server, template = request.getfixturevalue('legacy_postgres_environment')
        name = server.create_database(template=template)
        databases.append((server, name))
        return DatabaseConfig(**server.config(name))

    from services.prices import PriceBook
    original_prices = PriceBook.load

    def prices(cls, config_dir):
        live_enabled = request.node.get_closest_marker('live') and request.config.getoption('--run-live')
        if not live_enabled and Path(config_dir).resolve() == main.DEFAULT_CONFIG_DIR.resolve():
            isolated = request.getfixturevalue('tmp_path') / 'isolated-prices'
            isolated.mkdir(exist_ok=True)
            (isolated / 'gateway.yaml').write_text('pricing: {bindings: {}, versions: []}\n')
            return original_prices(isolated)
        return original_prices(config_dir)

    monkeypatch.setattr(PriceBook, 'load', classmethod(prices))
    monkeypatch.setattr(main, 'load_database_config', configuration)
    monkeypatch.setattr(main, 'load_registry', models)
    monkeypatch.setattr(main, 'load_settings', environment)
    yield
    for server, name in reversed(databases):
        server.drop_database(name)


@pytest.fixture(autouse=True)
def authenticated_historical_gateway_clients(request, monkeypatch):
    """Old HTTP scenarios now use a funded account; F9 tests set credentials explicitly."""
    if any(k in request.path.name for k in ('f9','f10','f11','f13','bug002_common')) or 'postgres' in request.path.parts:
        return
    import httpx
    from alembic import command
    from starlette.testclient import TestClient
    from legacy_identity import HEADERS, provision
    original_upgrade = command.upgrade
    original_build = httpx.AsyncClient.build_request
    original_test_build = TestClient.build_request

    def upgrade(config, revision, *args, **kwargs):
        result = original_upgrade(config, revision, *args, **kwargs)
        if revision == 'head' and 'config_dir' in config.attributes:
            provision(config.attributes['config_dir'])
        return result

    def build(client, *args, **kwargs):
        message = original_build(client, *args, **kwargs)
        if isinstance(client._transport, httpx.ASGITransport):
            if message.url.host in {'test', 'testserver'} and message.url.path.startswith('/v1/'):
                message.headers.setdefault('Authorization', HEADERS['Authorization'])
        return message

    monkeypatch.setattr(command, 'upgrade', upgrade)
    def test_build(client, *args, **kwargs):
        message = original_test_build(client, *args, **kwargs)
        if message.url.host == 'testserver' and message.url.path.startswith('/v1/'):
            message.headers.setdefault('Authorization', HEADERS['Authorization'])
        return message

    monkeypatch.setattr(TestClient, 'build_request', test_build)
    monkeypatch.setattr(httpx.AsyncClient, 'build_request', build)


@pytest.fixture(autouse=True)
def prices_for_historical_loopback_adapters(request, monkeypatch):
    if request.path.stem in {'test_f3_gateway_probe', 'test_f8_rejected_usage', 'test_f6_protocol', 'test_f7_protocol'}:
        from pathlib import Path
        from services.prices import PriceBook
        catalog = PriceBook.load(Path(__file__).parent / 'fixtures/wire_prices')
        monkeypatch.setattr(PriceBook, 'load', classmethod(lambda cls, directory: catalog))
