"""Real PostgreSQL evidence; each database belongs to these fixtures."""
import uuid
import pytest
from persistence.database import REVISION
from postgres_support import PostgresServer


@pytest.fixture(scope='module')
def pg_server():
    server = PostgresServer().start()
    yield server
    server.close()


def test_explicit_migration_creates_three_tables(pg_server, tmp_path):
    from postgres_support import write_config, migrate
    from f4_cases import registry
    name = pg_server.create_database()
    config_dir = write_config(tmp_path, pg_server, name, registry())
    # Once migration code exists this must explicitly migrate, never create_all.
    from pathlib import Path
    if (Path(__file__).resolve().parents[2] / 'alembic.ini').exists():
        migrate(config_dir)
    with pg_server.connect(name) as conn:
        names = {r[0] for r in conn.execute("select tablename from pg_tables where schemaname='public'")}
    assert {'tb_llm_request', 'tb_llm_attempt', 'tb_llm_stream_event'} <= names
    pg_server.drop_database(name)


@pytest.mark.asyncio
async def test_prepare_commits_snapshot(pg_server, tmp_path):
    from postgres_support import migrated_database
    from f4_cases import registry
    from persistence.database import Database, load_database_config
    from persistence.store import Store
    from services.trace import RequestTrace
    from decimal import Decimal
    from utils.json_codec import loads
    with migrated_database(pg_server, tmp_path, registry()) as (name, config_dir):
        db = Database(load_database_config(config_dir), config_dir)
        try:
            await db.start()
            trace = RequestTrace(str(uuid.uuid4()))
            await Store(db).prepare(trace, {'schema': {'minimum': Decimal('1.0000000000000001')}})
            with pg_server.connect(name) as conn:
                row = conn.execute('select input_snapshot from tb_llm_request where request_id=%s', (trace.request_id,)).fetchone()
            assert row is not None
            assert loads(row[0])['schema']['minimum'] == Decimal('1.0000000000000001')
        finally:
            await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('password', ['a:@/?# %特殊字符', ' a:@/?# %特殊字符 '])
async def test_special_password_authenticates_without_url_corruption(pg_server, persisted, password):
    from psycopg import sql
    from sqlalchemy import text
    from persistence.database import Database, load_database_config
    name, config_dir = persisted
    role = 'credential_' + uuid.uuid4().hex[:12]
    with pg_server.connect(autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(role), sql.Literal(password)))
    import psycopg
    with psycopg.connect(host='127.0.0.1', port=pg_server.port, user=role,
                         password=password, dbname=name, connect_timeout=5) as control:
        assert control.execute('SELECT current_user').fetchone() == (role,)
    (config_dir/'special.password').write_text(password+'\n')
    config = load_database_config(config_dir).model_copy(update={'username': role, 'password_file': 'special.password'})
    db = Database(config, config_dir)
    try:
        async with db.transaction() as session:
            assert await session.scalar(text('SELECT current_user')) == role
            assert await session.scalar(text('SELECT current_database()')) == name
    finally:
        await db.close()
        with pg_server.connect(autocommit=True) as conn:
            conn.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


@pytest.fixture
def persisted(pg_server, tmp_path):
    from postgres_support import migrated_database
    from f4_cases import registry
    with migrated_database(pg_server, tmp_path, registry()) as context:
        yield context


def execution(operation='streaming'):
    from services.trace import RequestTrace, AttemptTrace
    trace = RequestTrace(str(uuid.uuid4()), operation=operation, timeout_seconds=30)
    record = AttemptTrace(trace.request_id, 1, 'model', provider='mock', adapter='mock',
                          api_protocol='chat_completions', configured_model='mock-model')
    return trace, record


def storage(config_dir):
    from persistence.database import Database, load_database_config
    from persistence.store import Store
    database = Database(load_database_config(config_dir), config_dir)
    return database, Store(database)


@pytest.mark.asyncio
async def test_events_terminal_usage_and_idempotency(pg_server, persisted):
    from contracts import Usage
    name, config_dir = persisted
    db, store = storage(config_dir)
    trace, record = execution()
    try:
        await store.prepare(trace, {'messages': []})
        await store.begin_attempt(trace, record)
        assert await store.append_event(trace, 'response.delta', {'delta': '订单'}) == 1
        record.usage = Usage(input_tokens=10, output_tokens=3, total_tokens=13)
        assert await store.finish(trace, record, event=('response.completed', {'request_id': trace.request_id})) == 'completed'
        assert await store.finish(trace, record, error_code='client_disconnected', event=('response.failed', {})) == 'completed'
        trace.cleanup_error_code = 'stream_close_error'
        await store.update_diagnostics(trace, record)
        with pg_server.connect(name) as conn:
            row = conn.execute('select status,last_event_seq,created_at,started_at,ended_at,deadline_at from tb_llm_request').fetchone()
            assert row[:2] == ('completed', 2)
            assert all(value.tzinfo is not None for value in row[2:])
            assert conn.execute('select status,input_tokens,output_tokens,total_tokens,cleanup_error_code from tb_llm_attempt').fetchone() == ('completed',10,3,13,'stream_close_error')
            assert conn.execute('select seq,event_type from tb_llm_stream_event order by seq').fetchall() == [(1,'response.delta'), (2,'response.completed')]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_rejected_metadata_has_no_snapshot_or_attempt(pg_server, persisted):
    name, config_dir = persisted
    db, store = storage(config_dir)
    trace, _ = execution()
    try:
        assert await store.finish(trace, error_code='invalid_request') == 'failed'
        with pg_server.connect(name) as conn:
            assert conn.execute('select status,input_snapshot,error_code from tb_llm_request').fetchone() == ('failed',None,'invalid_request')
            assert conn.execute('select count(*) from tb_llm_attempt').fetchone()[0] == 0
            assert conn.execute('select count(*) from tb_llm_stream_event').fetchone()[0] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_request_concurrency_and_atomic_event_rollback(pg_server, persisted):
    import asyncio
    from persistence.database import StorageUnavailable
    name, config_dir = persisted
    db, store = storage(config_dir)
    trace, record = execution()
    other, _ = execution()
    try:
        await store.prepare(trace, {})
        await store.prepare(other, {})
        assert sorted(await asyncio.gather(*[store.append_event(trace,'response.delta',{'delta': str(i)}) for i in range(12)])) == list(range(1,13))
        assert await store.append_event(other, 'response.delta', {'delta':'other'}) == 1
        # The terminal row updates precede event insertion: invalid event must roll all back.
        with pytest.raises(ValueError):
            await store.finish(trace, record, event=('response.failed', {}))
        with pg_server.connect(name) as conn:
            assert conn.execute('select status,last_event_seq from tb_llm_request where request_id=%s',(trace.request_id,)).fetchone() == ('running',12)
        assert await store.append_event(trace, 'response.delta', {'delta':'after rollback'}) == 13
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_recovery_preserves_pending_terminal_and_usage(pg_server, persisted):
    from contracts import Usage
    name, config_dir = persisted
    db, store = storage(config_dir)
    try:
        traces = []
        for status in ['running','canceling','pending','completed','failed','canceled']:
            trace, record = execution()
            traces.append(trace)
            await store.prepare(trace, {})
            await store.begin_attempt(trace, record)
            with pg_server.connect(name) as conn:
                conn.execute('update tb_llm_request set status=%s where request_id=%s',(status,trace.request_id))
                conn.execute('update tb_llm_attempt set input_tokens=10,output_tokens=3,total_tokens=13')
        await store.recover()
        await store.recover()
        with pg_server.connect(name) as conn:
            statuses = [conn.execute('select status from tb_llm_request where request_id=%s',(t.request_id,)).fetchone()[0] for t in traces]
            assert statuses == ['failed','failed','pending','completed','failed','canceled']
            assert conn.execute('select count(*) from tb_llm_stream_event').fetchone()[0] == 2
            assert conn.execute('select count(*) from tb_llm_attempt where total_tokens=13').fetchone()[0] == 6
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reconcile_only_given_stopped_request(pg_server, persisted):
    name, config_dir = persisted
    db, store = storage(config_dir)
    stopped, _ = execution()
    active, _ = execution()
    try:
        await store.prepare(stopped, {})
        await store.prepare(active, {})
        assert await store.reconcile(stopped.request_id) == 'failed'
        with pg_server.connect(name) as conn:
            assert conn.execute('select status from tb_llm_request where request_id=%s',(active.request_id,)).fetchone()[0] == 'running'
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_commit_confirmation_loss_queries_actual_state(pg_server, persisted, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSessionTransaction
    from persistence.database import StorageUnavailable
    name, config_dir = persisted
    db, store = storage(config_dir)
    trace, record = execution()
    try:
        await store.prepare(trace, {})
        await store.begin_attempt(trace, record)
        original = AsyncSessionTransaction.__aexit__
        async def lost_confirmation(self, *args):
            await original(self, *args)
            raise ConnectionError('synthetic secret must not escape')
        with monkeypatch.context() as patch:
            patch.setattr(AsyncSessionTransaction, '__aexit__', lost_confirmation)
            with pytest.raises(StorageUnavailable) as failure:
                await store.finish(trace, record, event=('response.completed', {}))
            assert failure.value.uncertain
            assert 'synthetic secret' not in str(failure.value)
        assert await store.reconcile(trace.request_id) == 'completed'
        with pg_server.connect(name) as conn:
            assert conn.execute('select count(*) from tb_llm_stream_event').fetchone()[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_short_deadline_pool_and_sql_wait_releases_resources(pg_server, persisted):
    import asyncio
    from sqlalchemy import text
    from persistence.database import StorageUnavailable
    name, config_dir = persisted
    db, store = storage(config_dir)
    trace, _ = execution()
    await store.prepare(trace, {})
    connections = [await db.engine.connect() for _ in range(5)]
    try:
        begin = asyncio.get_running_loop().time()
        with pytest.raises(StorageUnavailable) as failure:
            await store.append_event(trace,'response.delta',{},deadline=begin+0.1)
        assert not failure.value.uncertain
        assert asyncio.get_running_loop().time() - begin < 1
    finally:
        for connection in connections:
            await connection.close()
    try:
        begin = asyncio.get_running_loop().time()
        with pytest.raises(StorageUnavailable):
            async with db.transaction(deadline=begin+0.1) as session:
                await session.execute(text('select pg_sleep(2)'))
        assert asyncio.get_running_loop().time() - begin < 1
        assert await store.append_event(trace,'response.delta',{}) == 1
    finally:
        await db.close()


def test_native_identifiers_indexes_constraints_and_repeat_migration(pg_server, persisted):
    import psycopg
    from postgres_support import migrate
    name, config_dir = persisted
    migrate(config_dir)
    with pg_server.connect(name) as conn:
        fields = {row[0]:row[1:] for row in conn.execute("select column_name,data_type,is_identity,identity_generation from information_schema.columns where table_name='tb_llm_request'")}
        assert fields['id'] == ('bigint','YES','ALWAYS')
        assert fields['request_id'][0] == 'uuid'
        keys = conn.execute("select tablename,indexdef from pg_indexes where schemaname='public'").fetchall()
        assert len(keys) == 13  # includes agent pagination, billing indexes and Alembic PK
        assert any('UNIQUE' in ddl and '(request_id)' in ddl for _,ddl in keys)
        foreign_keys = conn.execute("select count(*) from information_schema.table_constraints where constraint_type='FOREIGN KEY' and table_schema='public'").fetchone()[0]
        assert foreign_keys == 8
        request_id = str(uuid.uuid4())
        pk = conn.execute("insert into tb_llm_request(request_id,operation,status,created_at) values (%s,'completion','running',now()) returning id",(request_id,)).fetchone()[0]
        conn.commit()
        for sql, params in [
            ("insert into tb_llm_request(request_id,operation,status,created_at) values (%s,'completion','running',now())",(request_id,)),
            ("update tb_llm_request set status='unknown' where id=%s",(pk,)),
            ("update tb_llm_request set input_token_count=-1 where id=%s",(pk,)),
            ("insert into tb_llm_stream_event values (%s,1,'response.delta','{}',now())",(pk+999,)),
            ("insert into tb_llm_stream_event values (%s,0,'response.delta','{}',now())",(pk,)),
            ("insert into tb_llm_stream_event values (%s,-1,'response.delta','{}',now())",(pk,)),
            ("insert into tb_llm_attempt(request_pk,attempt_no,model,provider,adapter,api_protocol,configured_model,status,started_at) values (%s,1,'m','p','mock','responses','m','running',now())",(pk+999,)),
        ]:
            with pytest.raises(psycopg.IntegrityError):
                conn.execute(sql,params)
            conn.rollback()
        assert conn.execute('select count(*) from tb_llm_request').fetchone()[0] == 1
        assert conn.execute('select version_num from alembic_version').fetchone()[0] == REVISION
    migrate(config_dir)
    with pg_server.connect(name) as conn:
        assert conn.execute('SELECT request_id FROM tb_llm_request').fetchone() == (uuid.UUID(request_id),)


@pytest.mark.asyncio
async def test_start_rejects_unmigrated_or_mismatched_version(pg_server, persisted, tmp_path):
    from persistence.database import StorageUnavailable
    from postgres_support import write_config
    from f4_cases import registry
    name, config_dir = persisted
    with pg_server.connect(name) as conn:
        conn.execute("update alembic_version set version_num='wrong_revision'")
    db, _ = storage(config_dir)
    try:
        with pytest.raises(StorageUnavailable):
            await db.start()
    finally:
        await db.close()
    empty = pg_server.create_database()
    empty_dir = write_config(tmp_path/'empty',pg_server,empty,registry())
    db, _ = storage(empty_dir)
    try:
        with pytest.raises(StorageUnavailable):
            await db.start()
        with pg_server.connect(empty) as conn:
            assert conn.execute("select count(*) from pg_tables where schemaname='public'").fetchone()[0] == 0
    finally:
        await db.close()
        pg_server.drop_database(empty)


@pytest.mark.asyncio
async def test_programming_errors_are_not_storage_failures(persisted):
    _, config_dir = persisted
    db, _ = storage(config_dir)
    try:
        with pytest.raises(ValueError, match='programming mistake'):
            async with db.transaction():
                raise ValueError('programming mistake')
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_actual_commit_timeout_is_uncertain_and_reconciled(pg_server, persisted):
    import asyncio
    from persistence.database import StorageUnavailable
    name, config_dir = persisted
    db, store = storage(config_dir)
    trace, record = execution()
    try:
        await store.prepare(trace, {})
        await store.begin_attempt(trace, record)
        with pg_server.connect(name) as conn:
            conn.execute('''CREATE FUNCTION test_slow_commit() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN PERFORM pg_sleep(2); RETURN NEW; END $$''')
            conn.execute('''CREATE CONSTRAINT TRIGGER test_commit_delay AFTER UPDATE ON tb_llm_request
                DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION test_slow_commit()''')
        begin = asyncio.get_running_loop().time()
        with pytest.raises(StorageUnavailable) as failure:
            await store.finish(trace, record, event=('response.completed', {}),deadline=begin+0.1)
        assert failure.value.uncertain
        assert asyncio.get_running_loop().time()-begin < 1
        with pg_server.connect(name) as conn:
            conn.execute('DROP TRIGGER test_commit_delay ON tb_llm_request')
        final = await store.reconcile(trace.request_id)
        assert final in {'completed','failed'}  # PostgreSQL truth, never infer commit from timeout.
        with pg_server.connect(name) as conn:
            status, count = conn.execute('select status,last_event_seq from tb_llm_request').fetchone()
            assert status == final
            assert count == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_default_pool_wait_budget_is_five_seconds(persisted):
    import asyncio
    from persistence.database import StorageUnavailable
    _, config_dir = persisted
    db, store = storage(config_dir)
    trace, _ = execution()
    await store.prepare(trace,{})
    connections = [await db.engine.connect() for _ in range(5)]
    try:
        begin = asyncio.get_running_loop().time()
        with pytest.raises(StorageUnavailable):
            await store.append_event(trace,'response.delta',{})
        elapsed = asyncio.get_running_loop().time()-begin
        assert 4.8 <= elapsed <= 6
        assert db.engine.pool.size() == 5
        assert db.engine.pool.overflow() == 0
    finally:
        for connection in connections:
            await connection.close()
        await db.close()


@pytest.mark.asyncio
async def test_commit_rollback_confirmation_loss_reconciles_failed(pg_server, persisted, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSessionTransaction
    from persistence.database import StorageUnavailable
    name, config_dir = persisted
    db, store = storage(config_dir)
    trace, record = execution()
    try:
        await store.prepare(trace,{})
        await store.begin_attempt(trace,record)
        async def rollback_then_lose_confirmation(self, *args):
            await self.rollback()
            raise ConnectionError('private synthetic failure')
        with monkeypatch.context() as patch:
            patch.setattr(AsyncSessionTransaction,'__aexit__',rollback_then_lose_confirmation)
            with pytest.raises(StorageUnavailable) as failure:
                await store.finish(trace,record,event=('response.completed',{}))
            assert failure.value.uncertain
        assert await store.reconcile(trace.request_id) == 'failed'
        with pg_server.connect(name) as conn:
            assert conn.execute('select status,error_code from tb_llm_request').fetchone() == ('failed','execution_interrupted')
            assert conn.execute('select event_type from tb_llm_stream_event').fetchall() == [('response.failed',)]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_duplicate_attempt_event_and_invalid_usage_roll_back(pg_server, persisted):
    import psycopg
    from persistence.database import StorageUnavailable
    name, config_dir = persisted
    db, store = storage(config_dir)
    trace, record = execution()
    try:
        await store.prepare(trace,{})
        await store.begin_attempt(trace,record)
        with pytest.raises(StorageUnavailable):
            await store.begin_attempt(trace,record)
        await store.append_event(trace,'response.delta',{})
        with pg_server.connect(name) as conn:
            pk = conn.execute('select id from tb_llm_request').fetchone()[0]
            for sql,params in [
                ("insert into tb_llm_stream_event values (%s,1,'response.delta','{}',now())",(pk,)),
                ('update tb_llm_attempt set attempt_no=0',()),
                ('update tb_llm_attempt set attempt_no=-1',()),
                ('update tb_llm_attempt set input_tokens=-1,output_tokens=1,total_tokens=0',()),
                ('update tb_llm_attempt set input_tokens=1,output_tokens=1,total_tokens=3',()),
                ('update tb_llm_attempt set input_tokens=1',()),
            ]:
                with pytest.raises(psycopg.IntegrityError):
                    conn.execute(sql,params)
                conn.rollback()
            assert conn.execute('select input_tokens,output_tokens,total_tokens from tb_llm_attempt').fetchone() == (None,None,None)
            assert conn.execute('select count(*) from tb_llm_attempt').fetchone()[0] == 1
            assert conn.execute('select last_event_seq from tb_llm_request').fetchone()[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_post_terminal_diagnostics_preserve_execution_truth(pg_server, persisted):
    from contracts import Usage
    name, config_dir = persisted
    db, store = storage(config_dir)
    trace, record = execution()
    try:
        await store.prepare(trace,{})
        await store.begin_attempt(trace,record)
        record.usage = Usage(input_tokens=10,output_tokens=3,total_tokens=13)
        await store.finish(trace,record,event=('response.completed',{}))
        record.latency_ms = 125.5
        record.status = 'failed'
        record.error_code = 'must_not_replace_execution'
        record.usage = None
        trace.cleanup_error_code = 'stream_close_error'
        await store.update_diagnostics(trace,record)
        with pg_server.connect(name) as conn:
            assert conn.execute('select status,error_code,input_tokens,output_tokens,total_tokens,latency_ms,cleanup_error_code from tb_llm_attempt').fetchone() == ('completed',None,10,3,13,125.5,'stream_close_error')
            assert conn.execute('select status,last_event_seq from tb_llm_request').fetchone() == ('completed',1)
            assert conn.execute('select event_type from tb_llm_stream_event').fetchall() == [('response.completed',)]
        record.latency_ms = None
        await store.update_diagnostics(trace,record)
        with pg_server.connect(name) as conn:
            assert conn.execute('select latency_ms from tb_llm_attempt').fetchone()[0] == 125.5
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_prepare_records_actual_remaining_deadline_as_utc(pg_server, persisted):
    import asyncio
    from datetime import datetime, timezone, timedelta
    name, config_dir = persisted
    db, store = storage(config_dir)
    trace, _ = execution()
    try:
        before = datetime.now(timezone.utc)
        await store.prepare(trace,{},deadline=asyncio.get_running_loop().time()+0.3)
        with pg_server.connect(name) as conn:
            created, deadline = conn.execute('select created_at,deadline_at from tb_llm_request').fetchone()
            assert 0 < (deadline-created).total_seconds() <= 0.3
            assert abs((deadline-(before+timedelta(seconds=0.3))).total_seconds()) < 0.1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_uncommitted_prepare_is_invisible_to_independent_connection(pg_server, persisted, monkeypatch):
    import asyncio
    from sqlalchemy.ext.asyncio import AsyncSessionTransaction
    name, config_dir = persisted
    db, store = storage(config_dir)
    trace, _ = execution()
    original = AsyncSessionTransaction.__aexit__
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause_before_commit(self, *args):
        entered.set()
        await release.wait()
        return await original(self, *args)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(AsyncSessionTransaction, '__aexit__', pause_before_commit)
            task = asyncio.create_task(store.prepare(trace, {'messages': []}))
            await asyncio.wait_for(entered.wait(), 3)
            with pg_server.connect(name) as conn:
                assert conn.execute('SELECT count(*) FROM tb_llm_request').fetchone() == (0,)
            release.set()
            await task
        with pg_server.connect(name) as conn:
            assert conn.execute('SELECT count(*) FROM tb_llm_request').fetchone() == (1,)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_competing_terminal_transactions_keep_one_winner(pg_server, persisted):
    import asyncio
    name, config_dir = persisted
    db, store = storage(config_dir)
    trace, record = execution()
    _, contender = execution()
    contender.request_id = trace.request_id
    contender.error_code = 'provider_auth_error'
    try:
        await store.prepare(trace, {})
        await store.begin_attempt(trace, record)
        results = await asyncio.gather(
            store.finish(trace, record, event=('response.completed', {})),
            store.finish(trace, contender, error_code='provider_auth_error', event=('response.failed', {})))
        assert len(set(results)) == 1
        winner = results[0]
        with pg_server.connect(name) as conn:
            assert conn.execute('SELECT status FROM tb_llm_request').fetchone() == (winner,)
            assert conn.execute('SELECT status FROM tb_llm_attempt').fetchone() == (winner,)
            assert conn.execute('SELECT event_type,seq FROM tb_llm_stream_event').fetchall() == [('response.' + winner, 1)]
        assert await store.finish(trace, record, event=('response.completed', {})) == winner
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('table', ['tb_llm_request', 'tb_llm_attempt', 'tb_llm_stream_event'])
async def test_sql_fault_rolls_back_all_terminal_records(pg_server, persisted, table):
    from persistence.database import StorageUnavailable
    name, config_dir = persisted
    db, store = storage(config_dir)
    trace, record = execution()
    try:
        await store.prepare(trace, {})
        await store.begin_attempt(trace, record)
        with pg_server.connect(name) as conn:
            conn.execute('''CREATE FUNCTION test_fail_write() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'controlled write failure'; END $$''')
            # Identifiers come only from the fixed parameter list, never caller input.
            from psycopg import sql
            conn.execute(sql.SQL('CREATE TRIGGER test_fail BEFORE INSERT OR UPDATE ON {} '
                                 'FOR EACH ROW EXECUTE FUNCTION test_fail_write()').format(sql.Identifier(table)))
        with pytest.raises(StorageUnavailable):
            await store.finish(trace, record, event=('response.completed', {}))
        with pg_server.connect(name) as conn:
            assert conn.execute('SELECT status,last_event_seq FROM tb_llm_request').fetchone() == ('running', 0)
            assert conn.execute('SELECT status FROM tb_llm_attempt').fetchone() == ('running',)
            assert conn.execute('SELECT count(*) FROM tb_llm_stream_event').fetchone() == (0,)
            conn.execute(sql.SQL('DROP TRIGGER test_fail ON {}').format(sql.Identifier(table)))
        record.error_code = 'storage_unavailable'
        assert await store.finish(trace, record, error_code=record.error_code,
                                  event=('response.failed', {})) == 'failed'
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['invalid_request','argument','compile'])
async def test_sqlalchemy_programming_errors_propagate(persisted, kind):
    from sqlalchemy.exc import InvalidRequestError, ArgumentError, CompileError
    _, config_dir = persisted
    db, _ = storage(config_dir)
    error_type = {'invalid_request':InvalidRequestError,'argument':ArgumentError,'compile':CompileError}[kind]
    try:
        with pytest.raises(error_type):
            async with db.transaction():
                raise error_type('programming mistake')
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_external_cancel_before_commit_propagates(persisted):
    import asyncio
    _, config_dir = persisted
    db, _ = storage(config_dir)
    entered = asyncio.Event()
    async def operation():
        async with db.transaction():
            entered.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(operation())
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_external_cancel_during_commit_preserves_database_truth(pg_server, persisted, monkeypatch):
    import asyncio
    from sqlalchemy.ext.asyncio import AsyncSessionTransaction
    name, config_dir = persisted
    db, store = storage(config_dir)
    trace, record = execution()
    committed = asyncio.Event()
    original = AsyncSessionTransaction.__aexit__
    async def committed_waiting_confirmation(self, *args):
        await original(self,*args)
        committed.set()
        await asyncio.Event().wait()
    try:
        await store.prepare(trace,{})
        await store.begin_attempt(trace,record)
        with monkeypatch.context() as patch:
            patch.setattr(AsyncSessionTransaction,'__aexit__',committed_waiting_confirmation)
            task = asyncio.create_task(store.finish(trace,record,event=('response.completed',{})))
            await committed.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert await store.reconcile(trace.request_id) == 'completed'
        with pg_server.connect(name) as conn:
            assert conn.execute('select status,last_event_seq from tb_llm_request').fetchone() == ('completed',1)
            assert conn.execute('select event_type from tb_llm_stream_event').fetchall() == [('response.completed',)]
    finally:
        await db.close()
