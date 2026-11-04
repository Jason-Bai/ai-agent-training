from pricing_support import write_pricing
"""Owned in-process HTTP/PG harness for explicitly enabled real Bailian calls.

This proves real Adapter/Provider execution, not external-client first-byte timing
or agreement with a supplier invoice. No maintainer runtime config is rewritten.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
import yaml

from legacy_identity import HEADERS, provision
from main import create_app
from postgres_support import PostgresServer, migrate
from services.credentials import load_credentials
from settings import Settings
from upstream_observation import install


MODELS={'light':'qwen3.8-flash','general':'qwen3.7-plus','analysis':'qwen3.8-max'}
GROUP_DESCRIPTIONS={'light':'提取、标签、简单改写', 'general':'依据明确资料回答及常规整理',
                    'analysis':'证据冲突、多条件推导和处理路径比较'}
PRICE_VERSION='aliyun-beijing-20261103-public-promo'
PRICE_SOURCES=[
    'https://help.aliyun.com/zh/model-studio/model-pricing',
    'https://help.aliyun.com/zh/model-studio/context-cache',
    'https://help.aliyun.com/zh/model-studio/qwen3-8-flash',
    'https://help.aliyun.com/zh/model-studio/qwen3-8-max',
]


def real_prices():
    """Official Beijing list: Plus current 0.8 promotion, implicit hit=20%.

    Flash/Max hit prices are explicit model-page prices, not the generic 20%.
    Public price-rule arithmetic is verified; account-specific invoice isn't.
    """
    versions=[]
    for model,hit,miss,output in [
        ('qwen3.8-flash','0.1','0.8','2.7'),
        ('qwen3.7-plus','0.32','1.6','6.4'),
        ('qwen3.8-max','1.5','12','36')]:
        rate=dict(hit=hit,miss=miss,output=output)
        value=dict(supplier='bailian-beijing',model=model,version=PRICE_VERSION,
            effective_from='2026-11-03T00:00:00+08:00',peak=rate,off_peak=rate)
        if model=='qwen3.7-plus':
            high=dict(hit='0.96',miss='4.8',output='19.2')
            value['input_tiers']=[dict(max_input_tokens=256000,peak=rate,off_peak=rate),
                                  dict(max_input_tokens=1000000,peak=high,off_peak=high)]
        versions.append(value)
    return dict(bindings={'bailian-main':'bailian-beijing'},versions=versions)


def configuration(server,name):
    endpoints={group+'-endpoint':dict(provider='bailian-main',model_id=model,
        thinking_mode='disabled',enabled=True,token_counter='qwen_text_estimate_v1',
        supports_streaming=True,structured_output_mode='json_schema',min_output_tokens=16,
        context_window_tokens=200000,provider_max_output_tokens=131072,max_output_tokens=4096,
        safety_margin_tokens=1024,max_timeout_seconds=120) for group,model in MODELS.items()}
    return dict(providers={'bailian-main':dict(adapter='bailian',api_protocol='responses',
        base_url='https://dashscope.aliyuncs.com/compatible-mode/v1',credential_ref='bailian-main-key')},
        endpoints=endpoints,
        model_groups={group:dict(candidates=[group+'-endpoint'],routing_description=GROUP_DESCRIPTIONS[group],
            default_output_tokens=128,
            max_output_tokens=4096,default_timeout_seconds=60,max_timeout_seconds=120) for group in MODELS},
        auto_routes={'auto-general':dict(classifier_group='light',candidate_groups=list(MODELS),
            default_group='general',classifier_prompt={'name':'task_router','version':'1.0.0'},
            classifier_timeout_seconds=5,classifier_max_output_tokens=128,default_output_tokens=1024,
            max_output_tokens=4096,default_timeout_seconds=30,max_timeout_seconds=120)},
        concurrency=dict(max_requests=20,max_requests_per_agent=5),
        recovery=dict(max_attempts=3,max_attempts_per_endpoint=2,base_delay_seconds=.1,max_delay_seconds=1.),
        database=server.config(name))


@pytest.fixture(scope='module')
def f13_live_pg(request):
    assert request.config.getoption('--run-live'),'real fixture requires explicit --run-live'
    server=PostgresServer().start()
    try:yield server
    finally:server.close()


@dataclass
class RealEnvironment:
    client: TestClient
    application: object
    server: PostgresServer
    database_name: str
    directory: Path
    observe_provider: object = None

    def trace(self,rid):
        reply=self.client.get('/v1/traces/'+rid)
        assert reply.status_code==200,reply.text
        return reply.json()['data']

    def verify(self,rid,trace):
        detail=self.client.get('/v1/billing/requests/'+rid)
        assert detail.status_code==200,detail.text
        bill=detail.json()['data']
        with self.server.connect(self.database_name) as sql:
            pk,status=sql.execute('SELECT id,status FROM tb_llm_request WHERE request_id=%s',(rid,)).fetchone()
            rows=sql.execute('SELECT model,status,input_tokens,output_tokens,total_tokens FROM tb_llm_attempt WHERE request_pk=%s ORDER BY attempt_no',(pk,)).fetchall()
            charges=sql.execute('SELECT state,agent_charge,quote,cached_input_tokens FROM tb_billing_attempt WHERE request_pk=%s ORDER BY attempt_no',(pk,)).fetchall()
        assert status==trace['status'] and len(rows)==len(trace['attempts'])==len(charges)
        assert len(charges)==len(bill['attempts'])
        total=Decimal(0)
        for attempt,row,charge,public in zip(trace['attempts'],rows,charges,bill['attempts']):
            assert row[0]==attempt['model'] and row[1]==attempt['status']
            assert attempt['usage'] is not None
            usage=attempt['usage'];assert row[2:]==(usage['input_tokens'],usage['output_tokens'],usage['total_tokens'])
            assert charge[0]=='settled',public
            quoted=charge[2]
            assert isinstance(quoted,dict),'PostgreSQL JSONB quote must be a decoded object'
            assert quoted['version']==PRICE_VERSION
            cached=charge[3];assert type(cached) is int and 0<=cached<=usage['input_tokens']
            rates=next(v for v in real_prices()['versions'] if v['model']==attempt['configured_model'])
            rate=rates['peak']
            if rates.get('input_tiers'):
                rate=next(t for t in rates['input_tiers'] if usage['input_tokens']<=t['max_input_tokens'])['peak']
            expected=(cached*Decimal(rate['hit'])+(usage['input_tokens']-cached)*Decimal(rate['miss'])
                      +usage['output_tokens']*Decimal(rate['output']))/Decimal(1000000)
            assert charge[1]==expected==Decimal(public['agent_charge'])
            assert public['usage_source']=='real' and public['usage_complete'] is True
            total+=expected
        assert Decimal(bill['charged'])==total
        counts=self.application.state.concurrency_limiter.snapshot()
        assert counts['active_requests']==0 and not counts['agents']
        return bill


@pytest.fixture
def f13_real_environment(f13_live_pg,tmp_path,request,monkeypatch):
    # This is the only maintainer secret read, reached only with --run-live.
    assert request.config.getoption('--run-live')
    project=Path(__file__).resolve().parents[1]
    credential=load_credentials(project/'config/credentials.yaml',{'bailian-main-key'})
    name=f13_live_pg.create_database();directory=tmp_path/'f13-real';directory.mkdir()
    config=directory/'config';config.mkdir()
    (config/'gateway.yaml').write_text(yaml.safe_dump(configuration(f13_live_pg,name)))
    keyfile=config/'credentials.yaml'
    try:
        keyfile.write_text(yaml.safe_dump({'credentials':{'bailian-main-key':{
            'api_key':credential['bailian-main-key'].get_secret_value()}}}))
        keyfile.chmod(0o600)
        write_pricing(config, real_prices())
        migrate(config);provision(config)
        from services.classification import CLASSIFIER_TEMPLATE
        from services.prompts import DEFAULT_TEMPLATES, PromptRegistry
        templates={ (template.name,template.version):template for template in DEFAULT_TEMPLATES }
        templates[(CLASSIFIER_TEMPLATE.name,CLASSIFIER_TEMPLATE.version)]=CLASSIFIER_TEMPLATE
        application=create_app(settings=Settings(env='test',_env_file=None),config_dir=config,
            prompt_registry=PromptRegistry(templates.values()))
        observe_provider=install(application,directory,monkeypatch)
        with TestClient(application,headers=HEADERS) as client:
            yield RealEnvironment(client,application,f13_live_pg,name,directory,observe_provider)
    finally:
        keyfile.unlink(missing_ok=True)
        f13_live_pg.drop_database(name)


def save_evidence(env,rid,trace,billing,extra=None):
    document=dict(request_id=rid,trace=trace,billing=billing,extra=extra,
        price_version=PRICE_VERSION,price_sources=PRICE_SOURCES,
        collected_at=datetime.now(timezone.utc).isoformat(),
        scope='in-process HTTP/real Bailian/owned PostgreSQL; public pricing, no supplier-invoice assertion')
    (env.directory/('f13-real-'+rid+'.json')).write_text(json.dumps(document,ensure_ascii=False))
