"""F12 actual OS-process restart: a pending route survives only equivalent config."""
import asyncio

import pytest
import yaml

from f12_cases import registry,request
from f10_support import Environment,CredentialHeaders
from postgres_support import PostgresServer,write_config,migrate

BOOTSTRAP='''
from pathlib import Path
from main import create_app as build
from providers.mock import MockProvider
from settings import Settings
ROOT=Path(__file__).parent
def create_app():
    return build(settings=Settings(env='test',_env_file=None),config_dir=ROOT/'config',
                 provider=MockProvider(text='订单A123，商品充电器'))
'''


@pytest.mark.parametrize('change',['same','unrelated','related'])
async def test_pending_route_across_real_process_restart(tmp_path,change):
    pg=await asyncio.to_thread(lambda:PostgresServer().start())
    name=pg.create_database();env=Environment(pg,name,tmp_path,backend='mock')
    try:
        write_config(env.config,pg,name,registry())
        (tmp_path/'f10_bootstrap.py').write_text(BOOTSTRAP)
        await asyncio.to_thread(migrate,env.config)
        for agent in ('a','b'):
            await env.cli('create-agent',agent,'F12 restart')
            token=await env.cli('issue',agent)
            headers=CredentialHeaders(Authorization='Bearer '+token)
            if agent=='a':env.headers=headers
            else:env.other_headers=headers
            await env.cli('deposit',agent,'10000','--operation-id','f12-initial-'+agent)
        await env.start();first_pid=env.process.pid
        response=await env.client.post('/v1/llm/stream',json=request(streaming=True).model_dump(exclude_none=True))
        assert response.status_code==202,response.text
        rid=response.json()['data']['request_id']
        before=(await env.client.get('/v1/traces/'+rid)).json()['data']
        await env.stop()
        path=env.config/'gateway.yaml';config=yaml.safe_load(path.read_text())
        if change=='unrelated':
            config['model_groups']['unused']=config['model_groups']['general-primary'] | {'candidates':['responses-b']}
        if change=='related':config['model_groups']['general-primary']['max_output_tokens']=2048
        path.write_text(yaml.safe_dump(config))
        await env.start();assert env.process.pid!=first_pid
        result=await env.client.get(f'/v1/llm/stream/{rid}/events')
        assert result.status_code==200,result.text
        data=(await env.client.get('/v1/traces/'+rid)).json()['data']
        assert data['routing']==before['routing']
        if change=='related':
            assert data['status']=='failed' and data['error_code']=='execution_configuration_changed'
            assert data['attempts']==[]
        else:
            assert data['status']=='completed'
            assert [a['model'] for a in data['attempts']]==['chat-a']
        with pg.connect(name) as sql:
            counts=sql.execute('SELECT (SELECT count(*) FROM tb_llm_attempt WHERE request_pk=r.id),'
                '(SELECT count(*) FROM tb_billing_attempt WHERE request_pk=r.id) '
                'FROM tb_llm_request r WHERE request_id=%s',(rid,)).fetchone()
        assert counts==((0,0) if change=='related' else (1,1))
        again=await env.client.get(f'/v1/llm/stream/{rid}/events')
        assert again.text==result.text
        await env.assert_balances()
    finally:
        await env.stop()
        await asyncio.to_thread(pg.drop_database,name)
        await asyncio.to_thread(pg.close)
