from pricing_support import write_pricing
from registry_helpers import ModelConfig, ModelRegistry
"""SIGKILL a test-owned Gateway on either side of its first upstream call."""
from services.models import ConcurrencyPolicy
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import yaml

from f3_cases import MODEL_VALUES
from legacy_identity import provision
from postgres_support import write_config, migrate
from protocol.test_providers import server as wire
from services.models import ProviderConfig
from test_f5o1_process import body, gateway, process_pg, wait_for


BOOTSTRAP = """
import asyncio
from pathlib import Path
from main import create_app as build_app
from settings import Settings

ROOT = Path(__file__).parent

def create_app():
    app = build_app(settings=Settings(env='test', _env_file=None), config_dir=ROOT/'config')
    begin = app.state.storage.begin_attempt
    async def after_begin(trace, attempt, **kwargs):
        await begin(trace, attempt, **kwargs)
        (ROOT/'committed').touch()
        if (ROOT/'pause-before-provider').exists():
            await asyncio.Event().wait()
    app.state.storage.begin_attempt = after_begin
    return app
"""


@pytest.mark.parametrize('calls',[0,1],ids=['kill-after-hold','kill-after-upstream-entry'])
def test_paid_restart_keeps_unknown_hold_exactly_once(process_pg,tmp_path,wire,calls):
    name = process_pg.create_database()
    model = ModelConfig(**(MODEL_VALUES | {'provider':'wire'}))
    definition = ProviderConfig(adapter='deepseek',api_protocol='chat_completions',base_url=wire.url,
                                credential_ref='fixture')
    config = write_config(tmp_path/'config',process_pg,name,ModelRegistry({'general-primary':model},{'wire':definition}, concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64)))
    try:
        migrate(config)
        provision(config)
        (config/'credentials.yaml').write_text('credentials: {fixture: {api_key: loopback-only}}')
        rates = {'hit':'0','miss':'0','output':'1000'}
        write_pricing(config, {'bindings':{'wire':'fixture'},'versions':[{
            'model':'deepseek-flash','supplier':'fixture','version':'v1','effective_from':'2020-01-01T00:00:00Z',
            'peak':rates,'off_peak':rates}]})
        (tmp_path/'f5o1_bootstrap.py').write_text(BOOTSTRAP)
        if not calls:
            (tmp_path/'pause-before-provider').touch()
        wire.delay = 2
        with gateway(tmp_path) as (process,client), ThreadPoolExecutor() as pool:
            created = client.post('/v1/llm/stream',json=body('A123') | {'max_output_tokens':8})
            rid = created.json()['data']['request_id']
            path = f'/v1/llm/stream/{rid}/events'
            response = pool.submit(client.get,path)
            wait_for(lambda:(tmp_path/'committed').exists())
            if calls:
                wait_for(lambda:len(wire.calls)==1)
            assert len(wire.calls)==calls
            process.kill()
            process.wait(timeout=5)
            with pytest.raises(httpx.TransportError):
                response.result(timeout=5)
        with gateway(tmp_path) as (_,client):
            for _ in range(2):
                replay = client.get(path)
                assert replay.status_code==200 and 'execution_interrupted' in replay.text
                bills = client.get('/v1/billing/requests/'+rid).json()['data']
                item, = bills['attempts']
                assert item['state']=='pending' and item['agent_charge'] is item['supplier_cost'] is None
                totals = client.get('/v1/billing/summary').json()['data']
                assert Decimal(totals['available']) == Decimal('9999.992')
                assert Decimal(totals['pending']) == Decimal('.008')
        with process_pg.connect(name) as sql:
            assert sql.execute('SELECT state,reserved_amount,supplier_cost,agent_charge FROM tb_billing_attempt').fetchall() == [
                ('pending',Decimal('.008'),None,None)]
        assert len(wire.calls)==calls
    finally:
        process_pg.drop_database(name)
