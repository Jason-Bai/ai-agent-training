"""Database-safe diagnostic facts also apply to injected Provider errors and rejected input."""
import pytest

from contracts import Usage
from errors import ProviderCallError
from f6_cases import server, case, observe
from f7_cases import application
from providers.mock import MockProvider


@pytest.mark.parametrize('bad_field', ['actual_model', 'finish_reason', 'upstream_status', 'upstream_finish_reason'])
async def test_unrepresentable_error_metadata_cannot_block_valid_usage_or_termination(server, case, bad_field):
    name, config = case
    provider = MockProvider(error=ProviderCallError(code='provider_invalid_response',
        usage=Usage(input_tokens=60, output_tokens=25, total_tokens=85), **{bad_field:'invalid\x00meta'}))
    async with application(config, provider) as (_, client):
        failed = await client.post('/v1/models/general-primary/health')
        rid = failed.headers['x-request-id']
        assert (failed.status_code, failed.json()['code']) == (502, 'provider_invalid_response')
        row, attempts, _ = observe(server, name, rid)
        assert row['status'] == 'failed' and attempts[0][bad_field] is None
        detail = (await client.get(f'/v1/traces/{rid}')).json()['data']
        assert detail['usage']['known_subtotal']['total_tokens'] == attempts[0]['total_tokens'] == 85
        assert len(provider.calls) == 1


async def test_rejected_probe_model_name_cannot_prevent_metadata_registration(server, case):
    name, config = case
    provider = MockProvider()
    async with application(config, provider) as (_, client):
        rejected = await client.post('/v1/models/unknown%00alias/health')
        rid = rejected.headers['x-request-id']
        assert rejected.status_code == 400
        record = await client.get(f'/v1/traces/{rid}')
        assert record.status_code == 200
        assert record.json()['data']['status'] == 'failed'
        assert observe(server, name, rid)[0]['requested_model'] is None
        assert not provider.calls


async def test_individually_unrepresentable_usage_is_unknown_in_failed_trace(server, case):
    name, config = case
    provider = MockProvider(error=ProviderCallError(code='provider_invalid_response',
        actual_model='known', usage=Usage(input_tokens=2**63, output_tokens=0, total_tokens=2**63)))
    async with application(config, provider) as (_, client):
        response = await client.post('/v1/models/general-primary/health')
        assert response.status_code == 502
        rid = response.headers['x-request-id']
        saved = observe(server, name, rid)[1][0]
        assert saved['total_tokens'] is None and saved['actual_model'] == 'known'
        assert (await client.get(f'/v1/traces/{rid}')).json()['data']['usage'] == {
            'known_subtotal': None, 'unknown_attempts': [1]}


async def test_f8_known_subtotal_can_exceed_single_attempt_bigint(server, case):
    from f7_cases import body, registry, SequenceProvider
    from providers.base import ProviderResult
    from services.models import RecoveryPolicy

    name, config = case
    maximum = 2**63 - 1
    usage = Usage(input_tokens=maximum, output_tokens=0, total_tokens=maximum)
    primary = SequenceProvider([ProviderCallError(code='provider_timeout', usage=usage)])
    backup = SequenceProvider([ProviderResult(text='ok', usage=usage)])
    models = registry(policy=RecoveryPolicy(max_attempts=2, max_attempts_per_endpoint=1))
    async with application(config, primary, backup, models=models) as (_, client):
        reply = await client.post('/v1/llm', json=body())
        assert reply.status_code == 200
        rid = reply.headers['x-request-id']
        assert [a['total_tokens'] for a in observe(server, name, rid)[1]] == [maximum, maximum]
        data = (await client.get(f'/v1/traces/{rid}')).json()['data']
        assert data['usage']['known_subtotal']['total_tokens'] == reply.json()['data']['usage']['total_tokens'] == 2 * maximum
        assert data['usage']['unknown_attempts'] == []
