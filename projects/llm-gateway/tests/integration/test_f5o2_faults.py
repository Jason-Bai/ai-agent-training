"""Stage-specific faults use real commits/rollbacks and independent PG reads."""
import asyncio
from contextvars import ContextVar
import json
import time

import httpx
from legacy_identity import ASGI_AUTH
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction

from test_f5o2_http import server, case, make_app, body, observe, frames
from providers.mock import MockProvider
from providers.base import ProviderStreamEvent


def execute_ddl(server, name, statement):
    # DDL may wait for a background transaction: never block its event loop.
    with server.connect(name) as conn:
        conn.execute("SET lock_timeout='3s'")
        conn.execute(statement)


def inject_ack_loss(app, monkeypatch, stage, committed):
    marked = ContextVar('f5o2_fault_stage', default=False)
    original_exit = AsyncSessionTransaction.__aexit__
    method = {'creation':'create_pending', 'claim':'claim_stream',
              'delta':'append_event', 'terminal':'finish'}[stage]
    original_method = getattr(app.state.storage, method)
    receipt = {'count': 0}

    async def exit_transaction(transaction, *args):
        if not marked.get() or receipt['count']:
            return await original_exit(transaction, *args)
        receipt['count'] += 1
        if committed:
            await original_exit(transaction, *args)
        else:
            await transaction.rollback()
        raise ConnectionError('controlled private commit acknowledgement loss')

    async def operation(*args, **kwargs):
        token = marked.set(True)
        try:
            return await original_method(*args, **kwargs)
        finally:
            marked.reset(token)

    monkeypatch.setattr(AsyncSessionTransaction, '__aexit__', exit_transaction)
    monkeypatch.setattr(app.state.storage, method, operation)
    return receipt


async def settled(server, name, rid, expected, timeout=4):
    until = time.monotonic()+timeout
    while time.monotonic() < until:
        row, attempts, events = observe(server, name, rid)
        if row and row['status'] == expected:
            return row, attempts, events
        await asyncio.sleep(.03)
    raise AssertionError(f'request did not settle as {expected}: {row}')


@pytest.mark.parametrize('committed', [True, False])
async def test_creation_ack_loss_preserves_committed_pending_without_generation(server, case, monkeypatch, committed):
    name, config = case
    provider = MockProvider(); app = make_app(config, provider)
    receipt = inject_ack_loss(app, monkeypatch, 'creation', committed)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/v1/llm/stream', json=body())
            assert response.status_code == 503 and response.json()['code'] == 'storage_unavailable'
            rid = response.headers['x-request-id']
            await settled(server, name, rid, 'pending' if committed else 'failed')
            await asyncio.sleep(1.1)
            row, attempts, events = observe(server, name, rid)
            assert row['status'] == ('pending' if committed else 'failed')
            assert bool(row['input_snapshot']) == committed
            assert attempts == events == [] and provider.stream_calls == []
            assert receipt['count'] == 1
            if committed:
                result = await client.get(f'/v1/llm/stream/{rid}/events')
                assert frames(result.text)[-1][1] == 'response.completed'
                assert len(provider.stream_calls) == 1


@pytest.mark.parametrize('committed', [True, False])
async def test_claim_ack_loss_with_concurrent_get_never_reissues_model(server, case, monkeypatch, committed):
    name, config = case
    provider = MockProvider(delay=.1); app = make_app(config, provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            created = await client.post('/v1/llm/stream', json=body())
            rid = created.json()['data']['request_id']; url = f'/v1/llm/stream/{rid}/events'
            receipt = inject_ack_loss(app, monkeypatch, 'claim', committed)
            results = await asyncio.gather(*(client.get(url) for _ in range(5)))
            assert sum(result.status_code == 503 for result in results) == 1
            assert receipt['count'] == 1
            row, attempts, events = await settled(server, name, rid, 'failed' if committed else 'completed')
            assert len(provider.stream_calls) == (0 if committed else 1)
            assert len(attempts) == (0 if committed else 1)
            assert sum(event['event_type'] in {'response.completed','response.failed'} for event in events) == 1
            if committed:
                assert row['error_code'] == 'execution_interrupted'
            else:
                assert frames((await client.get(url)).text)[-1][1] == 'response.completed'


@pytest.mark.parametrize('stage', ['delta', 'terminal'])
@pytest.mark.parametrize('committed', [True, False])
async def test_generation_ack_loss_reconciles_actual_commit_without_duplicate_event(server, case, monkeypatch, stage, committed):
    name, config = case
    provider = MockProvider(stream_events=[ProviderStreamEvent(delta='订单 A123'),
        ProviderStreamEvent(status='completed', upstream_terminal_received=True)])
    app = make_app(config, provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            created = await client.post('/v1/llm/stream', json=body())
            rid = created.json()['data']['request_id']; url = f'/v1/llm/stream/{rid}/events'
            receipt = inject_ack_loss(app, monkeypatch, stage, committed)
            results = await asyncio.gather(client.get(url), client.get(url))
            expected = 'completed' if stage == 'terminal' and committed else 'failed'
            row, attempts, events = await settled(server, name, rid, expected)
            assert receipt['count'] == 1
            assert len(provider.stream_calls) == len(attempts) == 1
            assert [e['seq'] for e in events] == list(range(1, len(events)+1))
            deltas = [e for e in events if e['event_type'] == 'response.delta']
            assert len(deltas) == (0 if stage == 'delta' and not committed else 1)
            assert len([e for e in events if e['event_type'].startswith('response.') and e['event_type'] != 'response.delta']) == 1
            assert events[-1]['event_type'] == 'response.'+expected
            assert all(frames(response.text) == frames(results[0].text) for response in results)
            if expected == 'completed':
                assert row['error_code'] is None and attempts[0]['error_code'] is None
            assert provider.stream_close_calls == 1


@pytest.mark.parametrize('stage', ['creation','claim','attempt','delta','terminal'])
async def test_real_sql_write_failure_has_no_uncommitted_event_or_extra_provider(server, case, stage):
    name, config = case
    provider = MockProvider(); app = make_app(config, provider)
    table = {'creation':'tb_llm_request','claim':'tb_llm_request','attempt':'tb_llm_attempt',
             'delta':'tb_llm_stream_event','terminal':'tb_llm_stream_event'}[stage]
    operation = 'UPDATE' if stage == 'claim' else 'INSERT'
    condition = {'creation':"NEW.status='pending'", 'claim':"OLD.status='pending' AND NEW.status='running'",
                 'attempt':'true', 'delta':"NEW.event_type='response.delta'",
                 'terminal':"NEW.event_type='response.completed'"}[stage]
    async with app.router.lifespan_context(app):
        with server.connect(name) as conn:
            conn.execute(f"CREATE FUNCTION stage_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF {condition} THEN RAISE EXCEPTION 'controlled private SQL fault'; END IF; RETURN NEW; END $$")
            conn.execute(f'CREATE TRIGGER stage_fault BEFORE {operation} ON {table} FOR EACH ROW EXECUTE FUNCTION stage_fault()')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            created = await client.post('/v1/llm/stream', json=body())
            rid = created.headers['x-request-id']; url = f'/v1/llm/stream/{rid}/events'
            if stage == 'creation':
                assert created.status_code == 503
            else:
                assert created.status_code == 202
                result = await client.get(url)
                assert result.status_code == (503 if stage == 'claim' else 200)
            if stage == 'claim':
                row, attempts, events = observe(server, name, rid)
                assert row['status'] == 'pending' and attempts == events == []
            else:
                row, attempts, events = await settled(server, name, rid, 'failed')
                assert row['last_event_seq'] == (0 if stage == 'creation' else 2 if stage == 'terminal' else 1)
                assert not any(e['event_type'] == 'response.completed' for e in events)
            assert len(provider.stream_calls) == (1 if stage in {'delta','terminal'} else 0)
            assert 'controlled private SQL fault' not in created.text
            await asyncio.to_thread(execute_ddl,server,name,f'DROP TRIGGER stage_fault ON {table}')


async def test_subscription_real_sql_read_failure_stops_only_reader(server, case, monkeypatch):
    from sqlalchemy import text
    name, config = case
    provider = MockProvider(delay=.15); app = make_app(config, provider)
    original_read = app.state.storage.read_events
    original_execute = AsyncSession.execute
    target = ContextVar('reader_fault', default=False); injected = False
    async def read(*args, **kwargs):
        token=target.set(True)
        try:
            return await original_read(*args, **kwargs)
        finally:
            target.reset(token)
    async def execute(session, statement, *args, **kwargs):
        nonlocal injected
        if target.get() and not injected:
            injected=True
            return await original_execute(session, text('SELECT nonexistent_f5o2_column'))
        return await original_execute(session, statement, *args, **kwargs)
    monkeypatch.setattr(app.state.storage, 'read_events', read)
    monkeypatch.setattr(AsyncSession, 'execute', execute)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            created=await client.post('/v1/llm/stream',json=body()); rid=created.json()['data']['request_id'];url=f'/v1/llm/stream/{rid}/events'
            first=await client.get(url)
            assert first.status_code==200 and frames(first.text)==[]
            assert observe(server,name,rid)[0]['status']=='running'
            row,attempts,events=await settled(server,name,rid,'completed')
            assert len(provider.stream_calls)==len(attempts)==1 and row['error_code'] is None
            assert frames((await client.get(url)).text)[-1][1]=='response.completed'


async def test_creation_transport_failure_preserves_pending(server, case):
    name,config=case;provider=MockProvider();app=make_app(config,provider);messages=[]
    raw=json.dumps(body()).encode();received=False
    async def receive():
        nonlocal received
        if not received:
            received=True
            return {'type':'http.request','body':raw,'more_body':False}
        await asyncio.Event().wait()
    async def send(message):
        messages.append(message)
        if message['type']=='http.response.body':
            raise BrokenPipeError('controlled transport closed')
    scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','scheme':'http',
        'method':'POST','path':'/v1/llm/stream','raw_path':b'/v1/llm/stream','query_string':b'',
        'root_path':'','headers':[ASGI_AUTH, (b'content-type',b'application/json')],'server':('test',80),'client':('127.0.0.1',12345)}
    async with app.router.lifespan_context(app):
        with pytest.raises(BrokenPipeError):
            await app(scope,receive,send)
        rid=dict(messages[0]['headers'])[b'x-request-id'].decode()
        await asyncio.sleep(1.1)
        row,attempts,events=observe(server,name,rid)
        assert row['status']=='pending' and row['error_code'] is None
        assert attempts==events==[] and provider.stream_calls==[]


async def test_registration_failure_closes_coroutine_and_unowned_claim_fails(server, case, monkeypatch):
    name,config=case;provider=MockProvider();app=make_app(config,provider)
    original_create=asyncio.create_task;injected=False;closed=[]
    def create_task(coro,*args,**kwargs):
        nonlocal injected
        if not injected and getattr(coro,'cr_code',None) is not None and coro.cr_code.co_name=='_generate':
            injected=True
            closed.append(coro)
            # The application must close a coroutine whose scheduling failed.
            raise RuntimeError('controlled task registration failure')
        return original_create(coro,*args,**kwargs)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),base_url='http://test') as client:
            created=await client.post('/v1/llm/stream',json=body());rid=created.json()['data']['request_id'];url=f'/v1/llm/stream/{rid}/events'
            monkeypatch.setattr(asyncio,'create_task',create_task)
            result=await client.get(url)
            assert result.status_code==500
            row,attempts,events=await settled(server,name,rid,'failed')
            assert row['error_code']=='execution_interrupted' and attempts==[] and provider.stream_calls==[]
            assert closed[0].cr_frame is None, 'failed task registration leaked its unscheduled coroutine'
            assert len(events)==1 and events[0]['event_type']=='response.failed'


@pytest.mark.parametrize('events,code', [([object()],'provider_invalid_response'),
    ([RuntimeError('private internal failure')],'internal_error')])
async def test_invalid_provider_or_unexpected_exception_fails_once(server,case,events,code):
    name,config=case;provider=MockProvider(stream_events=events);app=make_app(config,provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            created=await client.post('/v1/llm/stream',json=body());rid=created.json()['data']['request_id']
            result=await client.get(f'/v1/llm/stream/{rid}/events')
            row,attempts,stored=await settled(server,name,rid,'failed')
            assert row['error_code']==code and len(provider.stream_calls)==len(attempts)==1
            assert frames(result.text)[-1][2]['code']==code and len(stored)==1
            assert provider.stream_close_calls==1 and 'private internal failure' not in result.text


async def test_claim_commit_time_is_inside_generation_budget(server,case,monkeypatch):
    name,config=case;provider=MockProvider(delay=.8);app=make_app(config,provider)
    original=app.state.storage.claim_stream
    async def delayed_claim(*args,**kwargs):
        result=await original(*args,**kwargs)
        await asyncio.sleep(.35)
        return result
    monkeypatch.setattr(app.state.storage,'claim_stream',delayed_claim)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            created=await client.post('/v1/llm/stream',json=body(timeout_seconds=1));rid=created.json()['data']['request_id']
            start=time.monotonic()
            result=await client.get(f'/v1/llm/stream/{rid}/events')
            row,attempts,events=await settled(server,name,rid,'failed')
            assert row['error_code']=='request_timeout'
            assert len(provider.stream_calls)==len(attempts)==1
            assert provider.stream_options[0]['timeout_seconds'] < .75
            assert (row['deadline_at']-row['started_at']).total_seconds()==1
            assert events[-1]['event_type']=='response.failed' and time.monotonic()-start<2.5


async def test_invalid_entry_stays_422_when_error_metadata_insert_fails(server,case):
    name,config=case;provider=MockProvider();app=make_app(config,provider)
    async with app.router.lifespan_context(app):
        with server.connect(name) as conn:
            conn.execute("CREATE FUNCTION reject_request() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'controlled secret write failure'; END $$")
            conn.execute('CREATE TRIGGER reject_request BEFORE INSERT ON tb_llm_request FOR EACH ROW EXECUTE FUNCTION reject_request()')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            result=await client.post('/v1/llm/stream',json=body(messages=[]))
            assert result.status_code==422 and result.json()['code']=='invalid_request'
            rid=result.headers['x-request-id']
            assert observe(server,name,rid)==(None,[],[])
            assert provider.calls==provider.stream_calls==[]
            await asyncio.to_thread(execute_ddl,server,name,'DROP TRIGGER reject_request ON tb_llm_request')
            row,attempts,events=await settled(server,name,rid,'failed')
            assert row['error_code']=='invalid_request' and row['input_snapshot'] is None
            assert attempts==events==[]


@pytest.mark.parametrize('exit_kind',['raises','canceled_before_start'])
async def test_generation_task_unexpected_exit_is_collected_and_failed(server,case,monkeypatch,exit_kind):
    from services.streaming_manager import StreamingManager
    name,config=case;provider=MockProvider();app=make_app(config,provider)
    if exit_kind=='raises':
        async def broken_generation(self,*args):
            raise RuntimeError('private early task failure')
        monkeypatch.setattr(StreamingManager,'_generate',broken_generation)
    else:
        original=asyncio.create_task
        def cancel_before_start(coro,*args,**kwargs):
            task=original(coro,*args,**kwargs)
            if getattr(coro,'cr_code',None) is not None and coro.cr_code.co_name=='_generate':
                task.cancel()
            return task
        monkeypatch.setattr(asyncio,'create_task',cancel_before_start)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            result=await client.post('/v1/llm/stream',json=body());rid=result.json()['data']['request_id']
            subscribed=await client.get(f'/v1/llm/stream/{rid}/events')
            row,attempts,events=await settled(server,name,rid,'failed')
            assert row['error_code']=='internal_error' and attempts==[] and provider.stream_calls==[]
            assert frames(subscribed.text)[-1][2]['code']=='internal_error'
            assert len(events)==1 and rid not in app.state.streaming_manager.active
            assert rid not in app.state.streaming_manager.tasks


async def test_generation_budget_includes_actual_delta_transaction_wait(server,case,monkeypatch):
    name,config=case;provider=MockProvider();app=make_app(config,provider)
    original=app.state.storage._event
    async def delay_delta(session,row,name,payload):
        if name=='response.delta':
            await asyncio.sleep(1.2)
        return await original(session,row,name,payload)
    monkeypatch.setattr(app.state.storage,'_event',delay_delta)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            created=await client.post('/v1/llm/stream',json=body(timeout_seconds=1));rid=created.json()['data']['request_id']
            start=time.monotonic()
            result=await client.get(f'/v1/llm/stream/{rid}/events')
            row,attempts,events=await settled(server,name,rid,'failed')
            assert row['error_code']=='request_timeout' and len(provider.stream_calls)==len(attempts)==1
            assert len(events)==1 and events[0]['seq']==1 and events[0]['event_type']=='response.failed'
            assert frames(result.text)[-1][2]['code']=='request_timeout'
            assert time.monotonic()-start<2.5 and provider.stream_close_calls==1


async def test_get_prestart_lookup_real_sql_failure_returns_503_then_safe_retry(server,case,monkeypatch):
    from sqlalchemy import text
    name,config=case;provider=MockProvider();app=make_app(config,provider)
    original_lookup=app.state.storage.get_stream;original_execute=AsyncSession.execute
    target=ContextVar('lookup_fault',default=False);injected=False
    async def lookup(*args,**kwargs):
        token=target.set(True)
        try:
            return await original_lookup(*args,**kwargs)
        finally:
            target.reset(token)
    async def execute(session,statement,*args,**kwargs):
        nonlocal injected
        if target.get() and not injected:
            injected=True
            return await original_execute(session,text('SELECT nonexistent_f5o2_lookup_column'))
        return await original_execute(session,statement,*args,**kwargs)
    monkeypatch.setattr(app.state.storage,'get_stream',lookup)
    monkeypatch.setattr(AsyncSession,'execute',execute)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            created=await client.post('/v1/llm/stream',json=body());rid=created.json()['data']['request_id'];url=f'/v1/llm/stream/{rid}/events'
            first=await client.get(url)
            assert first.status_code==503 and first.json()['code']=='storage_unavailable'
            row,attempts,events=observe(server,name,rid)
            assert row['status']=='pending' and attempts==events==[] and provider.stream_calls==[]
            result=await client.get(url)
            assert frames(result.text)[-1][1]=='response.completed' and len(provider.stream_calls)==1
