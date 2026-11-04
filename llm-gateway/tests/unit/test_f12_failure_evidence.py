"""Read-only evidence must wait for committed terminal, not just a free permit."""
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import pytest

import upstream_observation as observer


class SavedTrace:
    def __init__(self, status): self.status = status
    def model_dump(self, **kwargs): return {'status': self.status}


class Result:
    def __init__(self, values): self.values = values
    def mappings(self): return self
    def one(self): return self.values[0]
    def all(self): return self.values


def environment(statuses, monkeypatch, *, advance=None):
    state = {'reads': 0, 'status': 'running'}
    async def read_trace(*args):
        index = min(state['reads'], len(statuses)-1)
        state['status'] = statuses[index]
        state['reads'] += 1
        if advance is not None: advance()
        return SavedTrace(state['status'])
    monkeypatch.setattr('persistence.trace_query.read_trace', read_trace)
    class Session:
        async def execute(self, sql, params):
            text = str(sql)
            if 'FROM tb_llm_request' in text:
                return Result([{'id':1,'status':state['status'],'error_code':'provider_timeout'}])
            if 'FROM tb_billing_attempt' in text:
                return Result([{'attempt_no':1,'state':'reserved' if state['status']=='running' else 'pending',
                                'reserved_amount':1,'agent_charge':0,'supplier_cost':None}])
            return Result([])
    class Database:
        @asynccontextmanager
        async def transaction(self): yield Session()
    manager = SimpleNamespace(tasks={}, subscriptions={}, cleanup_tasks={}, cancellations={})
    trace = SimpleNamespace(status='failed', agent_id='test-agent')
    app = SimpleNamespace(state=SimpleNamespace(streaming_manager=manager,
        concurrency_limiter=SimpleNamespace(snapshot=lambda:{'active_requests':0,'agents':{}}),
        storage=SimpleNamespace(database=Database()), traces=SimpleNamespace(requests={'request':trace})))
    return app,state


async def test_evidence_waits_for_terminal_after_permit_is_already_released(tmp_path, monkeypatch):
    app,state=environment(['running','failed'],monkeypatch)
    await observer.snapshot_failures(app,tmp_path)
    saved=json.loads((tmp_path/'failure-scene-request.json').read_text())
    assert saved['trace']['status']=='failed'
    assert saved['sql_request']['status']=='failed'
    assert saved['bills'][0]['state']=='pending'
    assert state['reads']==2
    assert saved['evidence_complete'] is True


async def test_evidence_timeout_preserves_running_without_fabricating_failure(tmp_path, monkeypatch):
    clock={'now':0.}
    monkeypatch.setattr(observer,'monotonic',lambda:clock['now'])
    app,state=environment(['running'],monkeypatch,advance=lambda:clock.update(now=clock['now']+4))
    await observer.snapshot_failures(app,tmp_path)
    saved=json.loads((tmp_path/'failure-scene-request.json').read_text())
    assert saved['trace']['status']==saved['sql_request']['status']=='running'
    assert saved['bills'][0]['state']=='reserved'
    assert saved['evidence_complete'] is False
    assert saved['evidence_incomplete_reason']=='terminal_not_committed'
    assert state['reads']==2 and clock['now']==8


async def test_already_committed_terminal_needs_no_polling(tmp_path, monkeypatch):
    app,state=environment(['failed'],monkeypatch)
    await observer.snapshot_failures(app,tmp_path)
    saved=json.loads((tmp_path/'failure-scene-request.json').read_text())
    assert saved['evidence_complete'] is True
    assert state['reads']==1
