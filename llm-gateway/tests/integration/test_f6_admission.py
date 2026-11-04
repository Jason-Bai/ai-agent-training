"""Provider admission and durable cancellation share one start boundary."""
import asyncio

import pytest

from f6_cases import (server, case, application, ControlledProvider, create, cancel_url,
                      poll, observe, until, assert_events)
from test_f6_faults import acknowledgement_loss


@pytest.mark.parametrize('committed', [True, False])
async def test_f6_cancel_commit_before_attempt_ack_prevents_new_provider_start(server, case, monkeypatch, committed):
    name, config = case
    provider = ControlledProvider()
    async with application(config, provider) as (app, client):
        begun, release_begin, accepted, release_accept = [asyncio.Event() for _ in range(4)]
        begin = app.state.storage.begin_attempt

        async def held_begin(*args, **kwargs):
            await begin(*args, **kwargs)
            begun.set()
            await release_begin.wait()

        monkeypatch.setattr(app.state.storage, 'begin_attempt', held_begin)
        if not committed:
            acknowledgement_loss(app, monkeypatch, 'accept_cancel', False)
        accept = app.state.storage.accept_cancel

        async def held_accept(*args, **kwargs):
            try:
                status = await accept(*args, **kwargs)
            except Exception:
                accepted.set()
                await release_accept.wait()
                raise
            accepted.set()
            await release_accept.wait()
            return status

        monkeypatch.setattr(app.state.storage, 'accept_cancel', held_accept)
        rid = await create(client)
        manager = app.state.streaming_manager
        await manager.subscribe(rid, 0)
        await asyncio.wait_for(begun.wait(), 3)
        request = asyncio.create_task(client.post(cancel_url(rid)))
        try:
            await asyncio.wait_for(accepted.wait(), 3)
            assert observe(server, name, rid)[0]['status'] == ('canceling' if committed else 'running')
            assert provider.stream_calls == []
            release_begin.set()
            # Let the original executor resume; ACK remains blocked throughout.
            try:
                await asyncio.wait_for(provider.entered.wait(), .1)
            except TimeoutError:
                pass
            calls_before_ack = len(provider.stream_calls)
            release_accept.set()
            response = await request
            if committed:
                await poll(client, rid)
                assert_events(server, name, rid)
                assert calls_before_ack == 0, 'Provider started after durable canceling but before acceptance ACK'
                assert provider.stream_calls == []
            else:
                assert response.status_code == 503
                provider.release.set()
                await until(lambda: observe(server, name, rid)[0]['status'] == 'completed')
                assert len(provider.stream_calls) == 1
                assert not any(e['event_type'] == 'response.canceling' for e in observe(server, name, rid)[2])
        finally:
            release_begin.set()
            release_accept.set()
            provider.release.set()
            await asyncio.gather(request, return_exceptions=True)
