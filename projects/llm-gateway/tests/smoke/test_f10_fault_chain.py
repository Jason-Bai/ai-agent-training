from pricing_support import write_pricing
from registry_helpers import ModelConfig, ModelRegistry
"""CLI disposition closes an observable failed-primary/successful-backup request."""
from services.models import ConcurrencyPolicy
from decimal import Decimal
import json

import httpx
import pytest
import yaml

from api.identity import create_agent,issue_credential
from f3_cases import MODEL_VALUES
from f10_support import Environment
from integration.test_f5o2_http import case,server
from main import create_app
from persistence.billing import deposit
from protocol.test_providers import server as primary_wire,server as backup_wire
from services.models import ProviderConfig, RecoveryPolicy
from settings import Settings


@pytest.mark.parametrize('action,amount,available',[('settle','.008','.986'),('zero',None,'.994'),('platform',None,'.994')])
async def test_cli_closes_unknown_primary_after_backup_success(server,case,primary_wire,backup_wire,action,amount,available):
    name,config=case
    primary_wire.status=503
    backup_wire.raw=json.dumps({'object':'response','model':'deepseek-flash','status':'completed','error':None,
        'incomplete_details':None,'output':[{'type':'message','role':'assistant','status':'completed',
        'content':[{'type':'output_text','text':'A123'}]}],'usage':{'input_tokens':200,'output_tokens':40,'total_tokens':240,
        'input_tokens_details':{'cached_tokens':0}}}).encode()
    definitions={
        'primary':ProviderConfig(adapter='deepseek',api_protocol='chat_completions',base_url=primary_wire.url,credential_ref='wire'),
        'backup':ProviderConfig(adapter='openai_compatible',api_protocol='responses',base_url=backup_wire.url,credential_ref='wire')}
    models={'general-primary':ModelConfig(**(MODEL_VALUES|{'provider':'primary','fallback_models':('general-backup',)})),
            'general-backup':ModelConfig(**(MODEL_VALUES|{'provider':'backup'}))}
    write_pricing(config, {'bindings':{'primary':'fixture','backup':'fixture'},'versions':[{
        'supplier':'fixture','model':'deepseek-flash','version':'v1','effective_from':'2020-01-01T00:00:00Z',
        'peak':{'hit':'20','miss':'20','output':'50'},'off_peak':{'hit':'20','miss':'20','output':'50'}}]})
    (config/'credentials.yaml').write_text('credentials: {wire: {api_key: local-only}}')
    # Fixed input estimate makes the test's reserve independently hand-computable.
    from f3_cases import CountingFixture
    app=create_app(settings=Settings(env='test',_env_file=None),config_dir=config,
        model_registry=ModelRegistry(models,definitions,recovery=RecoveryPolicy(max_attempts=2,max_attempts_per_endpoint=1), concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64)),
        token_budget=CountingFixture(250).as_budget())
    async with app.router.lifespan_context(app):
        async with app.state.storage.database.transaction() as session:
            await create_agent(session,'a','A')
            token=await issue_credential(session,'a')
            await deposit(session,'a','initial','1')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
                                     headers={'Authorization':'Bearer '+token}) as client:
            reply=await client.post('/v1/llm',json={'model':'general-primary',
                'prompt':{'name':'basic_text','version':'1.0.0'},
                'messages':[{'role':'user','content':'A123'}],'max_output_tokens':100})
            assert reply.status_code==200
            rid=reply.headers['x-request-id']
            assert len(primary_wire.calls)==len(backup_wire.calls)==1
            before=(await client.get('/v1/billing/summary')).json()['data']
            assert Decimal(before['available'])==Decimal('.984')
            env=Environment(server,name,config.parent)
            args=['resolve',rid,'1',action,'--operation-id','verified-op','--reason','controlled statement']
            if amount is not None:
                args.extend(['--amount',amount])
            for _ in range(2):
                await env.cli(*args)
                after=(await client.get('/v1/billing/summary')).json()['data']
                assert Decimal(after['available'])==Decimal(available) and Decimal(after['pending'])==0
            rows=(await client.get('/v1/billing/requests/'+rid)).json()['data']['attempts']
            assert rows[0]['state']=={'settle':'settled','zero':'released','platform':'platform'}[action]
            assert rows[0]['supplier_cost'] is None if action=='platform' else Decimal(rows[0]['supplier_cost'])==Decimal(amount or '0')
