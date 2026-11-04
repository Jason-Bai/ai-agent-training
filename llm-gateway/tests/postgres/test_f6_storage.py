"""Cancellation acceptance barrier and atomic terminal records on real PostgreSQL."""
import asyncio
from uuid import uuid4
import pytest
from sqlalchemy.ext.asyncio import AsyncSessionTransaction
from contracts import Usage
from persistence.database import StorageUnavailable
from services.trace import AttemptTrace, RequestTrace
from test_f5o2_storage import pg_server, persisted, trace, claim
from utils.json_codec import loads

async def running(store, record):
    await store.create_pending(record, {})
    assert await claim(store, record.request_id)
    attempt = AttemptTrace(record.request_id, 1, 'general-primary', provider='mock', adapter='mock', configured_model='mock', api_protocol='chat_completions')
    await store.begin_attempt(record, attempt)
    return attempt

async def test_pending_cancel_two_transitions_zero_attempt(pg_server,persisted):
    db,store,name=persisted; r=trace(); await store.create_pending(r,{})
    assert await store.accept_cancel(r.request_id)=='canceling'
    row,events=await store.read_events(r.request_id,0)
    assert row['status']=='canceling' and row['ended_at'] is None
    assert [e['event_type'] for e in events]==['response.canceling']
    assert not await claim(store,r.request_id)
    assert await store.finish_cancel(r)=='canceled'
    row,events=await store.read_events(r.request_id,0)
    assert row['status']=='canceled' and row['ended_at'] is not None
    assert row['started_at'] is row['deadline_at'] is None
    assert [(e['seq'],e['event_type']) for e in events]==[(1,'response.canceling'),(2,'response.canceled')]
    assert all(loads(e['payload'])=={'request_id':r.request_id} for e in events)
    with pg_server.connect(name) as c: assert c.execute('select count(*) from tb_llm_attempt').fetchone()==(0,)
    assert db.engine.pool.checkedout()==0

async def test_twenty_duplicate_accept_and_finish(persisted):
    _,store,_=persisted; r=trace(); await store.create_pending(r,{})
    assert await asyncio.gather(*(store.accept_cancel(r.request_id) for _ in range(20)))==['canceling']*20
    assert await asyncio.gather(*(store.finish_cancel(r) for _ in range(20)))==['canceled']*20
    row,events=await store.read_events(r.request_id,0)
    assert row['last_event_seq']==2 and len(events)==2

@pytest.mark.parametrize('error',[None,'cancel_cleanup_failed'])
@pytest.mark.parametrize('with_attempt',[False,True])
async def test_attempt_usage_and_diagnostics_preserved(pg_server,persisted,error,with_attempt):
    _,store,name=persisted; r=trace(); a=await running(store,r)
    a.usage=Usage(input_tokens=3,output_tokens=2,total_tokens=5);a.actual_model='observed'
    with pg_server.connect(name) as c: c.execute("update tb_llm_attempt set input_tokens=3,output_tokens=2,total_tokens=5,actual_model='observed'")
    r.cleanup_error_code='stream_cleanup_error' if error else None
    await store.accept_cancel(r.request_id)
    expected='failed' if error else 'canceled'
    assert await store.finish_cancel(r,a if with_attempt else None,error_code=error)==expected
    with pg_server.connect(name) as c:
        assert c.execute('select status,input_tokens,output_tokens,total_tokens,actual_model,error_code from tb_llm_attempt').fetchone()==(expected,3,2,5,'observed',error)
    row,events=await store.read_events(r.request_id,0)
    assert row['status']==expected and row['error_code']==error
    assert events[-1]['event_type']=='response.'+expected
    if error: assert loads(events[-1]['payload'])['code']==error

async def test_205_history_cancel_barrier_all_normal_writers(persisted):
    _,store,_=persisted; r=trace(); a=await running(store,r)
    for n in range(205): await store.append_event(r,'response.delta',{'request_id':r.request_id,'delta':str(n)})
    await store.accept_cancel(r.request_id)
    with pytest.raises(ValueError): await store.append_event(r,'response.delta',{'delta':'forbidden'})
    with pytest.raises(ValueError): await store.begin_attempt(r,a)
    for error in [None,'provider_error']:
        assert await store.finish(r,a,error_code=error,event=('response.failed' if error else 'response.completed',{}))=='canceling'
    assert await store.finish_cancel(r,a)=='canceled'
    result=[];cursor=0
    while True:
        row,events=await store.read_events(r.request_id,cursor)
        if not events: break
        result.extend(events);cursor=events[-1]['seq']
    assert len(result)==207 and [e['seq'] for e in result]==list(range(1,208))
    assert [e['event_type'] for e in result]==['response.delta']*205+['response.canceling','response.canceled']

@pytest.mark.parametrize('terminal',['completed','failed','canceled'])
async def test_terminal_winner_preserved(persisted,terminal):
    _,store,_=persisted;r=trace();await store.create_pending(r,{})
    if terminal=='canceled':
        await store.accept_cancel(r.request_id);await store.finish_cancel(r)
    else:
        await claim(store,r.request_id)
        await store.finish(r,error_code='provider_error' if terminal=='failed' else None,event=('response.'+terminal,{'request_id':r.request_id}))
    before=await store.read_events(r.request_id,0)
    assert await store.accept_cancel(r.request_id)==terminal
    assert await store.finish_cancel(r,error_code='cancel_cleanup_failed')==terminal
    assert await store.read_events(r.request_id,0)==before

async def test_invalid_resource_and_unaccepted_finish(persisted):
    _,store,_=persisted;o=RequestTrace(str(uuid4()));rejected=trace();pending=trace()
    await store.prepare(o,{})
    await store.finish(rejected,error_code='invalid_request')
    for rid in [str(uuid4()),o.request_id,rejected.request_id]:
        assert await store.accept_cancel(rid) is None
        assert await store.finish_cancel(RequestTrace(rid)) is None
    await store.create_pending(pending,{})
    assert await store.finish_cancel(pending)=='pending'
    assert (await store.get_stream(pending.request_id))['status']=='pending'

@pytest.mark.parametrize('stage',['accept','finish'])
@pytest.mark.parametrize('committed',[False,True])
async def test_actual_commit_rollback_ack_loss(persisted,monkeypatch,stage,committed):
    db,store,_=persisted;r=trace();await store.create_pending(r,{})
    if stage=='finish':await store.accept_cancel(r.request_id)
    original=AsyncSessionTransaction.__aexit__
    async def lose(tx,*args):
        if committed:await original(tx,*args)
        else:await tx.rollback()
        raise ConnectionError('private acknowledgement lost')
    with monkeypatch.context() as scope:
        scope.setattr(AsyncSessionTransaction,'__aexit__',lose)
        with pytest.raises(StorageUnavailable) as exc:
            if stage=='accept':await store.accept_cancel(r.request_id)
            else:await store.finish_cancel(r)
        assert exc.value.uncertain
    row,events=await store.read_events(r.request_id,0)
    expected=('canceling' if committed else 'pending') if stage=='accept' else ('canceled' if committed else 'canceling')
    assert row['status']==expected and len(events)=={'pending':0,'canceling':1,'canceled':2}[expected]
    assert db.engine.pool.checkedout()==0

async def test_recover_canceling_once_pending_and_terminal_unchanged(persisted):
    _,store,_=persisted;p=trace();i=trace();c=trace()
    for r in [p,i,c]:await store.create_pending(r,{})
    await store.accept_cancel(i.request_id)
    await store.accept_cancel(c.request_id);await store.finish_cancel(c)
    await store.recover();await store.recover()
    assert (await store.get_stream(p.request_id))['status']=='pending'
    assert (await store.get_stream(c.request_id))['status']=='canceled'
    row,events=await store.read_events(i.request_id,0)
    assert row['status']=='failed' and row['error_code']=='execution_interrupted'
    assert [e['event_type'] for e in events]==['response.canceling','response.failed']

async def test_explicit_upgrade_preserves_old_data_and_old_revision_rejected(pg_server,tmp_path):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    from f4_cases import registry
    from postgres_support import write_config
    from persistence.database import Database,load_database_config
    import psycopg
    name=pg_server.create_database()
    try:
        directory=write_config(tmp_path/'old',pg_server,name,registry())
        root=Path(__file__).resolve().parents[2]
        config=Config(str(root/'alembic.ini'))
        config.set_main_option('script_location',str(root/'migrations'))
        config.attributes['config_dir']=directory
        command.upgrade(config,'f5o1_001')
        rid=str(uuid4())
        with pg_server.connect(name) as c:
            pk=c.execute("insert into tb_llm_request (request_id,operation,status,created_at) values (%s,'completion','completed',now()) returning id",(rid,)).fetchone()[0]
            c.execute("insert into tb_llm_attempt (request_pk,attempt_no,model,provider,adapter,api_protocol,configured_model,status,started_at) values (%s,1,'model','mock','mock','chat_completions','mock','completed',now())",(pk,))
            stream_id=str(uuid4())
            stream_pk=c.execute("insert into tb_llm_request (request_id,operation,status,created_at,input_snapshot,last_event_seq) values (%s,'streaming','completed',now(),%s,2) returning id",
                                (stream_id,'{"messages":[{"role":"user","content":"订单 A123"}]}')).fetchone()[0]
            c.execute("insert into tb_llm_attempt (request_pk,attempt_no,model,provider,adapter,api_protocol,configured_model,status,started_at,input_tokens,output_tokens,total_tokens) values (%s,1,'model','mock','mock','chat_completions','mock','completed',now(),3,2,5)",(stream_pk,))
            c.execute("insert into tb_llm_stream_event (request_pk,seq,event_type,payload,created_at) values (%s,1,'response.delta',%s,now()),(%s,2,'response.completed',%s,now())",
                      (stream_pk,'{"request_id":"'+stream_id+'","delta":"订单 A123"}',stream_pk,'{"request_id":"'+stream_id+'","usage":{"input_tokens":3,"output_tokens":2,"total_tokens":5}}'))
            original_requests=c.execute('select * from tb_llm_request order by id').fetchall()
            original_attempts=c.execute('select * from tb_llm_attempt order by request_pk,attempt_no').fetchall()
            original_events=c.execute('select * from tb_llm_stream_event order by request_pk,seq').fetchall()
        database=Database(load_database_config(directory),directory)
        try:
            with pytest.raises(StorageUnavailable):await database.start()
        finally:await database.close()
        command.upgrade(config,'head')
        database=Database(load_database_config(directory),directory)
        try:await database.start()
        finally:await database.close()
        with pg_server.connect(name) as c:
            assert c.execute('select version_num from alembic_version').fetchone()==('f9_001',)
            assert c.execute('select * from tb_llm_request order by id').fetchall()==[(*r, None) for r in original_requests]
            assert [row[:len(original_attempts[0])] for row in c.execute('select * from tb_llm_attempt order by request_pk,attempt_no').fetchall()]==original_attempts
            assert c.execute('select * from tb_llm_stream_event order by request_pk,seq').fetchall()==original_events
            c.execute("update tb_llm_attempt set status='canceled'")
        with pytest.raises(psycopg.errors.CheckViolation):
            with pg_server.connect(name) as c:c.execute("update tb_llm_attempt set status='canceling'")
        with pg_server.connect(name) as c:
            assert {r[0] for r in c.execute("select tablename from pg_tables where schemaname='public'").fetchall()}=={'alembic_version','tb_llm_request','tb_llm_attempt','tb_llm_stream_event','tb_agent','tb_agent_credential','tb_billing_attempt','tb_billing_operation'}
    finally:pg_server.drop_database(name)
