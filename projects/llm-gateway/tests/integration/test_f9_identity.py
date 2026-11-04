from uuid import uuid4

import httpx
import pytest

from f6_cases import case, server
from f7_cases import application, body
from providers.mock import MockProvider


async def test_missing_credential_never_starts_generation(case):
    _, config = case
    mock = MockProvider()
    async with application(config, mock) as (_, client):
        assert (await client.get('/healthz')).status_code == 200
        for headers in ({}, {'authorization': 'Basic invalid'}, [('authorization', 'Bearer x'), ('authorization', 'Bearer y')]):
            reply = await client.post('/v1/llm', headers=headers, json=body())
            assert reply.status_code == 401
            assert reply.json()['code'] == 'unauthorized'
            assert reply.headers['x-request-id']
        assert not mock.calls


async def test_auth_precedes_http_payload_validation(case):
    from test_f5o2_http import make_app
    app = make_app(case[1])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/v1/llm', json={})
        assert response.status_code == 401


async def test_rotation_revocation_durable_ownership(case, server, caplog):
    from api.identity import create_agent, issue_credential, rotate_credential, revoke_credential
    name, config = case
    provider = MockProvider()
    async with application(config, provider) as (app, client):
        database = app.state.storage.database
        async with database.transaction() as session:
            await create_agent(session, 'a', 'agent A')
            await create_agent(session, 'b', 'agent B')
            old = await issue_credential(session, 'a')
            other = await issue_credential(session, 'b')
        created = await client.post('/v1/llm/stream', headers={'Authorization': 'Bearer '+old}, json=body())
        assert created.status_code == 202
        rid = created.json()['data']['request_id']
        async with database.transaction() as session:
            new = await rotate_credential(session, 'a', old.split('.')[0])
        for verb, path in [('get', f'/v1/traces/{rid}'), ('get', f'/v1/billing/requests/{rid}'),
                           ('get', f'/v1/llm/stream/{rid}/events'), ('post', f'/v1/llm/stream/{rid}/cancel')]:
            resp = await getattr(client,verb)(path, headers={'Authorization': 'Bearer '+other})
            assert (resp.status_code, resp.json()['code']) == (404,'not_found')
        assert not provider.stream_calls
        assert (await client.get(f'/v1/traces/{rid}', headers={'Authorization':'Bearer '+old})).status_code == 401
    async with application(config, provider) as (app, client):
        result = await client.get(f'/v1/traces/{rid}', headers={'Authorization':'Bearer '+new})
        assert result.status_code == 200
        assert result.json()['data']['status'] == 'pending'
        with server.connect(name) as connection:
            rows = connection.execute('select * from tb_agent_credential').fetchall()
            owner, = connection.execute('select agent_id from tb_llm_request where request_id=%s', (rid,)).fetchone()
        assert owner == 'a'
        for token in [old, other, new]:
            assert token.split('.')[1] not in str(rows)+result.text+caplog.text
        async with app.state.storage.database.transaction() as session:
            await revoke_credential(session, 'a', new.split('.')[0])
        assert (await client.get(f'/v1/traces/{rid}', headers={'Authorization':'Bearer '+new})).status_code == 401


@pytest.mark.parametrize('verb,suffix,encoded_tail', [
    ('get', 'events', '%0A'), ('post', 'cancel', '%0A'),
    ('get', 'events', ''), ('post', 'cancel', ''),
])
async def test_cross_agent_matched_stream_route_has_no_side_effects(case, server, verb, suffix, encoded_tail):
    from api.identity import create_agent, issue_credential
    from f6_cases import observe

    name, config = case
    provider = MockProvider()
    async with application(config, provider) as (app, client):
        async with app.state.storage.database.transaction() as session:
            await create_agent(session, 'owner', 'Owner')
            await create_agent(session, 'intruder', 'Intruder')
            owner = {'Authorization': 'Bearer ' + await issue_credential(session, 'owner')}
            intruder = {'Authorization': 'Bearer ' + await issue_credential(session, 'intruder')}
        created = await client.post('/v1/llm/stream', headers=owner, json=body())
        rid = created.json()['data']['request_id']
        before = observe(server, name, rid)
        response = await client.request(verb, f'/v1/llm/stream/{rid}/{suffix}{encoded_tail}', headers=intruder)
        assert response.status_code == 404, response.text
        assert response.json()['data'] is None
        assert response.json()['code'] == 'not_found'
        assert not provider.stream_calls
        assert observe(server, name, rid) == before
        with server.connect(name) as conn:
            assert conn.execute('SELECT count(*) FROM tb_billing_attempt').fetchone() == (0,)
            assert conn.execute('SELECT count(*) FROM tb_billing_operation').fetchone() == (0,)
