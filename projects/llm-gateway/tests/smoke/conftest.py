from pathlib import Path
import shutil

import pytest


@pytest.fixture
def runtime_dir(tmp_path, legacy_postgres_environment):
    project = Path(__file__).resolve().parents[2]
    shutil.copytree(project / "src", tmp_path / "src", ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / 'config').mkdir()
    from f4_cases import registry
    from postgres_support import write_config
    server, template = legacy_postgres_environment
    name = server.create_database(template=template)
    write_config(tmp_path / 'config', server, name, registry())
    shutil.copytree(project / "assets", tmp_path / "assets", ignore=shutil.ignore_patterns("__pycache__"))
    content = (project / ".env.example").read_text().replace("ENV=development", "ENV=test")
    (tmp_path / ".env").write_text(content)
    (tmp_path / "unrelated-cwd").mkdir()
    try:
        yield tmp_path
    finally:
        server.drop_database(name)
