"""F7 Attempt end transactions and invocation evidence on real PostgreSQL."""
import pytest
from sqlalchemy.ext.asyncio import AsyncSessionTransaction
from contracts import Usage
from persistence.database import StorageUnavailable
from services.trace import AttemptTrace
from test_f5o2_storage import pg_server, persisted, trace, claim
from utils.json_codec import loads


def attempt(r, n=1):
    a = AttemptTrace(r.request_id, n, 'general-primary', provider='mock', adapter='mock', configured_model='mock', api_protocol='chat_completions')
    a.input_snapshot = {'messages': [{'role': 'user', 'content': '订单 A123'}]}
    a.prompt_fingerprint = 'hash'
    a.input_token_count, a.counter_version, a.max_output_tokens = 12, 'counter@1', 1024
    a.reason, a.provider_entered = ('initial' if n == 1 else 'retry'), False
    return a


async def admitted(store):
    r = trace()
    await store.create_pending(r, {})
    assert await claim(store, r.request_id)
    a = attempt(r)
    await store.begin_attempt(r, a)
    return r, a


async def test_exact_attempt_evidence_at_registration(pg_server, persisted):
    _, store, name = persisted
    r, a = await admitted(store)
    with pg_server.connect(name) as c:
        cols = {row[0] for row in c.execute("select column_name from information_schema.columns where table_name='tb_llm_attempt'")}
        assert {'input_snapshot', 'prompt_fingerprint', 'input_token_count', 'counter_version', 'max_output_tokens', 'reason', 'provider_entered'} <= cols
        row = c.execute('select input_snapshot,prompt_fingerprint,input_token_count,counter_version,max_output_tokens,reason,provider_entered from tb_llm_attempt').fetchone()
    assert loads(row[0]) == a.input_snapshot
    assert row[1:] == ('hash', 12, 'counter@1', 1024, 'initial', False)


async def test_failed_attempt_then_success_keeps_request_running(pg_server, persisted):
    _, store, name = persisted
    r, a = await admitted(store)
    a.status, a.error_code, a.provider_entered = 'failed', 'provider_connection_error', True
    a.usage = Usage(input_tokens=3, output_tokens=2, total_tokens=5)
    assert await store.finish_attempt(r, a) == 'running'
    row, events = await store.read_events(r.request_id, 0)
    assert row['status'] == 'running' and row['ended_at'] is None and events == []
    b = attempt(r, 2)
    b.max_output_tokens = 4096
    await store.begin_attempt(r, b)
    with pg_server.connect(name) as c:
        assert c.execute('select attempt_no,status,error_code,provider_entered,total_tokens,max_output_tokens from tb_llm_attempt order by attempt_no').fetchall() == [(1,'failed','provider_connection_error',True,5,1024),(2,'running',None,False,None,4096)]
    b.status, b.provider_entered = 'completed', True
    assert await store.finish_attempt(r, b) == 'running'
    assert await store.finish(r, b, event=('response.completed', {'request_id':r.request_id})) == 'completed'


@pytest.mark.parametrize('state', ['canceling','canceled','completed','failed'])
async def test_late_attempt_finish_preserves_winner(pg_server, persisted, state):
    _, store, name = persisted
    r, a = await admitted(store)
    if state in {'canceling','canceled'}:
        await store.accept_cancel(r.request_id)
        if state == 'canceled': await store.finish_cancel(r,a)
    else:
        await store.finish(r,a,error_code='provider_error' if state=='failed' else None,event=('response.'+state,{'request_id':r.request_id}))
    with pg_server.connect(name) as c: before=c.execute('select * from tb_llm_attempt').fetchall()
    a.status,a.error_code,a.provider_entered='failed','late',True
    assert await store.finish_attempt(r,a)==state
    with pg_server.connect(name) as c: assert c.execute('select * from tb_llm_attempt').fetchall()==before


async def test_duplicate_attempt_end_is_idempotent(pg_server,persisted):
    _,store,name=persisted;r,a=await admitted(store)
    a.status,a.error_code='failed','provider_connection_error'
    await store.finish_attempt(r,a)
    a.error_code='late_wrong';await store.finish_attempt(r,a)
    with pg_server.connect(name) as c: assert c.execute('select error_code from tb_llm_attempt').fetchone()==('provider_connection_error',)


@pytest.mark.parametrize('committed',[False,True])
async def test_attempt_end_real_ack_loss(persisted,monkeypatch,committed):
    db,store,_=persisted;r,a=await admitted(store);a.status,a.error_code='failed','provider_timeout'
    original=AsyncSessionTransaction.__aexit__
    async def lose(tx,*args):
        if committed: await original(tx,*args)
        else: await tx.rollback()
        raise ConnectionError('private acknowledgement lost')
    with monkeypatch.context() as patch:
        patch.setattr(AsyncSessionTransaction,'__aexit__',lose)
        with pytest.raises(StorageUnavailable) as exc: await store.finish_attempt(r,a)
        assert exc.value.uncertain
    row,events=await store.read_events(r.request_id,0)
    assert row['status']=='running' and events==[]
    from sqlalchemy import text
    async with db.transaction() as session:
        assert (await session.execute(text('select status from tb_llm_attempt'))).scalar()==('failed' if committed else 'running')
    assert db.engine.pool.checkedout()==0


async def test_f6_upgrade_preserves_old_columns_and_unknown_evidence(pg_server,tmp_path):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    from uuid import uuid4
    from f4_cases import registry
    from postgres_support import write_config
    from persistence.database import Database,load_database_config
    name=pg_server.create_database()
    try:
        directory=write_config(tmp_path/'old',pg_server,name,registry())
        root=Path(__file__).resolve().parents[2]
        config=Config(str(root/'alembic.ini'))
        config.set_main_option('script_location',str(root/'migrations'))
        config.attributes['config_dir']=directory
        command.upgrade(config,'f6_001')
        rid=str(uuid4())
        with pg_server.connect(name) as c:
            pk=c.execute("insert into tb_llm_request(request_id,operation,status,created_at) values(%s,'completion','completed',now()) returning id",(rid,)).fetchone()[0]
            c.execute("insert into tb_llm_attempt(request_pk,attempt_no,model,provider,adapter,api_protocol,configured_model,status,started_at) values(%s,1,'model','mock','mock','chat_completions','mock','completed',now())",(pk,))
            columns=[r[0] for r in c.execute("select column_name from information_schema.columns where table_name='tb_llm_attempt' order by ordinal_position")]
            before=c.execute('select * from tb_llm_attempt').fetchall()
        db=Database(load_database_config(directory),directory)
        try:
            with pytest.raises(StorageUnavailable):await db.start()
        finally:await db.close()
        command.upgrade(config,'head')
        db=Database(load_database_config(directory),directory)
        try:await db.start()
        finally:await db.close()
        with pg_server.connect(name) as c:
            assert c.execute('select version_num from alembic_version').fetchone()==('f9_001',)
            assert c.execute('select '+','.join(columns)+' from tb_llm_attempt').fetchall()==before
            assert c.execute('select input_snapshot,reason,provider_entered,input_token_count,max_output_tokens from tb_llm_attempt').fetchone()==(None,None,None,None,None)
    finally:pg_server.drop_database(name)
