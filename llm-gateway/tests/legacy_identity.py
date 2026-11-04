"""Credentials belong only to disposable PostgreSQL and Gateway HTTP clients in historical tests."""
from hashlib import sha256
from pathlib import Path
from tempfile import gettempdir
from uuid import uuid4
import secrets

import psycopg
import yaml


PUBLIC = uuid4().hex
SECRET = secrets.token_urlsafe(32)
HEADERS = {'Authorization': f'Bearer {PUBLIC}.{SECRET}'}
ASGI_AUTH = (b'authorization', HEADERS['Authorization'].encode())


def provision(directory):
    config = yaml.safe_load((Path(directory) / 'gateway.yaml').read_text())['database']
    password = Path(config['password_file']).resolve()
    assert config['host'] == '127.0.0.1' and config['username'] == 'gateway_test'
    assert config['name'].startswith('case_') and password.name == 'postgres.password'
    assert password.parent.parent == Path(gettempdir()).resolve()
    assert password.parent.name.startswith('llm-gateway-pg-test-')
    with psycopg.connect(host=config['host'], port=config['port'], dbname=config['name'],
                         user=config['username'], password=password.read_text()) as db:
        db.execute("INSERT INTO tb_agent VALUES ('legacy-test','integration fixture') ON CONFLICT DO NOTHING")
        db.execute("INSERT INTO tb_agent_credential(public_id,agent_id,digest) VALUES (%s,'legacy-test',%s) ON CONFLICT DO NOTHING",
                   (PUBLIC, sha256(SECRET.encode()).hexdigest()))
        db.execute("""INSERT INTO tb_billing_operation(operation_id,kind,agent_id,amount,reason,details)
                      VALUES ('initial-test-funds','deposit','legacy-test',10000,'test fixture','{}') ON CONFLICT DO NOTHING""")
