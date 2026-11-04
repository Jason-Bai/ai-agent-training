"""R1 P2#2: error terminal persistence shares the original request deadline."""
import json
import asyncio
from time import monotonic

from errors import ProviderCallError
from f6_cases import server, case, observe
from f7_cases import application, body
from providers.mock import MockProvider


async def test_failure_terminal_save_timeout_defers_without_rewriting_attempt(server,case,monkeypatch):
    name,config=case
    primary=MockProvider(error=ProviderCallError(code='provider_auth_error'))
    backup=MockProvider()
    interrupted=asyncio.Event()
    observed_deadlines=[]
    async with application(config,primary,backup) as (app,client):
        original=app.state.storage.finish
        async def delayed(trace,attempt=None,**kwargs):
            if kwargs.get('error_code')=='provider_auth_error':
                observed_deadlines.append(kwargs.get('deadline'))
                try:
                    # Real Request/Attempt writes before this point have committed;
                    # delay precedes the Request terminal transaction itself.
                    await asyncio.sleep(1.3)
                except asyncio.CancelledError:
                    interrupted.set()
                    raise
            return await original(trace,attempt,**kwargs)
        monkeypatch.setattr(app.state.storage,'finish',delayed)
        start=monotonic()
        response=await client.post('/v1/llm',json=body(timeout_seconds=1))
        elapsed=monotonic()-start
        assert response.status_code==504,response.text
        assert response.json()['code']=='request_timeout'
        assert elapsed<=1.25,elapsed
        assert interrupted.is_set()
        rid=response.headers['x-request-id']
        trace=app.state.traces.requests[rid]
        assert observed_deadlines==[trace._deadline]
        async with asyncio.timeout(3):
            while True:
                row,attempts,events=observe(server,name,rid)
                if row['status']=='failed':break
                await asyncio.sleep(.02)
        assert row['error_code']=='request_timeout' and events==[]
        assert json.loads(row['timing_snapshot'])['error_stage'] == 'finish'
        assert len(attempts)==1 and attempts[0]['status']=='failed'
        assert attempts[0]['error_code']=='provider_auth_error'
        assert len(primary.calls)==1 and not backup.calls
        assert attempts[0]['provider_entered'] and attempts[0]['ended_at'] is not None
