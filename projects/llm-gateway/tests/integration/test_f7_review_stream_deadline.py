"""R1 P2: Provider lifetime excludes pre-call registration and final persistence."""
import asyncio,time
import pytest
from f6_cases import server,case,observe
from f7_cases import registry,application,body
from providers.mock import MockProvider
from providers.base import ProviderStreamEvent
from errors import ProviderCallError

class SlowStream(MockProvider):
    def __init__(self,delay=.5):super().__init__();self.delay=delay;self.elapsed=None
    async def stream(self,model,messages,**kw):
        self.stream_calls.append((model,list(messages)));self.stream_options.append(kw);start=time.monotonic()
        try:
            await asyncio.sleep(self.delay)
            yield ProviderStreamEvent(delta='订单 A123')
            yield ProviderStreamEvent(status='completed',upstream_terminal_received=True)
        finally:self.elapsed=time.monotonic()-start;self.stream_close_calls+=1

@pytest.mark.parametrize('stage',['registration','admission','terminal_save'])
async def test_f7_provider_timer_excludes_nonprovider_stages(server,case,monkeypatch,stage):
    name,config=case
    models=registry({'general-primary':{'default_timeout_seconds':5,'max_timeout_seconds':5},'general-backup':{'default_timeout_seconds':1,'max_timeout_seconds':1}})
    primary=MockProvider(error=ProviderCallError(code='provider_connection_error'));backup=SlowStream()
    async with application(config,primary,backup,models=models) as (app,client):
        if stage=='registration':
            original=app.state.storage.begin_attempt
            async def slow(trace,attempt,**kw):
                await original(trace,attempt,**kw)
                if attempt.model=='general-backup':await asyncio.sleep(.7)
            monkeypatch.setattr(app.state.storage,'begin_attempt',slow)
        elif stage=='admission':
            original=app.state.streaming_manager._admit_provider
            async def slow(rid,execution,start):
                if execution.prepared.model_name=='general-backup':await asyncio.sleep(.7)
                return await original(rid,execution,start)
            monkeypatch.setattr(app.state.streaming_manager,'_admit_provider',slow)
        else:
            original=app.state.storage.finish
            async def slow(trace,attempt=None,**kw):
                if attempt and attempt.model=='general-backup':await asyncio.sleep(.7)
                return await original(trace,attempt,**kw)
            monkeypatch.setattr(app.state.storage,'finish',slow)
        rid=(await client.post('/v1/llm/stream',json=body(timeout_seconds=5))).json()['data']['request_id']
        response=await client.get(f'/v1/llm/stream/{rid}/events')
        row,attempts,events=observe(server,name,rid)
        assert row['status']=='completed',(stage,row['error_code'],backup.elapsed)
        assert len(primary.stream_calls)==2 and len(backup.stream_calls)==1
        assert attempts[-1]['status']=='completed'
        assert backup.elapsed>=.49
        assert events[-1]['event_type']=='response.completed'
        assert 'response.failed' not in [e['event_type'] for e in events]

async def test_f7_provider_timeout_still_bounds_actual_generation(server,case):
    name,config=case;models=registry({'general-primary':{'default_timeout_seconds':5,'max_timeout_seconds':5},'general-backup':{'default_timeout_seconds':1,'max_timeout_seconds':1}})
    primary=MockProvider(error=ProviderCallError(code='provider_connection_error'));backup=SlowStream(1.4)
    async with application(config,primary,backup,models=models) as (_,client):
        rid=(await client.post('/v1/llm/stream',json=body(timeout_seconds=5))).json()['data']['request_id']
        await client.get(f'/v1/llm/stream/{rid}/events')
        row,attempts,_=observe(server,name,rid)
        assert row['status']=='failed' and row['error_code']=='provider_timeout'
        assert len(backup.stream_calls)==1 and backup.elapsed<1.3
