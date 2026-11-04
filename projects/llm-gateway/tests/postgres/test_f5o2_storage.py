"""F5-O2 real PostgreSQL creation, atomic claims, and bounded replay."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from f4_cases import registry
from persistence.database import Database, StorageUnavailable, load_database_config
from persistence.store import Store
from postgres_support import PostgresServer, migrated_database
from services.trace import AttemptTrace, RequestTrace
from utils.json_codec import loads


@pytest.fixture(scope='module')
def pg_server():
    server = PostgresServer().start()
    yield server
    server.close()


@pytest.fixture
async def persisted(pg_server, tmp_path):
    with migrated_database(pg_server, tmp_path, registry()) as (name, directory):
        database = Database(load_database_config(directory), directory)
        try:
            await database.start()
            yield database, Store(database), name
        finally:
            await database.close()


def trace():
    return RequestTrace(str(uuid4()), operation='streaming', timeout_seconds=30)


async def claim(store, request_id):
    started = datetime.now(timezone.utc)
    return await store.claim_stream(request_id, started, started + timedelta(seconds=30),
                                    deadline=asyncio.get_running_loop().time() + 30)


async def test_creation_committed_pending_without_generation(pg_server, persisted):
    database, store, name = persisted
    record = trace()
    snapshot = {'messages': [{'role': 'user', 'content': '订单 A123'}], 'timeout_seconds': 30}
    await store.create_pending(record, snapshot)
    row = await store.get_stream(record.request_id)
    assert row['status'] == 'pending' and row['last_event_seq'] == 0
    assert row['started_at'] is row['deadline_at'] is row['ended_at'] is None
    assert loads(row['input_snapshot']) == snapshot
    with pg_server.connect(name) as conn:
        assert conn.execute('select status from tb_llm_request where request_id=%s', (record.request_id,)).fetchone() == ('pending',)
        assert conn.execute('select count(*) from tb_llm_attempt').fetchone() == (0,)
        assert conn.execute('select count(*) from tb_llm_stream_event').fetchone() == (0,)
    assert database.engine.pool.checkedout() == 0


async def test_twenty_claims_have_one_winner_and_preserve_claim_timestamp(pg_server, persisted):
    _, store, name = persisted
    record = trace()
    await store.create_pending(record, {})
    started = datetime.now(timezone.utc) - timedelta(milliseconds=100)
    end = started + timedelta(seconds=30)
    winners = await asyncio.gather(*(store.claim_stream(record.request_id, started, end,
        deadline=asyncio.get_running_loop().time() + 30) for _ in range(20)))
    assert winners.count(True) == 1 and winners.count(False) == 19
    attempt = AttemptTrace(record.request_id, 1, 'general-primary', provider='mock', adapter='mock',
                           configured_model='mock', api_protocol='chat_completions')
    await store.begin_attempt(record, attempt)
    with pg_server.connect(name) as conn:
        assert conn.execute('select status,started_at,deadline_at from tb_llm_request').fetchone() == ('running', started, end)
        assert conn.execute('select count(*) from tb_llm_attempt').fetchone() == (1,)
    assert not await claim(store, record.request_id)


async def test_pagination_205_deltas_plus_terminal_is_ordered_bounded_and_releases_pool(persisted):
    database, store, _ = persisted
    record = trace()
    await store.create_pending(record, {})
    assert await claim(store, record.request_id)
    for number in range(205):
        assert await store.append_event(record, 'response.delta', {'request_id': record.request_id, 'delta': f'订单 {number}\n'}) == number + 1
    await store.finish(record, event=('response.completed', {'request_id': record.request_id, 'usage': None}))
    cursor, observed, sizes = 0, [], []
    while True:
        row, events = await store.read_events(record.request_id, cursor)
        assert row['status'] == 'completed'
        assert database.engine.pool.checkedout() == 0
        if not events:
            break
        sizes.append(len(events))
        observed.extend(events)
        cursor = events[-1]['seq']
    assert sizes == [100, 100, 6]
    assert [event['seq'] for event in observed] == list(range(1, 207))
    assert all(loads(event['payload'])['delta'] == f'订单 {number}\n' for number, event in enumerate(observed[:-1]))
    assert observed[-1]['event_type'] == 'response.completed'
    assert (await store.read_events(record.request_id, 205))[1][0]['seq'] == 206
    assert not await claim(store, record.request_id)


async def test_absent_ordinary_and_rejected_are_not_subscribable(persisted):
    _, store, _ = persisted
    absent = str(uuid4())
    ordinary = RequestTrace(str(uuid4()))
    rejected = trace()
    await store.prepare(ordinary, {})
    await store.finish(rejected, error_code='invalid_request')
    for request_id in [absent, ordinary.request_id, rejected.request_id]:
        assert await store.get_stream(request_id) is None
        assert await store.read_events(request_id, 0) == (None, [])
        assert not await claim(store, request_id)


@pytest.mark.parametrize('stage', ['creation', 'claim'])
@pytest.mark.parametrize('committed', [False, True])
async def test_creation_and_claim_commit_ack_loss_never_guesses_state(persisted, monkeypatch, stage, committed):
    from sqlalchemy.ext.asyncio import AsyncSessionTransaction
    database, store, _ = persisted
    record = trace()
    if stage == 'claim':
        await store.create_pending(record, {})
    original = AsyncSessionTransaction.__aexit__

    async def lose_confirmation(transaction, *args):
        if committed:
            await original(transaction, *args)
        else:
            await transaction.rollback()
        raise ConnectionError('synthetic private exception')

    with monkeypatch.context() as scoped:
        scoped.setattr(AsyncSessionTransaction, '__aexit__', lose_confirmation)
        with pytest.raises(StorageUnavailable) as error:
            if stage == 'creation':
                await store.create_pending(record, {})
            else:
                await claim(store, record.request_id)
        assert error.value.uncertain is True
    row = await store.get_stream(record.request_id)
    if stage == 'creation':
        assert (row['status'] if row else None) == ('pending' if committed else None)
    else:
        assert row['status'] == ('running' if committed else 'pending')
    assert database.engine.pool.checkedout() == 0


async def test_claim_rollback_and_recover_keep_unclaimed_pending(pg_server, persisted):
    database, store, name = persisted
    record = trace()
    await store.create_pending(record, {})
    with pg_server.connect(name) as conn:
        conn.execute("""CREATE FUNCTION reject_claim() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF OLD.status='pending' AND NEW.status='running' THEN
                RAISE EXCEPTION 'synthetic claim failure'; END IF; RETURN NEW; END $$""")
        conn.execute('CREATE TRIGGER reject_claim BEFORE UPDATE ON tb_llm_request FOR EACH ROW EXECUTE FUNCTION reject_claim()')
    with pytest.raises(StorageUnavailable) as error:
        await claim(store, record.request_id)
    assert not error.value.uncertain
    with pg_server.connect(name) as conn:
        conn.execute('DROP TRIGGER reject_claim ON tb_llm_request')
    await store.recover()
    row, events = await store.read_events(record.request_id, 0)
    assert row['status'] == 'pending' and row['started_at'] is row['deadline_at'] is None
    assert events == [] and database.engine.pool.checkedout() == 0
