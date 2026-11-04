"""Test-only JSONB verification contract; no database or real Provider."""
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from pathlib import Path
import json
import pytest
from f13_real_support import RealEnvironment, PRICE_VERSION

def environment(quote):
    cost=Decimal('.000062')
    public=dict(charged=str(cost),attempts=[dict(agent_charge=str(cost),usage_source='real',usage_complete=True)])
    trace=dict(status='completed',attempts=[dict(model='light-endpoint',status='completed',configured_model='qwen3.8-flash',usage=dict(input_tokens=10,output_tokens=20,total_tokens=30))])
    class Connection:
        def execute(self,sql,args):
            if 'SELECT id,status' in sql:return SimpleNamespace(fetchone=lambda:(1,'completed'))
            if 'SELECT model,status' in sql:return SimpleNamespace(fetchall=lambda:[('light-endpoint','completed',10,20,30)])
            return SimpleNamespace(fetchall=lambda:[('settled',cost,quote,0)])
    @contextmanager
    def connect(name):yield Connection()
    env=RealEnvironment(SimpleNamespace(get=lambda path:SimpleNamespace(status_code=200,json=lambda:dict(data=public))),
      SimpleNamespace(state=SimpleNamespace(concurrency_limiter=SimpleNamespace(snapshot=lambda:dict(active_requests=0,agents={})))),
      SimpleNamespace(connect=connect),'fake',Path('.'))
    return env,trace

def test_verify_consumes_psycopg_jsonb_dictionary():
    env,trace=environment({'version':PRICE_VERSION})
    assert Decimal(env.verify('synthetic',trace)['charged'])==Decimal('.000062')

def test_verify_does_not_silently_accept_json_text_for_jsonb():
    env,trace=environment(json.dumps({'version':PRICE_VERSION}))
    with pytest.raises(AssertionError):env.verify('synthetic',trace)
