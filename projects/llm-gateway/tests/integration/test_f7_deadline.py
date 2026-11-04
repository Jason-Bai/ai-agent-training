"""Request's final commit is part of the one shared deadline."""
import json
import asyncio
from time import monotonic
import pytest
from f6_cases import server,case,observe
from f7_cases import application,body
from providers.mock import MockProvider


async def test_f7_final_commit_cannot_extend_total_deadline(server,case,monkeypatch):
    name,config=case
    p=MockProvider()
    async with application(config,p,MockProvider()) as (app,client):
        original=app.state.storage.finish
        async def delayed(*args,**kwargs):
            await asyncio.sleep(1.3)
            return await original(*args,**kwargs)
        monkeypatch.setattr(app.state.storage,'finish',delayed)
        started=monotonic()
        response=await client.post('/v1/llm',json=body(timeout_seconds=1))
        elapsed=monotonic()-started
        assert response.status_code==504 and response.json()['code']=='request_timeout'
        assert elapsed<1.25
        rid=response.headers['x-request-id']
        assert len(p.calls)==1
        monkeypatch.setattr(app.state.storage,'finish',original)
        async with asyncio.timeout(3):
            while observe(server,name,rid)[0]['status']=='running':
                await asyncio.sleep(.02)
        row,attempts,_=observe(server,name,rid)
        assert row['status']=='failed' and row['error_code']=='request_timeout'
        assert json.loads(row['timing_snapshot'])['error_stage'] == 'finish'
        assert attempts[0]['status']=='completed'
        assert app.state.traces.attempts[-1].status=='completed'

@pytest.mark.parametrize('stage',['prepare','finish_attempt'])
async def test_f7_preparation_and_attempt_save_share_deadline(server,case,monkeypatch,stage):
    name,config=case;p=MockProvider()
    async with application(config,p,MockProvider()) as (app,client):
        original=getattr(app.state.storage,stage)
        async def delayed(*args,**kwargs):
            await asyncio.sleep(1.3)
            return await original(*args,**kwargs)
        monkeypatch.setattr(app.state.storage,stage,delayed)
        started=monotonic()
        response=await client.post('/v1/llm',json=body(timeout_seconds=1))
        assert response.status_code==504 and response.json()['code']=='request_timeout'
        assert monotonic()-started<1.25
        assert len(p.calls)==int(stage=='finish_attempt')
        monkeypatch.setattr(app.state.storage,stage,original)
        rid=response.headers['x-request-id']
        async with asyncio.timeout(3):
            while (observe(server,name,rid)[0] or {}).get('status') not in {'failed','completed'}:
                await asyncio.sleep(.02)
        row,attempts,_=observe(server,name,rid)
        assert row['status']=='failed'
        assert json.loads(row['timing_snapshot'])['error_stage'] == stage
        if stage=='finish_attempt': assert attempts[0]['status']=='completed'
        else: assert attempts==[]

async def test_f7_expired_admission_never_enters_provider(server,case,monkeypatch):
    import time
    name,config=case;p=MockProvider()
    async with application(config,p,MockProvider()) as (app,client):
        original=app.state.storage.begin_attempt
        async def late(*args,**kwargs):
            result=await original(*args,**kwargs)
            # Advance the real clock without yielding to the deadline callback.
            time.sleep(1.05)
            return result
        monkeypatch.setattr(app.state.storage,'begin_attempt',late)
        response=await client.post('/v1/llm',json=body(timeout_seconds=1))
        assert not p.calls
        assert response.status_code==504 and response.json()['code']=='request_timeout'
