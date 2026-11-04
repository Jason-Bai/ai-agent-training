"""F6 test-owned barriers; cancellation is always performed by product code."""
import asyncio
from contextlib import asynccontextmanager

import httpx

from test_f5o2_http import server, case, body, make_app, observe, frames
from providers.mock import MockProvider
from providers.base import ProviderStreamEvent


class ControlledProvider(MockProvider):
    def __init__(self, *, first=False, close_error=False):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.closing = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_release.set()
        self.first, self.close_error = first, close_error

    async def stream(self, model, messages, *, max_output_tokens, timeout_seconds):
        self.stream_calls.append((model, list(messages)))
        self.entered.set()
        try:
            if self.first:
                self.stream_next_calls += 1
                yield ProviderStreamEvent(delta='订单 A123')
            await self.release.wait()
            self.stream_next_calls += 1
            yield ProviderStreamEvent(delta='不得提交')
            yield ProviderStreamEvent(status='completed', upstream_terminal_received=True)
        finally:
            self.stream_close_calls += 1
            self.closing.set()
            await self.close_release.wait()
            if self.close_error:
                raise RuntimeError('private cleanup exception must not leak')


@asynccontextmanager
async def application(config, provider):
    app = make_app(config, provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                     base_url='http://test') as client:
            yield app, client


async def create(client):
    response = await client.post('/v1/llm/stream', json=body())
    assert response.status_code == 202, response.text
    return response.json()['data']['request_id']


def cancel_url(rid):
    return f'/v1/llm/stream/{rid}/cancel'


async def poll(client, rid, expected='canceled'):
    async with asyncio.timeout(8):
        while True:
            response = await client.post(cancel_url(rid))
            assert response.status_code in {200, 202}, response.text
            value = response.json()
            assert set(value) == {'success', 'code', 'data', 'message'}
            assert value['success'] is True and value['code'] == 'ok'
            assert value['data']['request_id'] == rid
            assert response.headers['x-request-id'] == rid
            if response.status_code == 200:
                assert value['data']['status'] == expected, value
                return response
            assert value['data']['status'] == 'canceling'
            await asyncio.sleep(.01)


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.01)


def assert_events(server, name, rid, terminal='canceled'):
    row, attempts, events = observe(server, name, rid)
    assert row['status'] == terminal
    assert [e['seq'] for e in events] == list(range(1, len(events)+1))
    names = [e['event_type'] for e in events]
    assert names.count('response.canceling') == 1
    assert names[-1] == 'response.' + terminal
    assert sum(n in {'response.completed', 'response.failed', 'response.canceled'} for n in names) == 1
    assert 'response.delta' not in names[names.index('response.canceling')+1:]
    return row, attempts, events
