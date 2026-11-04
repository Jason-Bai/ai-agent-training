"""Cross-check the diagnostic GET against the writer, independent SQL and call counts."""
import asyncio

import pytest

from contracts import Usage
from errors import ProviderCallError
from f6_cases import ControlledProvider, case, server, assert_events, create, observe, poll
from f7_cases import SequenceProvider, application, body, registry
from providers.base import ProviderResult, ProviderStreamEvent
from services.models import RecoveryPolicy


async def diagnosis(client, rid):
    response = await client.get(f'/v1/traces/{rid}')
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert set(envelope) == {'success', 'code', 'message', 'data'}
    assert envelope['success'] and envelope['code'] == 'ok'
    assert envelope['data']['request_id'] == rid
    return envelope['data']


@pytest.mark.parametrize('close_error,terminal', [(False, 'canceled'), (True, 'failed')])
async def test_f8_query_during_cancel_and_after_cleanup(server, case, close_error, terminal):
    name, config = case
    provider = ControlledProvider(first=True, close_error=close_error)
    provider.close_release.clear()
    async with application(config, provider) as (_, client):
        rid = await create(client)
        assert (await diagnosis(client, rid))['status'] == 'pending'
        assert not provider.stream_calls
        subscriber = asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
        try:
            await asyncio.wait_for(provider.entered.wait(), 3)
            assert (await diagnosis(client, rid))['status'] == 'running'
            accepted = await client.post(f'/v1/llm/stream/{rid}/cancel')
            assert accepted.status_code == 202
            await asyncio.wait_for(provider.closing.wait(), 3)
            assert (await diagnosis(client, rid))['status'] == 'canceling'
        finally:
            provider.close_release.set()
            provider.release.set()
            await asyncio.wait_for(subscriber, 6)
        await poll(client, rid, terminal)
        data = await diagnosis(client, rid)
        row, attempts, _ = assert_events(server, name, rid, terminal)
        assert data['status'] == row['status'] == terminal
        assert data['error_code'] == row['error_code']
        assert data['attempts'][0]['status'] == attempts[0]['status']
        assert len(provider.stream_calls) == provider.stream_close_calls == len(data['attempts']) == 1


async def test_f8_fallback_preserves_partial_usage_and_ordinary_response(server, case):
    name, config = case
    usage = Usage(input_tokens=60, output_tokens=25, total_tokens=85)
    primary = SequenceProvider([ProviderCallError(code='provider_timeout')])
    backup = SequenceProvider([ProviderResult(text='private reply', actual_model='actual-backup', usage=usage)])
    models = registry(policy=RecoveryPolicy(max_attempts=2, max_attempts_per_endpoint=1))
    async with application(config, primary, backup, models=models) as (_, client):
        reply = await client.post('/v1/llm', json=body())
        assert reply.status_code == 200, reply.text
        assert reply.json()['data'] == {'text': 'private reply', 'usage': None}
        rid = reply.headers['x-request-id']
        trace = await diagnosis(client, rid)
        row, attempts, _ = observe(server, name, rid)
        assert trace['status'] == row['status'] == 'completed'
        assert len(primary.calls) == len(backup.calls) == 1
        assert [(a['status'], a['reason']) for a in trace['attempts']] == [
            (a['status'], a['reason']) for a in attempts] == [('failed', 'initial'), ('completed', 'fallback')]
        assert trace['attempts'][1]['actual_model'] == attempts[1]['actual_model'] == 'actual-backup'
        assert trace['usage']['known_subtotal'] == usage.model_dump()
        assert trace['usage']['unknown_attempts'] == [1]
        assert 'private reply' not in str(trace)


async def test_f8_two_ids_overlap_and_each_query_returns_its_own_state(server, case):
    name, config = case
    stream = ControlledProvider()
    async with application(config, stream) as (_, client):
        rid = await create(client)
        subscriber = asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
        try:
            await asyncio.wait_for(stream.entered.wait(), 3)
            bad = await client.post('/v1/llm', json=body(max_output_tokens=999999))
            assert bad.status_code == 400
            bad_id = bad.headers['x-request-id']
            running, failed = await asyncio.gather(diagnosis(client, rid), diagnosis(client, bad_id))
            assert running['status'] == 'running' and len(running['attempts']) == 1
            assert failed['status'] == 'failed' and failed['attempts'] == []
            assert failed['error_stage'] and failed['error_code'] == bad.json()['code']
            assert list(observe(server, name, bad_id)[1]) == []
        finally:
            stream.release.set()
            await subscriber
        assert (await diagnosis(client, rid))['status'] == 'completed'
        assert (await diagnosis(client, bad_id)) == failed
        assert len(stream.stream_calls) == 1 and not stream.calls


async def test_f8_diagnostic_bytes_exclude_inputs_outputs_and_errors(server, case, caplog, monkeypatch):
    from persistence.database import StorageUnavailable
    from providers.mock import MockProvider

    name, config = case
    markers = ['PRIVATE-VARIABLE-A123', 'private_schema_field', 'PRIVATE-REPLY-A123',
               'PRIVATE-DELTA-A123', 'FAKE-KEY-ONLY-TEST', 'PRIVATE-STACK-A123']
    result = ProviderResult(text='{"private_schema_field":"PRIVATE-REPLY-A123"}')
    provider = MockProvider(result=result, stream_events=[
        ProviderStreamEvent(delta=markers[3]),
        ProviderStreamEvent(status='completed', upstream_terminal_received=True),
    ])
    async with application(config, provider) as (app, client):
        request = body(response_schema={'type': 'object', 'properties': {
            markers[1]: {'type': 'string'}}, 'required': [markers[1]], 'additionalProperties': False})
        request['prompt']['variables']['context'] = markers[0]
        request['messages'][0]['content'] = markers[4]
        generated = await client.post('/v1/llm', json=request)
        assert generated.status_code == 200 and markers[2] in generated.text
        rid = generated.headers['x-request-id']
        row, _, _ = observe(server, name, rid)
        assert markers[0] in row['input_snapshot']
        protected = [await client.get(f'/v1/traces/{rid}')]

        stream_body = body(messages=[{'role': 'user', 'content': markers[3]}])
        stream_id = (await client.post('/v1/llm/stream', json=stream_body)).json()['data']['request_id']
        streamed = await client.get(f'/v1/llm/stream/{stream_id}/events')
        assert streamed.status_code == 200 and markers[3] in streamed.text
        assert markers[3] in observe(server, name, stream_id)[2][0]['payload']
        protected.append(await client.get(f'/v1/traces/{stream_id}'))
        def fail_read():
            raise StorageUnavailable() from RuntimeError(markers[5])
        with monkeypatch.context() as m:
            m.setattr(app.state.storage.database, 'transaction', fail_read)
            protected.append(await client.get(f'/v1/traces/{rid}'))
        assert [response.status_code for response in protected] == [200, 200, 503]
        for marker in markers:
            assert all(marker.encode() not in response.content for response in protected)
            assert marker not in caplog.text
