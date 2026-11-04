"""Offline boundaries must fail before touching a maintainer's private files."""
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

import pytest


@pytest.mark.parametrize('name', ['gateway.yaml', 'credentials.yaml', 'postgres.password'])
def test_offline_guard_blocks_maintainer_files_before_raw_io(monkeypatch, name):
    path = Path(__file__).resolve().parents[2] / 'config' / name
    opened = []

    def raw_io(*args, **kwargs):
        opened.append(args[0])
        raise RuntimeError('unexpected raw IO reached')

    with monkeypatch.context() as patch:
        patch.setattr(io, 'open', raw_io)
        with pytest.raises(AssertionError, match='offline tests must not read'):
            with path.open():
                pass
    assert opened == []


def test_offline_guard_allows_test_owned_yaml_and_fake_credentials(tmp_path):
    for name in ['gateway.yaml', 'credentials.yaml', 'postgres.password']:
        path = tmp_path / name
        path.write_text('test-owned fake data')
        assert path.read_text() == 'test-owned fake data'


@pytest.mark.parametrize('method', ['connect', 'connect_ex'])
def test_offline_guard_rejects_non_loopback_before_underlying_socket(method):
    # A closed descriptor cannot send a packet even if the guard regresses.
    sock = socket.socket()
    sock.close()
    with pytest.raises(AssertionError, match='offline tests must not connect'):
        getattr(sock, method)(('203.0.113.1', 443))


def test_offline_guard_allows_owned_loopback_connection():
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        server.listen()
        with socket.socket() as client:
            client.connect(server.getsockname())
            connection, _ = server.accept()
            connection.close()


@pytest.fixture
def isolated_test_project(tmp_path):
    root = Path(__file__).resolve().parents[2]
    project = tmp_path / 'project'
    for name in ['src', 'tests']:
        shutil.copytree(root / name, project / name,
                        ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
    (project / 'assets').symlink_to(root / 'assets', target_is_directory=True)
    shutil.copyfile(root / 'pyproject.toml', project / 'pyproject.toml')
    approved = project / 'docs/features/F1/design.md'
    approved.parent.mkdir(parents=True)
    shutil.copyfile(root / 'docs/features/F1/design.md', approved)
    # Observe accesses during collection as well as test execution. No secret
    # file is copied; an accidental access fails before the raw open operation.
    with (project / 'tests/conftest.py').open('a') as stream:
        stream.write('''
import atexit as _audit_exit
import io as _audit_io
from pathlib import Path as _AuditPath
_audit_root = _AuditPath(__file__).resolve().parents[1]
_audit_reads = []
_audit_open = _audit_io.open
_audit_private = {(_audit_root/'config'/name).resolve(): name for name in
                  ('gateway.yaml', 'credentials.yaml', 'postgres.password')}
def _observed_runtime_open(file, *args, **kwargs):
    if isinstance(file, (str, _AuditPath)):
        path = _AuditPath(file).resolve()
        if path in _audit_private:
            _audit_reads.append(_audit_private[path])
            if _audit_private[path] != 'gateway.yaml' or (_audit_root/'deny-runtime-yaml').exists():
                raise AssertionError('isolated probe attempted maintainer file IO')
    return _audit_open(file, *args, **kwargs)
_audit_io.open = _observed_runtime_open
def _save_audit():
    with _audit_open(_audit_root/'runtime-reads.json', 'w') as file:
        file.write(__import__('json').dumps(_audit_reads))
_audit_exit.register(_save_audit)
''')
    return project


def run_isolated_pytest(project, targets, record_property, *, live=False):
    args = [sys.executable, '-W', 'error', '-m', 'pytest', *targets,
            '-o', 'junit_family=xunit1', '--junitxml=' + str(project / 'receipt.xml'), '-q']
    if live:
        args.append('--run-live')
    result = subprocess.run(args, cwd=project, env={'PATH': os.defpath},
                            capture_output=True, text=True, timeout=90)
    output = result.stdout + result.stderr
    reads = json.loads((project / 'runtime-reads.json').read_text())
    record_property('isolated_configuration_receipt', json.dumps({
        'command': args, 'exit_code': result.returncode, 'output': output,
        'runtime_reads': reads, 'junit': (project / 'receipt.xml').read_text(),
    }))
    assert reads == [], 'test collection or execution read maintainer configuration'
    return result.returncode, output


@pytest.mark.parametrize('variant', ['mock', 'real-definition', 'denied', 'symlinked'])
def test_mock_and_protocol_cases_ignore_maintainer_model_binding(
        isolated_test_project, record_property, variant):
    from bug002_cases import write_registry
    project = isolated_test_project
    write_registry(project / 'config', real=variant == 'real-definition')
    if variant == 'symlinked':
        linked = project / 'runtime-model.yaml'
        (project / 'config/gateway.yaml').rename(linked)
        (project / 'config/gateway.yaml').symlink_to(linked)
    if variant == 'denied':
        (project / 'deny-runtime-yaml').touch()
    targets = ['tests/unit/test_f3_config.py', 'tests/unit/test_core.py',
               'tests/unit/test_bug002_registry.py',
               'tests/unit/test_bug002_isolation.py::test_offline_guard_blocks_maintainer_files_before_raw_io[gateway.yaml]',
               'tests/protocol/test_f3_prompt_wire.py', 'tests/protocol/test_providers.py',
               'tests/protocol/test_f8_wire_diagnostic_values.py']
    code, output = run_isolated_pytest(project, targets, record_property)
    assert code == 0, output


@pytest.mark.parametrize('live', [False, True], ids=['default-skips', 'explicit-missing-key-fails'])
def test_live_requires_explicit_selection_and_missing_key_fails(
        isolated_test_project, record_property, live):
    project = isolated_test_project
    probe = project / 'tests/live/test_bug002_probe.py'
    probe.write_text('''
from pathlib import Path
import pytest
from live.test_real_providers import live_server
pytestmark = pytest.mark.live
async def test_live_probe(live_server):
    Path('unexpected-provider-body').touch()
    raise AssertionError('missing credentials must stop before the test body')
''')
    code, output = run_isolated_pytest(project, [str(probe)], record_property, live=live)
    assert not (project / 'unexpected-provider-body').exists()
    if live:
        assert code == 1 and '真实接入前置条件未满足' in output, output
    else:
        assert code == 0 and '1 skipped' in output, output


def test_collection_audit_detects_symlink_read_before_fixtures(isolated_test_project, record_property):
    from bug002_cases import write_registry
    project = isolated_test_project
    path = write_registry(project / 'config')
    target = project / 'runtime-model.yaml'
    path.rename(target)
    path.symlink_to(target)
    probe = project / 'tests/test_collection_probe.py'
    probe.write_text('''
from pathlib import Path
Path('config/gateway.yaml').read_text()
def test_probe():
    pass
''')
    with pytest.raises(AssertionError, match='test collection or execution read maintainer configuration'):
        run_isolated_pytest(project, [str(probe)], record_property)
