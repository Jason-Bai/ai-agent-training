"""F5-O2 subscriber isolation and bounded delivery; no database/Provider substitutes for integration."""
import asyncio
import importlib.util
from time import monotonic

import pytest

from utils.json_codec import dumps


def subscription(store, **kwargs):
    assert importlib.util.find_spec('services.subscriptions'), 'F5-O2 read-only subscription service is absent'
    from services.subscriptions import EventSubscription
    return EventSubscription(store, 'request-one', 0, **kwargs)


def event(seq, name='response.delta', **data):
    return {'seq': seq, 'event_type': name, 'payload': dumps({'request_id': 'request-one', **data})}


class Store:
    def __init__(self, events=(), status='completed'):
        self.events = list(events)
        self.status = status
        self.calls = []
        self.query_gate = None
        self.queries = 0

    async def read_events(self, request_id, after_seq, limit):
        self.calls.append((request_id, after_seq, limit))
        self.queries += 1
        if self.query_gate:
            await self.query_gate.wait()
        return ({'status': self.status, 'last_event_seq': len(self.events)},
                [e for e in self.events if e['seq'] > after_seq][:limit])


async def execute(subject, *, emit=None, start=None, disconnected=None):
    sent = []
    async def headers():
        sent.append('headers')
    async def send(name, payload, seq=None):
        sent.append((name, payload, seq))
    await asyncio.wait_for(subject.run(start or headers, emit or send,
                                      disconnected or asyncio.Event()), 2)
    return sent


async def test_paginates_205_deltas_and_terminal_without_empty_poll_delay():
    store = Store([event(i, delta=str(i)) for i in range(1, 206)] + [event(206, 'response.completed', usage=None)])
    subject = subscription(store, poll_seconds=10)
    sent = await execute(subject)
    assert sent[0] == 'headers'
    assert [frame[2] for frame in sent[1:]] == list(range(1, 207))
    assert [(c[1], c[2]) for c in store.calls] == [(0, 100), (100, 100), (200, 100)]
    assert sent[-1][0] == 'response.completed'


async def test_resumes_strictly_after_cursor_and_preserves_unicode_whitespace():
    store = Store([event(1, delta='old'), event(2, delta=' 中文 \n'), event(3, 'response.failed', code='provider_timeout')])
    subject = subscription(store)
    subject.after_seq = 1
    sent = await execute(subject)
    assert [x[2] for x in sent[1:]] == [2, 3]
    assert sent[1][1]['delta'] == ' 中文 \n'


async def test_terminal_at_cursor_returns_without_waiting():
    subject = subscription(Store())
    assert await execute(subject) == ['headers']


async def test_empty_running_polls_and_heartbeats_without_seq_until_terminal():
    store = Store(status='running')
    subject = subscription(store, poll_seconds=.003, heartbeat_seconds=.012)
    async def send(name, payload, seq=None):
        frames.append((name, payload, seq))
        if name == 'response.heartbeat':
            store.events.append(event(1, 'response.completed', usage=None))
            store.status = 'completed'
    frames = []
    await execute(subject, emit=send)
    assert len(store.calls) >= 2
    assert frames == [('response.heartbeat', {'request_id': 'request-one'}, None),
                      ('response.completed', {'request_id': 'request-one', 'usage': None}, 1)]


async def test_heartbeat_during_slow_query_does_not_start_second_query():
    store = Store([event(1, 'response.completed', usage=None)])
    store.query_gate = asyncio.Event()
    subject = subscription(store, heartbeat_seconds=.01)
    frames = []
    async def send(name, payload, seq=None):
        frames.append(name)
        assert store.queries == 1
        if name == 'response.heartbeat':
            store.query_gate.set()
    await execute(subject, emit=send)
    assert frames == ['response.heartbeat', 'response.completed']


@pytest.mark.parametrize('position', ['headers', 'delta', 'heartbeat', 'terminal'])
async def test_each_send_has_its_own_deadline(position):
    name = 'response.completed' if position == 'terminal' else 'response.delta'
    store = Store([] if position == 'heartbeat' else [event(1, name, delta='text')],
                  'running' if position == 'heartbeat' else 'completed')
    subject = subscription(store, heartbeat_seconds=.005, send_timeout_seconds=.015)
    never = asyncio.Event()
    async def start():
        if position == 'headers':
            await never.wait()
    async def send(name, payload, seq=None):
        if ('completed' if position == 'terminal' else position) == name.removeprefix('response.'):
            await never.wait()
    await execute(subject, emit=send, start=start)
    assert subject.stopped_reason == 'send_timeout'
    assert store.status in {'running', 'completed'}


async def test_total_subscription_can_exceed_individual_send_timeout():
    store = Store([event(i, delta=str(i)) for i in range(1, 5)] + [event(5, 'response.completed')])
    subject = subscription(store, send_timeout_seconds=.03)
    frames = []
    async def send(name, payload, seq=None):
        await asyncio.sleep(.012)
        frames.append(seq)
    began = monotonic()
    await execute(subject, emit=send)
    assert monotonic() - began > .03
    assert frames == [1, 2, 3, 4, 5]


async def test_disconnect_cancels_only_own_slow_query():
    store = Store(status='running')
    store.query_gate = asyncio.Event()
    disconnected = asyncio.Event()
    subject = subscription(store)
    task = asyncio.create_task(execute(subject, disconnected=disconnected))
    while not store.calls:
        await asyncio.sleep(0)
    disconnected.set()
    await task
    assert subject.stopped_reason == 'client_disconnected'
    assert store.status == 'running'
    assert not store.query_gate.is_set()


async def test_query_failure_does_not_emit_failed_or_change_generation(caplog):
    class FailedStore(Store):
        async def read_events(self, *args, **kwargs):
            raise OSError('PRIVATE DATABASE PASSWORD')
    store = FailedStore(status='running')
    subject = subscription(store)
    assert await execute(subject) == ['headers']
    assert store.status == 'running'
    assert 'PRIVATE' not in caplog.text


async def test_first_text_callback_once_only_after_successful_send():
    store = Store([event(1, delta='a'), event(2, delta='b'), event(3, 'response.completed')])
    observed = []
    subject = subscription(store, on_first_text=observed.append)
    await execute(subject)
    assert len(observed) == 1
    assert isinstance(observed[0], float)


async def test_failed_text_send_never_records_ttft():
    store = Store([event(1, delta='a'), event(2, 'response.completed')])
    observed = []
    subject = subscription(store, on_first_text=observed.append)
    async def send(*args, **kwargs):
        raise OSError('socket closed')
    await execute(subject, emit=send)
    assert observed == []
    assert store.status == 'completed'


async def test_slow_subscriber_does_not_block_another():
    store = Store([event(1, delta='a'), event(2, 'response.completed')])
    slow = subscription(store, send_timeout_seconds=.03)
    fast = subscription(store)
    async def block(*args, **kwargs):
        await asyncio.Event().wait()
    slow_task = asyncio.create_task(execute(slow, emit=block))
    result = await execute(fast)
    assert result[-1][0] == 'response.completed'
    await slow_task
    assert slow.stopped_reason == 'send_timeout'


async def test_outer_cancel_collects_own_tasks():
    store = Store(status='running')
    store.query_gate = asyncio.Event()
    subject = subscription(store)
    task = asyncio.create_task(execute(subject))
    while not store.calls:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.status == 'running'


async def test_storage_unavailable_has_separate_diagnostic_and_no_model_failure(caplog):
    from persistence.database import StorageUnavailable
    class Unavailable(Store):
        async def read_events(self, *args, **kwargs):
            raise StorageUnavailable()
    store = Unavailable(status='running')
    subject = subscription(store)
    assert await execute(subject) == ['headers']
    assert subject.stopped_reason == 'storage_unavailable'
    assert store.status == 'running'
    assert 'storage_unavailable' in caplog.text


async def test_pending_subscription_does_not_create_an_execution():
    store = Store(status='pending')
    disconnected = asyncio.Event()
    subject = subscription(store, heartbeat_seconds=.005)
    async def send(name, payload, seq=None):
        assert name == 'response.heartbeat'
        disconnected.set()
    await execute(subject, emit=send, disconnected=disconnected)
    assert store.status == 'pending'
    assert store.events == []


async def test_disconnect_collects_inflight_send():
    store = Store([event(1, delta='a'), event(2, 'response.completed')])
    disconnected, entered, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    subject = subscription(store)
    async def send(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    task = asyncio.create_task(execute(subject, emit=send, disconnected=disconnected))
    await entered.wait()
    disconnected.set()
    await task
    assert closed.is_set()
    assert subject.after_seq == 0
    assert store.status == 'completed'


async def test_two_subscribers_use_their_own_idle_clocks(monkeypatch):
    import services.subscriptions as module
    clock=[0.0]
    monkeypatch.setattr(module,'monotonic',lambda:clock[0])
    class EmptyStore:
        async def read_events(self,*args,**kwargs):
            await asyncio.sleep(0)
            return {'status':'running','last_event_seq':0},[]
    sent=[[],[]];started=[asyncio.Event(),asyncio.Event()];closed=[asyncio.Event(),asyncio.Event()]
    async def start(i):started[i].set()
    async def emit(i,name,payload,seq=None):
        assert name=='response.heartbeat' and seq is None
        sent[i].append(clock[0])
    async def until(predicate):
        async with asyncio.timeout(.3):
            while not predicate():await asyncio.sleep(.001)
    tasks=[]
    try:
        for i,when in [(0,0),(1,5)]:
            clock[0]=when
            sub=module.EventSubscription(EmptyStore(),'clock-test',0,poll_seconds=.001)
            tasks.append(asyncio.create_task(sub.run(lambda i=i:start(i),lambda *a,i=i,**k:emit(i,*a,**k),closed[i])))
            await started[i].wait();await asyncio.sleep(.002)
        clock[0]=15;await until(lambda:len(sent[0])==1)
        assert sent==[[15],[]]
        clock[0]=20;await until(lambda:len(sent[1])==1)
        assert sent==[[15],[20]]
        clock[0]=30;await until(lambda:len(sent[0])==2)
        clock[0]=35;await until(lambda:len(sent[1])==2)
        assert sent==[[15,30],[20,35]]
    finally:
        for event in closed:event.set()
        await asyncio.gather(*tasks)
