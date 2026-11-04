"""Compare Mock reports at the HTTP, PostgreSQL and diagnostic boundaries."""
import pytest

from contracts import Usage
from errors import ProviderCallError
from f3_cases import MOCK_USAGE
from f6_cases import case, frames, observe, server
from f7_cases import application, body, registry
from providers.base import ProviderResult, ProviderStreamEvent
from providers.mock import MockProvider
from services.models import RecoveryPolicy


CUSTOM = {'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5}


@pytest.mark.parametrize('mode', ['completion', 'streaming'])
@pytest.mark.parametrize('expected', [MOCK_USAGE, CUSTOM, None], ids=['default', 'custom', 'missing'])
async def test_mock_report_round_trip(server, case, mode, expected):
    name, config = case
    mock = MockProvider()
    if expected != MOCK_USAGE:
        usage = Usage(**expected) if expected else None
        mock = MockProvider(result=ProviderResult(text='ok', usage=usage), stream_events=[
            ProviderStreamEvent(delta='ok'),
            ProviderStreamEvent(status='completed', upstream_terminal_received=True, usage=usage),
        ])
    async with application(config, mock) as (_, client):
        if mode == 'completion':
            response = await client.post('/v1/llm', json=body())
            assert response.status_code == 200
            rid = response.headers['x-request-id']
            assert response.json()['data']['usage'] == expected
        else:
            created = await client.post('/v1/llm/stream', json=body())
            rid = created.json()['data']['request_id']
            url = f'/v1/llm/stream/{rid}/events'
            delta, terminal = frames((await client.get(url)).text)
            assert delta[1] == 'response.delta' and 'usage' not in delta[2]
            assert terminal[1:] == ('response.completed', {'request_id': rid, 'usage': expected})
            assert frames((await client.get(url, params={'after_seq': delta[0]})).text) == [terminal]
            assert len(mock.stream_calls) == 1
        detail = (await client.get(f'/v1/traces/{rid}')).json()['data']
        row, attempts, _ = observe(server, name, rid)
        attempt, = attempts
        assert row['input_token_count'] == detail['input_token_count'] != MOCK_USAGE['input_tokens']
        for key in MOCK_USAGE:
            assert attempt[key] == (expected[key] if expected else None)
        assert detail['attempts'][0]['usage'] == expected
        assert detail['usage'] == {'known_subtotal': expected, 'unknown_attempts': [] if expected else [1]}


async def test_unknown_failure_and_default_mock_fallback(server, case):
    name, config = case
    models = registry(policy=RecoveryPolicy(max_attempts=2, max_attempts_per_endpoint=1))
    primary = MockProvider(error=ProviderCallError(code='provider_timeout'))
    async with application(config, primary, MockProvider(), models=models) as (_, client):
        reply = await client.post('/v1/llm', json=body())
        assert reply.status_code == 200 and reply.json()['data']['usage'] is None
        rid = reply.headers['x-request-id']
        trace = (await client.get(f'/v1/traces/{rid}')).json()['data']
        assert trace['usage'] == {'known_subtotal': MOCK_USAGE, 'unknown_attempts': [1]}
        assert [a['total_tokens'] for a in observe(server, name, rid)[1]] == [None, 11]
