"""F7 subsequent invocation admission shares the F6 durable cancel barrier."""
import asyncio
import pytest
from f7_stream_cases import (server,case,application,ScriptedStream,success,failure,create,observe,until,poll,cancel_url)


@pytest.mark.parametrize('number',[2,3])
async def test_f7_later_attempt_waits_for_cancel_ack_and_never_enters_provider(server,case,monkeypatch,number):
    name,config=case
    primary=ScriptedStream([[failure()],[failure()]])
    backup=ScriptedStream([success()])
    async with application(config,primary,backup) as (app,client):
        begun,release_begin,accepted,release_accept=[asyncio.Event() for _ in range(4)]
        begin=app.state.storage.begin_attempt
        async def held_begin(trace,attempt,**kwargs):
            await begin(trace,attempt,**kwargs)
            if attempt.number==number:
                begun.set();await release_begin.wait()
        monkeypatch.setattr(app.state.storage,'begin_attempt',held_begin)
        accept=app.state.storage.accept_cancel
        async def held_accept(*args,**kwargs):
            status=await accept(*args,**kwargs)
            accepted.set();await release_accept.wait();return status
        monkeypatch.setattr(app.state.storage,'accept_cancel',held_accept)
        rid=await create(client)
        await app.state.streaming_manager.subscribe(rid,0)
        await asyncio.wait_for(begun.wait(),4)
        cancel=asyncio.create_task(client.post(cancel_url(rid)))
        try:
            await asyncio.wait_for(accepted.wait(),4)
            assert observe(server,name,rid)[0]['status']=='canceling'
            release_begin.set()
            # The ACK remains held while all ordinary ready tasks get a turn.
            for _ in range(20):await asyncio.sleep(0)
            assert len(primary.stream_calls)+len(backup.stream_calls)==number-1
            release_accept.set();await cancel
            await poll(client,rid)
            row,attempts,events=observe(server,name,rid)
            assert row['status']=='canceled'
            assert len(primary.stream_calls)+len(backup.stream_calls)==number-1
            assert sum(e['event_type']=='response.canceled' for e in events)==1
            assert not any(a['provider_entered'] for a in attempts[number-1:])
        finally:
            release_begin.set();release_accept.set()
            await asyncio.gather(cancel,return_exceptions=True)


@pytest.mark.parametrize('growth',[False,True])
async def test_f7_cancel_between_failed_attempt_and_recovery_prevents_next_call(server,case,monkeypatch,growth):
    name,config=case
    provider=ScriptedStream([[failure('output_truncated' if growth else 'provider_connection_error')],success()])
    async with application(config,provider) as (app,client):
        saved,release=asyncio.Event(),asyncio.Event()
        original=app.state.storage.finish_attempt
        async def held(*args,**kwargs):
            status=await original(*args,**kwargs);saved.set();await release.wait();return status
        monkeypatch.setattr(app.state.storage,'finish_attempt',held)
        rid=await create(client,initial_output_tokens=256,max_output_tokens=1024)
        await app.state.streaming_manager.subscribe(rid,0)
        await asyncio.wait_for(saved.wait(),3)
        await poll(client,rid)
        release.set()
        assert len(provider.stream_calls)==1
        row,_,events=observe(server,name,rid)
        assert row['status']=='canceled' and events[-1]['event_type']=='response.canceled'


async def test_f7_cancel_interrupts_retry_after_wait_without_poll_driving_cleanup(server,case,monkeypatch):
    name,config=case
    provider=ScriptedStream([[failure('provider_rate_limited',retry_after_seconds=5.)],success()])
    async with application(config,provider) as (app,client):
        saved=asyncio.Event();original=app.state.storage.finish_attempt
        async def ended(*args,**kwargs):
            result=await original(*args,**kwargs);saved.set();return result
        monkeypatch.setattr(app.state.storage,'finish_attempt',ended)
        rid=await create(client)
        await app.state.streaming_manager.subscribe(rid,0)
        await asyncio.wait_for(saved.wait(),3)
        response=await client.post(cancel_url(rid))
        assert response.status_code in {200,202}
        await until(lambda:observe(server,name,rid)[0]['status']=='canceled')
        assert len(provider.stream_calls)==1
        assert (await client.post(cancel_url(rid))).json()['data']['status']=='canceled'


@pytest.mark.parametrize('result',['completed','failed'])
@pytest.mark.parametrize('cancel_first',[False,True])
async def test_f7_final_result_vs_cancel_uses_pg_winner(server,case,monkeypatch,result,cancel_first):
    name,config=case
    terminal=success() if result=='completed' else [failure('provider_auth_error')]
    provider=ScriptedStream([[failure()],terminal])
    async with application(config,provider) as (app,client):
        entered,release=asyncio.Event(),asyncio.Event();original=app.state.storage.finish
        async def held(*args,**kwargs):
            entered.set();await release.wait();return await original(*args,**kwargs)
        monkeypatch.setattr(app.state.storage,'finish',held)
        rid=await create(client)
        subscriber=asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
        try:
            await asyncio.wait_for(entered.wait(),3)
            if cancel_first:
                await poll(client,rid);release.set()
            else:
                release.set();await until(lambda:observe(server,name,rid)[0]['status']==result)
                await poll(client,rid,result)
            await subscriber
            row,_,events=observe(server,name,rid)
            assert row['status']==('canceled' if cancel_first else result)
            assert len(provider.stream_calls)==2
            assert sum(e['event_type'] in {'response.completed','response.failed','response.canceled'} for e in events)==1
        finally:
            release.set();await asyncio.gather(subscriber,return_exceptions=True)


async def test_f7_two_requests_share_client_but_not_recovery_budget_or_usage(server,case):
    from providers.base import ProviderStreamEvent
    from providers.mock import MockProvider
    from contracts import Usage
    name,config=case
    class Shared(MockProvider):
        def __init__(self):
            super().__init__();self.counts={};self.release=asyncio.Event();self.budgets=[]
        async def stream(self,model,messages,*,max_output_tokens,timeout_seconds):
            key=messages[-1].content
            self.counts[key]=self.counts.get(key,0)+1
            self.stream_calls.append((model,list(messages)));self.budgets.append((key,max_output_tokens))
            try:
                if key=='订单 A123':
                    if self.counts[key]==1:raise failure()
                    await self.release.wait()
                    yield ProviderStreamEvent(delta='A不能到达')
                else:
                    yield ProviderStreamEvent(delta='B456')
                    yield ProviderStreamEvent(status='completed',upstream_terminal_received=True,usage=Usage(input_tokens=4,output_tokens=1,total_tokens=5))
            finally:self.stream_close_calls+=1
    provider=Shared()
    async with application(config,provider) as (app,client):
        a=await create(client,max_output_tokens=512)
        await app.state.streaming_manager.subscribe(a,0)
        await until(lambda:provider.counts.get('订单 A123')==2)
        b=await create(client,max_output_tokens=1024,messages=[{'role':'user','content':'订单 B456'}])
        answer=await client.get(f'/v1/llm/stream/{b}/events')
        await poll(client,a)
        provider.release.set()
        ar,aa,ae=observe(server,name,a);br,ba,be=observe(server,name,b)
        assert ar['status']=='canceled' and br['status']=='completed'
        assert len(aa)==2 and len(ba)==1 and provider.counts=={'订单 A123':2,'订单 B456':1}
        assert provider.budgets==[('订单 A123',512),('订单 A123',512),('订单 B456',1024)]
        assert ba[0]['total_tokens']==5 and all(x['total_tokens'] is None for x in aa)
        assert 'A不能到达' not in answer.text and 'B456' in answer.text


@pytest.mark.parametrize('mode',['error','block','swallow'])
async def test_f7_failed_iterator_cleanup_prevents_all_recovery(server,case,mode):
    from f7_stream_cases import ClosingFailureProvider
    name,config=case;provider=ClosingFailureProvider(mode)
    async with application(config,provider) as (app,client):
        rid=await create(client)
        try:
            await app.state.streaming_manager.subscribe(rid,0)
            await asyncio.wait_for(provider.closing.wait(),3)
            await until(lambda:rid not in app.state.streaming_manager.tasks)
            row,attempts,_=observe(server,name,rid)
            await until(lambda:observe(server,name,rid)[0]['status']=='failed')
            assert len(provider.stream_calls)==1 and len(attempts)==1
            trace=app.state.traces.requests[rid]
            assert trace.cleanup_error_code==('stream_cleanup_error' if mode=='error' else 'stream_cleanup_timeout')
            if mode=='swallow':assert provider.cancelled.is_set()
        finally:
            provider.release.set()
            await until(lambda:provider.active_closes==0)


async def test_f7_cancel_again_during_failed_iterator_cleanup_still_never_retries(server,case):
    from f7_stream_cases import ClosingFailureProvider
    name,config=case;provider=ClosingFailureProvider('swallow')
    async with application(config,provider) as (app,client):
        rid=await create(client)
        try:
            await app.state.streaming_manager.subscribe(rid,0)
            await asyncio.wait_for(provider.closing.wait(),3)
            generation=app.state.streaming_manager.tasks[rid]
            response=await client.post(cancel_url(rid));assert response.status_code in {200,202}
            generation.cancel();await asyncio.sleep(0);generation.cancel()
            await until(lambda:rid not in app.state.streaming_manager.tasks)
            assert len(provider.stream_calls)==1
            trace=app.state.traces.requests[rid]
            assert trace.cleanup_error_code=='stream_cleanup_timeout'
            await poll(client,rid,'failed')
            assert observe(server,name,rid)[0]['error_code']=='cancel_cleanup_failed'
        finally:
            provider.release.set()
            await until(lambda:provider.active_closes==0)
