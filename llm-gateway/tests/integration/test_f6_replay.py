"""F6 E: persisted histories survive cancellation and independent readers."""
import asyncio
import json

import pytest

from f6_cases import (server, case, application, ControlledProvider, create, cancel_url,
                      poll, observe, assert_events, until, frames)
from providers.mock import MockProvider
from providers.base import ProviderStreamEvent


async def test_f6_two_subscribers_receive_same_history_and_canceled_terminal(server, case):
    name, config = case
    provider = ControlledProvider(first=True)
    async with application(config, provider) as (_, client):
        rid = await create(client)
        url = f'/v1/llm/stream/{rid}/events'
        one, two = asyncio.create_task(client.get(url)), asyncio.create_task(client.get(url))
        await asyncio.wait_for(provider.entered.wait(), 3)
        await until(lambda: len(observe(server, name, rid)[2]) == 1)
        await poll(client, rid)
        a, b = await asyncio.gather(one, two)
        assert frames(a.text) == frames(b.text)
        assert [f[1] for f in frames(a.text)] == ['response.delta','response.canceling','response.canceled']
        assert_events(server, name, rid)
        assert len(provider.stream_calls) == provider.stream_close_calls == 1


async def test_f6_canceled_history_crosses_pages_and_replays_from_all_boundaries(server, case):
    name, config = case
    gate = asyncio.Event()
    async def blocked():
        await gate.wait()
        return ProviderStreamEvent(delta='不得提交')
    provider = MockProvider(stream_events=[*[ProviderStreamEvent(delta=str(i)) for i in range(205)], blocked])
    async with application(config, provider) as (_, client):
        rid = await create(client); url = f'/v1/llm/stream/{rid}/events'
        subscriber = asyncio.create_task(client.get(url))
        await until(lambda: len(observe(server, name, rid)[2]) == 205)
        await poll(client, rid)
        live = frames((await subscriber).text)
        row, attempts, saved = assert_events(server, name, rid)
        assert len(saved) == 207 and row['last_event_seq'] == 207
        for cursor in [None, 0, 99, 205, 206]:
            response = await client.get(url + ('' if cursor is None else f'?after_seq={cursor}'))
            assert frames(response.text) == live[(cursor or 0):]
        assert (await client.get(url+'?after_seq=207')).status_code == 204
        assert (await client.get(url+'?after_seq=208')).status_code == 400
        assert len(provider.stream_calls) == 1 and len(attempts) == 1
        assert all(json.loads(e['payload'])['request_id'] == rid for e in saved)


@pytest.mark.parametrize('cursor', ['', '-1', '1.1', 'true', '0&after_seq=0', '0&other=1'])
async def test_f6_cancel_does_not_weaken_cursor_validation(server, case, cursor):
    _, config = case
    provider = MockProvider()
    async with application(config, provider) as (_, client):
        rid = await create(client)
        await poll(client, rid)
        response = await client.get(f'/v1/llm/stream/{rid}/events?after_seq={cursor}')
        assert response.status_code == 422
        assert provider.stream_calls == []
