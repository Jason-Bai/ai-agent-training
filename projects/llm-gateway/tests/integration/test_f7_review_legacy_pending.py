from registry_helpers import ModelRegistry
"""F12 upgrade: old F6 snapshots fail conservatively; historical replay/cancel remain valid."""
from services.models import ConcurrencyPolicy
from contextlib import asynccontextmanager
import httpx
import pytest
from f4_cases import registry
from f6_cases import server,case,body,observe,frames,poll
from f7_stream_cases import ScriptedStream,success,failure
from main import create_app
from services.models import RecoveryPolicy
from settings import Settings
from utils.json_codec import dumps,loads


@asynccontextmanager
async def application(config,provider):
    base=registry()
    models=ModelRegistry(base.endpoints,base.providers,model_groups=base.model_groups,recovery=RecoveryPolicy(base_delay_seconds=0.,max_delay_seconds=0.), concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))
    app=create_app(settings=Settings(env='test',_env_file=None),config_dir=config,model_registry=models,provider=provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),base_url='http://test') as client:
            yield app,client


async def create(client):
    result=await client.post('/v1/llm/stream',json=body())
    assert result.status_code==202,result.text
    return result.json()['data']['request_id']


def restore_f6_snapshot(server,name,rid):
    row,attempts,events=observe(server,name,rid)
    source=loads(row['input_snapshot'])
    # F6 Gateway._snapshot contained exactly these six top-level fields and no
    # fallback_models ModelConfig field (verified against frozen F6 source).
    snapshot={k:source[k] for k in ('messages','model','provider','max_output_tokens','timeout_seconds','response_schema')}
    snapshot['model'].pop('fallback_models',None)
    snapshot['model'].pop('enabled',None)
    group=source['routing_configuration']['group']
    snapshot['model']['default_output_tokens']=group['default_output_tokens']
    snapshot['model']['default_timeout_seconds']=group['default_timeout_seconds']
    with server.connect(name) as c:
        c.execute('update tb_llm_request set input_snapshot=%s where request_id=%s',(dumps(snapshot),rid))
    return snapshot


@pytest.mark.parametrize('failed',[False,True])
async def test_f7_review_unchanged_legacy_pending_executes_once(server,case,failed):
    name,config=case
    provider=ScriptedStream([[failure()] if failed else success(),success()])
    async with application(config,provider) as (_,client):
        rid=await create(client)
        snapshot=restore_f6_snapshot(server,name,rid)
        response=await client.get(f'/v1/llm/stream/{rid}/events')
        row,attempts,events=observe(server,name,rid)
        assert row['status']=='failed',response.text
        assert row['error_code']=='execution_configuration_changed'
        assert provider.stream_calls==attempts==[]
        assert frames(response.text)[-1][1]=='response.failed'
        assert loads(row['input_snapshot'])==snapshot


async def test_f7_review_legacy_pending_cancel_never_calls_provider(server,case):
    name,config=case;provider=ScriptedStream([success()])
    async with application(config,provider) as (_,client):
        rid=await create(client);restore_f6_snapshot(server,name,rid)
        await poll(client,rid)
        response=await client.get(f'/v1/llm/stream/{rid}/events')
        row,attempts,events=observe(server,name,rid)
        assert row['status']=='canceled' and attempts==provider.stream_calls==[]
        assert [x[1] for x in frames(response.text)]==['response.canceling','response.canceled']


async def test_f7_review_legacy_terminal_replays_without_execution(server,case):
    name,config=case;provider=ScriptedStream([success()])
    async with application(config,provider) as (_,client):
        rid=await create(client)
        first=await client.get(f'/v1/llm/stream/{rid}/events')
        assert observe(server,name,rid)[0]['status']=='completed'
        restore_f6_snapshot(server,name,rid)
        replay=await client.get(f'/v1/llm/stream/{rid}/events')
        assert frames(first.text)==frames(replay.text)
        assert len(provider.stream_calls)==1


@pytest.mark.parametrize('change',[{'fallback_models':('general-backup',)},{'max_output_tokens':2048},{'default_timeout_seconds':29}])
async def test_f7_review_legacy_pending_rejects_real_model_change(server,case,change):
    name,config=case;provider=ScriptedStream([success()])
    async with application(config,provider) as (app,client):
        rid=await create(client);restore_f6_snapshot(server,name,rid)
        manager=app.state.streaming_manager;old=manager.registry
        models=dict(old.endpoints)
        models['general-primary']=models['general-primary'].model_copy(update=change)
        manager.registry=ModelRegistry(models,old.providers,recovery=old.recovery, concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))
        result=await client.get(f'/v1/llm/stream/{rid}/events')
        row,attempts,events=observe(server,name,rid)
        assert row['status']=='failed' and row['error_code']=='execution_configuration_changed'
        assert attempts==provider.stream_calls==[]
        assert frames(result.text)[-1][2]['code']=='execution_configuration_changed'
