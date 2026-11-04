import asyncio
import httpx
from test_f5o2_http import server, case, make_app, body, observe, frames
from test_f5o2_faults import inject_ack_loss
from providers.mock import MockProvider

async def test_orphan_recovery_waits_for_inflight_retry_claim(server,case,monkeypatch):
    name,config=case;provider=MockProvider();app=make_app(config,provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            rid=(await client.post('/v1/llm/stream',json=body())).json()['data']['request_id'];url=f'/v1/llm/stream/{rid}/events'
            inject_ack_loss(app,monkeypatch,'claim',False)
            assert (await client.get(url)).status_code==503
            assert rid in app.state.streaming_manager.orphans
            original=app.state.storage.claim_stream;committed=asyncio.Event();release=asyncio.Event()
            async def held_claim(*args,**kwargs):
                claimed=await original(*args,**kwargs)
                committed.set();await release.wait()
                return claimed
            monkeypatch.setattr(app.state.storage,'claim_stream',held_claim)
            get=asyncio.create_task(client.get(url));recovery=None
            try:
                await asyncio.wait_for(committed.wait(),2)
                recovery=asyncio.create_task(app.state.streaming_manager.reconcile())
                await asyncio.sleep(.03)
                assert observe(server,name,rid)[0]['status']=='running','Recovery killed an inflight claim before task registration'
                assert not recovery.done(),'Recovery must coordinate with the claim registration lock'
                release.set();response=await asyncio.wait_for(get,3)
                await asyncio.wait_for(recovery,2)
                assert frames(response.text)[-1][1]=='response.completed'
                assert len(provider.stream_calls)==1
            finally:
                release.set()
                await asyncio.gather(*(t for t in [get,recovery] if t is not None),return_exceptions=True)


async def test_new_subscription_preserves_deferred_owner_error_and_usage(server,case,monkeypatch):
    import lifecycle
    from contracts import Usage
    from sqlalchemy import text
    from providers.base import ProviderStreamEvent
    monkeypatch.setattr(lifecycle,'DATABASE_RECONCILE_INTERVAL_SECONDS',3600)
    name,config=case
    usage=Usage(input_tokens=10,output_tokens=3,total_tokens=13)
    provider=MockProvider(stream_events=[ProviderStreamEvent(delta='A123'),
        ProviderStreamEvent(status='failed',error_code='provider_rate_limited',usage=usage)])
    app=make_app(config,provider);original_finish=app.state.storage.finish;injected=False
    async def fail_first_finish(*args,**kwargs):
        nonlocal injected
        if not injected:
            injected=True
            async with app.state.storage.database.transaction() as session:
                await session.execute(text('SELECT 1/0'))
        return await original_finish(*args,**kwargs)
    monkeypatch.setattr(app.state.storage,'finish',fail_first_finish)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            rid=(await client.post('/v1/llm/stream',json=body())).json()['data']['request_id']
            manager=app.state.streaming_manager
            await manager.subscribe(rid,0)
            async with asyncio.timeout(2):
                while rid in manager.tasks:await asyncio.sleep(.005)
            assert injected and observe(server,name,rid)[0]['status']=='running'
            row,_=await manager.subscribe(rid,0)
            assert row['status']=='running','Subscriber replaced the stopped owner pending terminal commit'
    row,attempts,events=observe(server,name,rid)
    assert row['status']=='failed' and row['error_code']=='storage_unavailable'
    assert tuple(attempts[0][key] for key in ('input_tokens','output_tokens','total_tokens'))==(10,3,13)
    assert attempts[0]['provider_error_code']=='provider_rate_limited'
    assert [event['seq'] for event in events]==[1,2]
    assert events[-1]['event_type']=='response.failed' and len(provider.stream_calls)==1
