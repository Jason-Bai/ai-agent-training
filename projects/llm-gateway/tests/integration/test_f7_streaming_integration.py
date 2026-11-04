from registry_helpers import ModelRegistry
"""F7 recovery observed through committed PG and HTTP replay."""
from services.models import ConcurrencyPolicy
import asyncio
import pytest
from f7_stream_cases import (server,case,application,ScriptedStream,success,failure,create,observe,frames,until)
from providers.base import ProviderStreamEvent
from utils.json_codec import loads
from services.prompts import fingerprint_messages


@pytest.mark.parametrize('fallback',[False,True])
async def test_f7_predelta_retry_or_fallback_exact_attempts(server,case,fallback):
    name,config=case
    primary=ScriptedStream([[failure()], [failure()] if fallback else success()])
    backup=ScriptedStream([success()])
    async with application(config,primary,backup) as (app,client):
        rid=await create(client)
        result=await client.get(f'/v1/llm/stream/{rid}/events')
        row,attempts,events=observe(server,name,rid)
        assert row['status']=='completed',(row,result.text)
        assert len(primary.stream_calls)==2 and len(backup.stream_calls)==int(fallback)
        assert len(attempts)==2+int(fallback)
        assert [a['reason'] for a in attempts]==['initial','network_retry']+(['fallback'] if fallback else [])
        assert [e['event_type'] for e in events]==['response.delta','response.completed']
        assert frames(result.text)[-1][1]=='response.completed'
        for a in attempts:
            snapshot=loads(a['input_snapshot'])
            assert snapshot['messages']==[m.model_dump() for m in primary.stream_calls[0][1]]
            assert a['prompt_fingerprint']==fingerprint_messages(primary.stream_calls[0][1])
            assert a['max_output_tokens']==1024 and a['input_token_count']>0 and a['counter_version']
            assert a['provider_entered'] is True


@pytest.mark.parametrize('delta',['订单 A123',' ','\n'])
@pytest.mark.parametrize('code',['provider_connection_error','output_truncated'])
async def test_f7_committed_delta_without_subscriber_never_recovers(server,case,delta,code):
    name,config=case
    provider=ScriptedStream([[ProviderStreamEvent(delta=delta),failure(code)],success()])
    async with application(config,provider) as (app,client):
        rid=await create(client,initial_output_tokens=256,max_output_tokens=1024)
        await app.state.streaming_manager.subscribe(rid,0)
        await until(lambda: observe(server,name,rid)[0]['status']=='failed')
        row,attempts,events=observe(server,name,rid)
        assert len(provider.stream_calls)==len(attempts)==1
        assert [e['event_type'] for e in events]==['response.delta','response.failed']
        assert loads(events[0]['payload'])['delta']==delta
        replay=await client.get(f'/v1/llm/stream/{rid}/events')
        assert [f[1] for f in frames(replay.text)]==['response.delta','response.failed']


async def test_f7_empty_delta_does_not_block_recovery(server,case):
    name,config=case
    provider=ScriptedStream([[ProviderStreamEvent(delta=''),failure()],success()])
    async with application(config,provider) as (_,client):
        rid=await create(client)
        response=await client.get(f'/v1/llm/stream/{rid}/events')
        assert len(provider.stream_calls)==2
        assert [f[1] for f in frames(response.text)]==['response.delta','response.completed']
        assert observe(server,name,rid)[0]['status']=='completed'


async def test_f7_growth_budget_and_multiple_subscribers_replay(server,case):
    name,config=case;release=asyncio.Event()
    provider=ScriptedStream([[failure('output_truncated')],[release,*success()]])
    async with application(config,provider) as (app,client):
        rid=await create(client,initial_output_tokens=256,max_output_tokens=1024)
        await app.state.streaming_manager.subscribe(rid,0)
        await until(lambda:len(provider.stream_calls)==2)
        a=asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
        b=asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
        await asyncio.sleep(0);a.cancel();await asyncio.gather(a,return_exceptions=True)
        assert len(provider.stream_calls)==2
        release.set();result=await b
        assert [f[1] for f in frames(result.text)]==['response.delta','response.completed']
        replay=await client.get(f'/v1/llm/stream/{rid}/events?after_seq=1')
        assert frames(replay.text)==frames(result.text)[1:]
        _,attempts,_=observe(server,name,rid)
        assert provider.budgets==[256,1024]
        assert [a['max_output_tokens'] for a in attempts]==[256,1024]
        assert [a['reason'] for a in attempts]==['initial','output_growth']


@pytest.mark.parametrize('committed',[False,True])
@pytest.mark.parametrize('method',['finish_attempt','append_event'])
async def test_f7_uncertain_attempt_or_delta_commit_stops_model_recovery(server,case,monkeypatch,committed,method):
    from test_f6_faults import acknowledgement_loss
    name,config=case
    script=[failure()] if method=='finish_attempt' else [ProviderStreamEvent(delta='订单 A123'),failure()]
    provider=ScriptedStream([script,success()])
    async with application(config,provider) as (app,client):
        receipt=acknowledgement_loss(app,monkeypatch,method,committed)
        rid=await create(client)
        await app.state.streaming_manager.subscribe(rid,0)
        await until(lambda:rid not in app.state.streaming_manager.tasks)
        assert receipt['count']==1 and len(provider.stream_calls)==1
        row,attempts,events=observe(server,name,rid)
        assert len(attempts)==1
        deltas=[e for e in events if e['event_type']=='response.delta']
        assert len(deltas)==int(committed and method=='append_event')


async def test_f7_all_three_failures_have_one_failed_terminal_and_no_retry_event(server,case):
    name,config=case
    primary=ScriptedStream([[failure()],[failure()]])
    backup=ScriptedStream([[failure('provider_timeout')]])
    async with application(config,primary,backup) as (_,client):
        rid=await create(client)
        response=await client.get(f'/v1/llm/stream/{rid}/events')
        row,attempts,events=observe(server,name,rid)
        assert row['status']=='failed' and row['error_code']=='provider_timeout'
        assert len(primary.stream_calls)==2 and len(backup.stream_calls)==1
        assert [a['status'] for a in attempts]==['failed']*3
        assert [e['event_type'] for e in events]==['response.failed']
        assert [f[1] for f in frames(response.text)]==['response.failed']
        assert 'retry_exhausted' not in response.text and 'actual_model' not in response.text


@pytest.mark.parametrize('committed',[False,True])
async def test_f7_terminal_save_ack_loss_never_regenerates_answer(server,case,monkeypatch,committed):
    from test_f6_faults import acknowledgement_loss
    name,config=case;provider=ScriptedStream([[failure()],success()])
    async with application(config,provider) as (app,client):
        receipt=acknowledgement_loss(app,monkeypatch,'finish',committed)
        rid=await create(client)
        await app.state.streaming_manager.subscribe(rid,0)
        await until(lambda:rid not in app.state.streaming_manager.tasks)
        await until(lambda:observe(server,name,rid)[0]['status'] in {'completed','failed'})
        row,attempts,events=observe(server,name,rid)
        assert receipt['count']==1 and len(provider.stream_calls)==len(attempts)==2
        assert sum(e['event_type'] in {'response.completed','response.failed'} for e in events)==1
        if committed:assert row['status']=='completed'
        else:assert row['status']=='failed' and row['error_code']=='storage_unavailable'


@pytest.mark.parametrize('changed',['policy','backup_model','backup_counter'])
async def test_f7_pending_frozen_recovery_rejects_changed_configuration(server,case,monkeypatch,changed):
    from services.models import RecoveryPolicy
    name,config=case;provider=ScriptedStream([success()])
    async with application(config,provider) as (app,client):
        rid=await create(client)
        manager=app.state.streaming_manager;old=manager.registry
        if changed=='policy':
            manager.registry=ModelRegistry(old.endpoints,old.providers,model_groups=old.model_groups,recovery=RecoveryPolicy(max_attempts=1,max_attempts_per_endpoint=1), concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))
        elif changed=='backup_model':
            models=dict(old.endpoints)
            models['general-backup']=models['general-backup'].model_copy(update={'model_id':'changed'})
            manager.registry=ModelRegistry(models,old.providers,recovery=old.recovery, concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))
        else:
            original=manager.token_budget.counter_for
            class Changed:
                version='changed-backup-counter'
            monkeypatch.setattr(manager.token_budget,'counter_for',lambda m:Changed() if m.provider=='mock-backup' else original(m))
        result=await client.get(f'/v1/llm/stream/{rid}/events')
        row,attempts,events=observe(server,name,rid)
        assert row['status']=='failed' and row['error_code']=='execution_configuration_changed'
        assert provider.stream_calls==[] and attempts==[]
        assert frames(result.text)[-1][1]=='response.failed'
