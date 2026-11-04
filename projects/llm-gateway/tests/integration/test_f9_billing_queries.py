from fastapi.testclient import TestClient

from test_f5o2_http import case, make_app, server


def test_billing_summary_is_an_authenticated_gateway_route(case, server):
    _, config = case
    with TestClient(make_app(config)) as client:
        response = client.get('/v1/billing/summary')
    assert response.status_code == 401
    assert response.json()['success'] is False
    assert response.json()['data'] is None


import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

from api.identity import create_agent, issue_credential
from f7_cases import application, body
from persistence import billing
from providers.mock import MockProvider


async def enroll(app, agent_id, amount='1'):
    async with app.state.storage.database.transaction() as s:
        await create_agent(s, agent_id, agent_id)
        token = await issue_credential(s, agent_id)
        if amount:
            await billing.deposit(s, agent_id, agent_id+'-funds', amount)
    return {'Authorization': 'Bearer ' + token}


async def test_owned_mock_usage_and_empty_history(case, server):
    _, config = case
    async with application(config, MockProvider()) as (app, client):
        owner = await enroll(app,'orders')
        other = await enroll(app,'another')
        response = await client.post('/v1/llm', json=body(), headers=owner)
        assert response.status_code == 200, response.text
        rid = response.headers['x-request-id']
        view = (await client.get('/v1/billing/summary', headers=owner)).json()['data']
        assert Decimal(view['available']) == 1 and Decimal(view['charged']) == 0
        assert view['usage']['mock']['known_subtotal'] == response.json()['data']['usage']
        assert view['usage']['real']['attempts'] == 0
        url = '/v1/billing/requests/' + rid
        detail = (await client.get(url,headers=owner)).json()['data']
        assert detail['attempts'][0]['usage_source'] == 'mock'
        assert Decimal(detail['attempts'][0]['agent_charge']) == 0
        assert (await client.get(url,headers=other)).status_code == 404
        assert (await client.get('/v1/billing/requests',headers=other)).json()['data']['items'] == []


async def test_cursor_pages_and_date_boundaries(server, case):
    _, config = case
    async with application(config, MockProvider()) as (app, client):
        headers = await enroll(app,'pages')
        stamp = datetime(2026,1,1,tzinfo=timezone.utc)
        ids = [uuid4() for _ in range(102)]
        sql = text("""INSERT INTO tb_llm_request
            (agent_id,request_id,operation,status,created_at) VALUES ('pages',:rid,'completion','failed',:at)""")
        async with app.state.storage.database.transaction() as s:
            await s.execute(sql,[{'rid':rid,'at':stamp if i<101 else stamp.replace(day=2)} for i,rid in enumerate(ids)])
        params = {'from':stamp.isoformat(),'to':stamp.replace(day=2).isoformat()}
        url = '/v1/billing/requests'
        page = (await client.get(url,params=params,headers=headers)).json()['data']
        assert len(page['items']) == 50
        found = [r['request_id'] for r in page['items']]
        while page['next_cursor']:
            page = (await client.get(url,params=dict(params,cursor=page['next_cursor'],limit=100),
                                     headers=headers)).json()['data']
            found.extend(row['request_id'] for row in page['items'])
        assert found == [str(i) for i in reversed(ids[:101])]
        for invalid in ({'limit':0},{'limit':101},{'cursor':'bad'},{'to':'2026-01-01'}):
            assert (await client.get(url,params=invalid,headers=headers)).status_code == 422


def test_admin_issues_credential_and_replays_deposit(server, case):
    _, config = case
    root = Path(__file__).resolve().parents[2]
    def cli(*args):
        result = subprocess.run([sys.executable,'-m','admin','--config-dir',str(config),*args],cwd=root,
                                env=dict(os.environ,PYTHONPATH=str(root/'src')),capture_output=True,text=True)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()
    cli('create-agent','orders','售后')
    token = cli('issue','orders')
    for _ in range(2):
        cli('deposit','orders','1.25','--operation-id','receipt-1')
    with TestClient(make_app(config)) as client:
        summary = client.get('/v1/billing/summary',headers={'Authorization':'Bearer '+token})
        assert summary.status_code == 200
        assert Decimal(summary.json()['data']['available']) == Decimal('1.25')
