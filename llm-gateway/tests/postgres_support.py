"""Owned, disposable Compose PostgreSQL for tests; never uses developer credentials."""
import atexit
from contextlib import contextmanager
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import uuid

import psycopg
from psycopg import sql
import yaml


class PostgresServer:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix='llm-gateway-pg-test-'))
        self.project = 'llmgw-test-' + uuid.uuid4().hex[:12]
        self.username = 'gateway_test'
        self.password = secrets.token_urlsafe(24)
        self.password_path = self.root / 'postgres.password'
        self.password_path.write_text(self.password)
        self.password_path.chmod(0o600)
        self.compose_path = self.root / 'compose.yaml'
        # Keep restart fault tests out of the client's ephemeral port range.
        # Docker's dynamic binding releases that port while PostgreSQL is off.
        for _ in range(20):
            port = 20000 + secrets.randbelow(10000)
            with socket.socket() as listener:
                try:
                    listener.bind(('127.0.0.1', port))
                except OSError:
                    continue
                self.port = port
                break
        else:
            raise RuntimeError('No free isolated test PostgreSQL port')
        self.compose_path.write_text(yaml.safe_dump({
            'services': {'postgres': {
                'image': 'postgres:18.6-bookworm',
                'environment': {'POSTGRES_USER': self.username, 'POSTGRES_DB': 'gateway_test',
                                'POSTGRES_PASSWORD_FILE': '/run/secrets/postgres_password'},
                'secrets': ['postgres_password'],
                'ports': [f'127.0.0.1:{self.port}:5432'],
                'volumes': ['test_data:/var/lib/postgresql'],
                'healthcheck': {'test': ['CMD-SHELL', 'pg_isready -U gateway_test -d gateway_test'],
                                'interval': '1s', 'timeout': '3s', 'retries': 40},
            }},
            'secrets': {'postgres_password': {'file': str(self.password_path)}},
            'volumes': {'test_data': {}},
        }))
        self.started = False

    def compose(self, *args, timeout=120):
        return subprocess.run(
            ['docker', 'compose', '-p', self.project, '-f', str(self.compose_path), *args],
            capture_output=True, text=True, timeout=timeout,
        )

    def start(self):
        result = self.compose('up', '-d', '--wait', '--wait-timeout', '60')
        if result.returncode:
            self.close()
            raise RuntimeError('isolated test PostgreSQL did not become ready: ' + result.stderr[-1500:])
        address = self.compose('port', 'postgres', '5432').stdout.strip()
        if not address.startswith('127.0.0.1:'):
            self.close()
            raise RuntimeError('test database is not bound to loopback')
        self.port = int(address.rsplit(':', 1)[1])
        self.started = True
        atexit.register(self.close)
        return self

    def connect(self, name='gateway_test', **kwargs):
        return psycopg.connect(host='127.0.0.1', port=self.port, user=self.username,
                               password=self.password, dbname=name, connect_timeout=5, **kwargs)

    def create_database(self, *, template=None):
        name = 'case_' + uuid.uuid4().hex
        with self.connect(autocommit=True) as conn:
            statement = sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name))
            if template:
                statement += sql.SQL(' TEMPLATE {}').format(sql.Identifier(template))
            conn.execute(statement)
        return name

    def drop_database(self, name):
        if not name.startswith('case_'):
            raise ValueError('refusing to drop a non-test database')
        with self.connect(autocommit=True) as conn:
            conn.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)))

    def config(self, name='gateway_test'):
        return {'host': '127.0.0.1', 'port': self.port, 'name': name,
                'username': self.username, 'password_file': str(self.password_path)}

    def close(self):
        # Removal applies only to this randomly named, test-owned Compose project.
        if self.project.startswith('llmgw-test-') and self.compose_path.is_file():
            self.compose('down', '-v', '--remove-orphans', timeout=60)
            self.started = False


def write_config(directory, server, name, registry):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    document = {
        'providers': {key: value.model_dump(exclude_none=True) for key, value in registry.providers.items()},
        'endpoints': {key: value.model_dump() for key, value in registry.endpoints.items()},
        'database': server.config(name),
        'model_groups': {key: value.model_dump(mode='json') for key, value in registry.model_groups.items()},
        'concurrency': registry.concurrency.model_dump(),
        'recovery': registry.recovery.model_dump(),
    }
    (directory / 'gateway.yaml').write_text(yaml.safe_dump(document), encoding='utf-8')
    return directory


def migrate(config_dir):
    from alembic import command
    from alembic.config import Config
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / 'alembic.ini'))
    config.set_main_option('script_location', str(root / 'migrations'))
    config.attributes['config_dir'] = Path(config_dir)
    command.upgrade(config, 'head')


@contextmanager
def migrated_database(server, directory, registry):
    name = server.create_database()
    try:
        config_dir = write_config(directory, server, name, registry)
        migrate(config_dir)
        yield name, config_dir
    finally:
        server.drop_database(name)
