from types import SimpleNamespace

import pytest

from errors import GatewayError
from services.concurrency import ConcurrencyLimiter


def limiter(total=2, per_agent=1):
    return ConcurrencyLimiter(SimpleNamespace(max_requests=total, max_requests_per_agent=per_agent))


def test_global_limit_rejects_next_request():
    subject = limiter()
    subject.acquire('a', 'request-a')
    subject.acquire('b', 'request-b')
    with pytest.raises(GatewayError) as rejected:
        subject.acquire('c', 'request-c')
    assert rejected.value.code == 'concurrency_limited'
    assert rejected.value.status_code == 429


def test_agent_rejection_leaves_other_agent_and_global_capacity_unchanged(caplog):
    subject = limiter(3, 1)
    subject.acquire('a', 'request-a')
    before = subject.snapshot()
    with pytest.raises(GatewayError):
        subject.acquire('a', 'request-a2')
    assert subject.snapshot() == before == {'active_requests': 1, 'agents': {'a': 1}}
    subject.acquire('b', 'request-b')
    subject.acquire('c', 'request-c')
    assert 'scope=agent' in caplog.text
    assert 'request_id=request-a2' in caplog.text
    assert 'limit=1 in_use=1' in caplog.text


def test_global_rejection_is_reported_before_agent_rejection(caplog):
    subject = limiter(1, 1)
    subject.acquire('a', 'request-a')
    with pytest.raises(GatewayError):
        subject.acquire('a', 'request-b')
    assert 'scope=global' in caplog.text
    assert 'scope=agent' not in caplog.text


def test_release_returns_both_counts_once_and_never_releases_another_request():
    subject = limiter(3, 2)
    first = subject.acquire('a', 'request-a')
    second = subject.acquire('a', 'request-b')
    third = subject.acquire('b', 'request-c')
    assert subject.snapshot() == {'active_requests': 3, 'agents': {'a': 2, 'b': 1}}
    first.release()
    first.release()
    assert subject.snapshot() == {'active_requests': 2, 'agents': {'a': 1, 'b': 1}}
    replacement = subject.acquire('a', 'request-d')
    second.release()
    third.release()
    replacement.release()
    assert subject.snapshot() == {'active_requests': 0, 'agents': {}}


@pytest.mark.parametrize('agent', ['a', 'b'])
def test_duplicate_request_cannot_replace_its_owner(agent):
    subject = limiter(3, 2)
    permit = subject.acquire('a', 'request-a')
    with pytest.raises(RuntimeError):
        subject.acquire(agent, 'request-a')
    assert subject.snapshot() == {'active_requests': 1, 'agents': {'a': 1}}
    permit.release()
    assert subject.snapshot() == {'active_requests': 0, 'agents': {}}


def test_snapshot_is_a_copy_and_policy_is_fixed_at_creation():
    policy = SimpleNamespace(max_requests=1, max_requests_per_agent=1)
    subject = ConcurrencyLimiter(policy)
    subject.acquire('a', 'request-a')
    snapshot = subject.snapshot()
    snapshot['agents'].clear()
    snapshot['active_requests'] = 0
    policy.max_requests = policy.max_requests_per_agent = 3
    assert subject.snapshot() == {'active_requests': 1, 'agents': {'a': 1}}
    with pytest.raises(GatewayError):
        subject.acquire('b', 'request-b')


@pytest.mark.parametrize('scope', ['global', 'agent'])
async def test_simultaneous_contenders_never_exceed_last_slot(scope):
    import asyncio
    for iteration in range(10):
        subject = limiter(2 if scope == 'global' else 100, 2)
        first = subject.acquire('a', 'request-first')
        go = asyncio.Event()
        permits = []
        rejected = []
        snapshots = []

        async def contender(index):
            await go.wait()
            try:
                permits.append(subject.acquire('a' if scope == 'agent' else str(index), str(index)))
            except GatewayError as error:
                rejected.append(error.code)
            snapshots.append(subject.snapshot())

        tasks = [asyncio.create_task(contender(i)) for i in range(32)]
        go.set()
        await asyncio.gather(*tasks)
        assert len(permits) == 1
        assert rejected == ['concurrency_limited'] * 31
        assert max(s['active_requests'] for s in snapshots) == 2
        for permit in [first, *permits]:
            permit.release()
        assert subject.snapshot() == {'active_requests': 0, 'agents': {}}


async def test_deferred_release_waits_for_every_task_after_cancellation():
    import asyncio
    subject = limiter(1, 1)
    permit = subject.acquire('a', 'request-a')
    cleaning = asyncio.Event()
    finish = asyncio.Event()

    async def stubborn_cleanup():
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cleaning.set()
            await finish.wait()

    cleanup = asyncio.create_task(stubborn_cleanup())
    await asyncio.sleep(0)
    cleanup.cancel()
    await cleaning.wait()
    other = asyncio.create_task(finish.wait())
    permit.release_when_done([cleanup, other, cleanup])
    with pytest.raises(RuntimeError):
        permit.release()
    assert subject.snapshot() == {'active_requests': 1, 'agents': {'a': 1}}
    with pytest.raises(GatewayError):
        subject.acquire('b', 'request-b')
    finish.set()
    await asyncio.gather(cleanup, other)
    await asyncio.sleep(0)
    assert subject.snapshot() == {'active_requests': 0, 'agents': {}}
    permit.release()
    replacement = subject.acquire('b', 'request-b')
    replacement.release()


async def test_deferred_release_handles_finished_cancelled_and_failed_tasks():
    import asyncio
    subject = limiter(2, 1)
    permit = subject.acquire('a', 'request-a')

    async def fail():
        raise ValueError('cleanup failed')

    failed = asyncio.create_task(fail())
    cancelled = asyncio.create_task(asyncio.sleep(100))
    cancelled.cancel()
    with pytest.raises(ValueError, match='cleanup failed'):
        await failed
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    permit.release_when_done([failed, cancelled])
    assert subject.snapshot() == {'active_requests': 0, 'agents': {}}
    permit.release_when_done([])


async def test_deferred_release_can_extend_the_set_before_all_tasks_stop():
    import asyncio
    subject = limiter(1, 1)
    permit = subject.acquire('a', 'request-a')
    first, second = asyncio.Event(), asyncio.Event()
    a = asyncio.create_task(first.wait())
    b = asyncio.create_task(second.wait())
    permit.release_when_done([a])
    permit.release_when_done([a, b])
    first.set()
    await a
    await asyncio.sleep(0)
    assert subject.snapshot()['active_requests'] == 1
    second.set()
    await b
    await asyncio.sleep(0)
    assert subject.snapshot() == {'active_requests': 0, 'agents': {}}


async def test_cleanup_callback_observes_failure_without_changing_task_outcome():
    import asyncio
    observed = []

    class ObservedTask(asyncio.Task):
        def exception(self):
            observed.append(True)
            return super().exception()

    async def fail():
        raise ValueError('cleanup failed')

    subject = limiter(1, 1)
    permit = subject.acquire('a', 'request-a')
    cleanup = ObservedTask(fail())
    permit.release_when_done([cleanup])
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert observed, 'a failed detached cleanup task must have its exception observed'
    assert subject.snapshot() == {'active_requests': 0, 'agents': {}}
    with pytest.raises(ValueError, match='cleanup failed'):
        await cleanup
