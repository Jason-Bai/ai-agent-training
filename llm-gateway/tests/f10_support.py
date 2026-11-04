from pricing_support import copy_pricing
"""Real-process acceptance harness; every writable resource belongs to this run."""
import asyncio
from datetime import datetime, timezone
from contextvars import copy_context
from decimal import Decimal, localcontext
import json
import os
import platform
from importlib.metadata import version
from pathlib import Path
import socket
import subprocess
import sys
from time import monotonic

import httpx
import pytest_asyncio
from psycopg.rows import dict_row

from postgres_support import PostgresServer, write_config, migrate
from services.models import DEFAULT_CONFIG_DIR
from live.test_real_providers import real_registry
from providers.mock import MockProvider
from providers.base import ProviderResult, ProviderStreamEvent
from contracts import Usage


PROJECT = Path(__file__).resolve().parents[1]
BOOTSTRAP = """
from contextlib import asynccontextmanager
import asyncio, json
from contextvars import ContextVar
from utils.json_codec import dumps
from datetime import datetime, timezone
from pathlib import Path
from main import create_app as build
from settings import Settings
ROOT=Path(__file__).parent
OBSERVATION_ERRORS=[]
def record_json(path,value):
    try:
        path.write_text(dumps(value))
    except (OSError,TypeError,ValueError) as exc:
        OBSERVATION_ERRORS.append(type(exc).__name__)
def create_app():
    app=build(settings=Settings(env='test',_env_file=None), config_dir=ROOT/'config')
    # Associate final upstream Responses snapshots with the executing request.
    # This is test observation only; the original parser/result is retained.
    from providers import streaming, openai_compatible, deepseek
    original_frames=streaming.frames
    async def observed_frames(lines):
        observed=[]
        rid=current_request.get()
        try:
            async for event,data in original_frames(lines):
                try: observed.append(dict(event=event,data=json.loads(data)))
                except ValueError: observed.append(dict(event=event,data=data))
                yield event,data
        finally:
            if rid is not None:
                record_json(ROOT/('wire-'+rid+'.json'),observed)
    streaming.frames=observed_frames
    original_status=deepseek.error_for_status
    def observed_status(status,headers=None):
        error=original_status(status,headers)
        rid=current_request.get()
        if rid is not None:
            record_json(ROOT/('http-failure-'+rid+'.json'),dict(status=status,code=error.code))
        return error
    deepseek.error_for_status=observed_status
    openai_compatible.error_for_status=observed_status
    original_parse=streaming.parse_responses_response
    current_request=ContextVar('f10_request',default=None)
    begin=app.state.storage.begin_attempt
    async def begin_observed(trace,attempt,**kwargs):
        await begin(trace,attempt,**kwargs)
        current_request.set(trace.request_id)
    app.state.storage.begin_attempt=begin_observed
    def parse_observed(raw):
        result=original_parse(raw)
        rid=current_request.get()
        if rid is not None and result.upstream_status=='completed':
            record_json(ROOT/('response-final-'+rid+'.json'),
                        dict(text=result.text,raw=raw,usage=raw.get('usage')))
        return result
    streaming.parse_responses_response=parse_observed
    openai_compatible.parse_responses_response=parse_observed
    original_chat=deepseek.parse_response
    def chat_observed(raw):
        result=original_chat(raw)
        rid=current_request.get()
        if rid is not None:
            record_json(ROOT/('response-final-'+rid+'.json'),
                        dict(text=result.text,raw=raw,usage=raw.get('usage')))
        return result
    deepseek.parse_response=chat_observed
    original=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(app):
        async with original(app):
            async def snapshot():
                while True:
                    manager=app.state.streaming_manager
                    concurrency=app.state.concurrency_limiter.snapshot()
                    values=dict(observation_errors=len(OBSERVATION_ERRORS),snapshot_at=datetime.now(timezone.utc).isoformat(), active_generations=len(manager.tasks), subscriptions=len(manager.subscriptions),
                                cleanup_tasks=len(manager.cleanup_tasks), cancellations=len(manager.cancellations),
                                active_requests=concurrency['active_requests'], agent_requests=concurrency['agents'],
                                trace_requests=len(app.state.traces.requests), trace_attempts=len(app.state.traces.attempts))
                    staging=ROOT/'observed.tmp'
                    staging.write_text(json.dumps(values))
                    staging.replace(ROOT/'observed.json')
                    await asyncio.sleep(1)
            task=asyncio.create_task(snapshot())
            try: yield
            finally:
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
    app.router.lifespan_context=lifespan
    from upstream_observation import install
    from _pytest.monkeypatch import MonkeyPatch
    install(app, ROOT, MonkeyPatch())
    return app
"""


# A test script at the typed Provider boundary; this is not an upstream protocol.
class AcceptanceMockProvider(MockProvider):
    def __init__(self, observe=lambda value: None):
        super().__init__()
        self.observe = observe

    def called(self, model, messages, options, operation, observe):
        observe(dict(kind='call', operation=operation, model=model.model_id,
                          provider=model.provider, messages=[m.model_dump() for m in messages],
                          options=options, source='scripted_mock_typed_provider'))

    async def complete(self, model, messages, *, max_output_tokens, timeout_seconds, response_schema=None):
        context = copy_context()
        observe = lambda value: context.run(self.observe, value)
        options = dict(max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds,
                       response_schema=response_schema)
        self.calls.append((model, list(messages)))
        self.call_options.append(options)
        self.called(model, messages, options, 'complete', observe)
        text = (json.dumps({'order_id':'A123','product':'充电器'}, ensure_ascii=False)
                if response_schema is not None else '订单 A123 的商品是充电器。')
        result = ProviderResult(text=text, usage=Usage(input_tokens=10, output_tokens=8, total_tokens=18))
        observe(dict(kind='result', text=text, usage=result.usage.model_dump()))
        return result

    async def stream(self, model, messages, *, max_output_tokens, timeout_seconds):
        # Cleanup may run in another Task; preserve this invocation's request association.
        context = copy_context()
        observe = lambda value: context.run(self.observe, value)
        options = dict(max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds)
        self.stream_calls.append((model, list(messages)))
        self.stream_options.append(options)
        self.called(model, messages, options, 'stream', observe)
        try:
            for part in ('订单 ', 'A123', ' 的商品是', '充电器', '。'):
                await asyncio.sleep(.05)
                self.stream_next_calls += 1
                observe(dict(kind='delta', text=part))
                yield ProviderStreamEvent(delta=part)
            # A bounded cancellation window after the first downstream delta.
            await asyncio.sleep(.2)
            terminal = ProviderStreamEvent(status='completed', upstream_terminal_received=True,
                                           usage=Usage(input_tokens=10, output_tokens=8, total_tokens=18))
            observe(dict(kind='terminal', usage=terminal.usage.model_dump()))
            yield terminal
        finally:
            self.stream_close_calls += 1
            observe(dict(kind='stream_closed'))

    async def aclose(self):
        self.close_calls += 1
        self.observe(dict(kind='provider_closed'))


def validate_mock_observation(records, *, attempts, model, operation, status):
    calls = [item for item in records if item['kind'] == 'call']
    assert len(calls) == attempts, 'Mock invocation count differs from persisted attempts'
    if not attempts:
        assert records == [], 'pending cancellation must not enter Mock'
        return dict(source='scripted_mock_typed_provider', calls=0, stream_closes=0)
    assert attempts == 1
    call = calls[0]
    assert call.get('source') == 'scripted_mock_typed_provider'
    assert call.get('provider') == ('mock-main' if model == 'general-primary' else 'mock-backup')
    assert call.get('model') == 'deepseek-flash'
    assert call.get('operation') == ('stream' if operation == 'streaming' else 'complete')
    options = call['options']
    assert options['max_output_tokens'] == 128
    timeout = options['timeout_seconds']
    assert type(timeout) in (int, float) and 0 < timeout <= 30
    messages = ''.join(message['content'] for message in call['messages'])
    assert 'A123' in messages and '充电器' in messages
    closes = sum(item['kind'] == 'stream_closed' for item in records)
    assert closes == (1 if operation == 'streaming' else 0), 'Mock stream not closed exactly once'
    reports = [item for item in records if item['kind'] in {'result','terminal'}]
    assert len(reports) == (1 if status == 'completed' else 0)
    if reports:
        assert reports[0]['usage'] == dict(input_tokens=10, output_tokens=8, total_tokens=18)
    return dict(source='scripted_mock_typed_provider', calls=len(calls), stream_closes=closes)


MOCK_BOOTSTRAP = """
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
import json
from main import create_app as build
from settings import Settings
from f10_support import AcceptanceMockProvider
ROOT=Path(__file__).parent
current_request=ContextVar('mock_acceptance_request',default=None)
def observe(value):
    rid=current_request.get()
    path=ROOT/('mock-'+rid+'.json' if rid else 'mock-lifecycle.json')
    values=json.loads(path.read_text()) if path.exists() else []
    values.append(value)
    path.write_text(json.dumps(values,ensure_ascii=False))
def create_app():
    provider=AcceptanceMockProvider(observe)
    app=build(settings=Settings(env='test',_env_file=None), provider=provider, config_dir=ROOT/'config')
    begin=app.state.storage.begin_attempt
    async def begin_observed(trace,attempt,**kwargs):
        await begin(trace,attempt,**kwargs)
        current_request.set(trace.request_id)
    app.state.storage.begin_attempt=begin_observed
    original=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(app):
        try:
            async with original(app): yield
        finally:
            await provider.aclose()
    app.router.lifespan_context=lifespan
    return app
"""


def payload(model='general-primary', mode='text'):
    value = {'model':model, 'prompt':{'name':'basic_text','version':'1.0.0'},
             'messages':[{'role':'user','content':'订单 A123 的商品是充电器。请仅用一句简短中文复述这个事实。'}],
             'max_output_tokens':128, 'timeout_seconds':30}
    if mode == 'structured':
        value['prompt'] = {'name':'context_answer','version':'1.2.0',
                           'variables':{'context':'订单 A123 的商品是充电器。'}}
        value['messages'][0]['content'] = '提取订单号和商品，返回JSON。'
        value['response_schema'] = {'type':'object','properties':{
            'order_id':{'type':'string'},'product':{'type':'string'}},
            'required':['order_id','product'],'additionalProperties':False}
    return value


async def frames(response):
    frame = []
    async for line in response.aiter_lines():
        if line:
            frame.append(line)
        elif frame:
            fields = dict(line.split(': ',1) for line in frame if ': ' in line)
            if 'event' in fields:
                yield (int(fields['id']) if 'id' in fields else None, fields['event'],json.loads(fields['data']))
            frame = []
    assert not frame, 'incomplete downstream SSE frame'


def verified_quality(answer, upstream, *, require_facts):
    assert answer==upstream, 'Gateway/upstream text mismatch'
    correct='A123' in answer and '充电器' in answer
    if require_facts:
        assert correct, 'business fact mismatch'
    return correct


class CredentialHeaders(dict):
    def __repr__(self):
        return "{'Authorization': '<redacted test credential>'}"


class Environment:
    def __init__(self, pg, name, directory, *, backend='real', concurrency=None):
        assert backend in {'real', 'mock'}
        self.backend = backend
        self.concurrency = dict(concurrency) if concurrency is not None else {
            'max_requests':64, 'max_requests_per_agent':64}
        self.pg, self.name, self.directory = pg,name,directory
        self.config = directory/'config'
        self.process = self.client = self.listener = self.log = None
        self.headers = self.other_headers = None
        self.bootstrap_number = 0

    async def cli(self, *args):
        result = await asyncio.to_thread(subprocess.run,
            [sys.executable,'-m','admin','--config-dir',str(self.config),*map(str,args)],
            cwd=PROJECT,env={'PATH':os.defpath,'PYTHONPATH':str(PROJECT/'src')},
            capture_output=True,text=True,timeout=30)
        assert result.returncode == 0, f'admin {args[0]} failed with exit {result.returncode}'
        return result.stdout.strip()

    async def start(self):
        import yaml
        path = self.config / 'gateway.yaml'
        document = yaml.safe_load(path.read_text())
        document['concurrency'] = self.concurrency
        path.write_text(yaml.safe_dump(document))
        self.listener=socket.socket()
        self.listener.bind(('127.0.0.1',0))
        self.listener.listen()
        port=self.listener.getsockname()[1]
        self.bootstrap_number+=1
        self.log=(self.directory/f'gateway-{self.bootstrap_number}.log').open('w+')
        self.process=subprocess.Popen(
            [sys.executable,'-m','uvicorn','f10_bootstrap:create_app','--factory','--workers','1',
             '--fd',str(self.listener.fileno()),'--log-level','warning','--timeout-graceful-shutdown','5'],
            cwd=self.directory,env={'PATH':os.defpath,'PYTHONPATH':os.pathsep.join([str(self.directory),str(PROJECT/'src'),str(PROJECT/'tests')])},
            pass_fds=(self.listener.fileno(),),stdout=self.log,stderr=subprocess.STDOUT)
        self.listener.close()
        self.client=httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',headers=self.headers,
                                      timeout=45,trust_env=False,limits=httpx.Limits(max_connections=100))
        async with asyncio.timeout(20):
            while True:
                assert self.process.poll() is None, 'isolated Gateway exited during startup'
                try:
                    if (await self.client.get('/healthz')).status_code==200:
                        return
                except httpx.TransportError:
                    pass
                await asyncio.sleep(.05)

    async def stop(self):
        if self.client is not None:
            await self.client.aclose()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait,timeout=12)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait,timeout=5)
                raise AssertionError('owned Gateway failed to shut down')
        if self.log is not None:
            self.log.close()

    async def restart(self):
        await self.stop()
        await self.start()

    def assert_provider_contract(self, trace):
        from services.models import read_yaml
        configuration = read_yaml(self.config / 'gateway.yaml')
        route = trace['routing']
        assert route['version'] == 1 and route['strategy'] == 'ordered'
        group = configuration['model_groups'][trace['requested_model']]
        assert [item['endpoint_id'] for item in route['candidates']] == group['candidates']
        assert route['selected_endpoint'] == trace['attempts'][0]['model']
        for attempt in trace['attempts']:
            endpoint = configuration['endpoints'][attempt['model']]
            provider = configuration['providers'][endpoint['provider']]
            assert attempt['provider'] == endpoint['provider']
            assert attempt['configured_model'] == endpoint['model_id']
            assert attempt['api_protocol'] == provider['api_protocol']
            assert attempt['adapter'] == provider['adapter']
        expected = 'chat_completions' if trace['requested_model'] == 'general-primary' else 'responses'
        assert trace['attempts'][0]['api_protocol'] == expected
        assert trace['attempts'][0]['adapter'] == ('mock' if self.backend == 'mock' else
                    'deepseek' if trace['requested_model'] == 'general-primary' else 'openai_compatible')

    def mock_observation(self, rid):
        path = self.directory/('mock-'+rid+'.json')
        return json.loads(path.read_text()) if path.exists() else []

    async def upstream_answer(self,rid,model,mode):
        if self.backend == 'mock':
            saved = self.mock_observation(rid)
            text = ''.join(item['text'] for item in saved if item['kind'] in {'result','delta'})
            return text, saved
        wire=mode=='streaming' and model=='general-primary'
        path=self.directory/(('wire-' if wire else 'response-final-')+rid+'.json')
        async with asyncio.timeout(3):
            while not path.exists():
                await asyncio.sleep(.01)
        saved=json.loads(path.read_text())
        if wire:
            parts=[]
            for event in saved:
                raw=event['data']
                if isinstance(raw,dict):
                    for choice in raw.get('choices',[]):
                        part=choice.get('delta',{}).get('content')
                        if isinstance(part,str):
                            parts.append(part)
            return ''.join(parts),saved
        raw=saved['raw']
        if model=='general-primary':
            text=raw['choices'][0]['message']['content']
        else:
            text=''.join(part['text'] for item in raw['output'] if item.get('type')=='message'
                         for part in item.get('content',[]) if part.get('type')=='output_text')
        return text,raw

    async def call(self, model, mode, *, headers=None, quality_gate=True):
        started=monotonic()
        data=payload(model,mode)
        events=[]
        first_text=None
        create_ms=None
        answer=None
        if mode=='streaming':
            response=await self.client.post('/v1/llm/stream',json=data,headers=headers)
            create_ms=(monotonic()-started)*1000
            rid=response.headers['x-request-id']
            if response.status_code!=202:
                return dict(request_id=rid,status='failed',error_code=response.json()['code'],
                            elapsed_ms=(monotonic()-started)*1000,first_text_ms=None,create_ms=create_ms,events=[])
            subscribed=monotonic()
            async with self.client.stream('GET',f'/v1/llm/stream/{rid}/events',headers=headers) as stream:
                if stream.status_code!=200:
                    await stream.aread()
                    return dict(request_id=rid,status='failed',error_code=stream.json()['code'],
                                elapsed_ms=(monotonic()-started)*1000,first_text_ms=None,create_ms=create_ms,events=[])
                async for event in frames(stream):
                    if event[1]=='response.delta' and first_text is None:
                        first_text=(monotonic()-subscribed)*1000
                    if event[1]!='response.heartbeat':
                        events.append(event)
            assert events and events[-1][1] in {'response.completed','response.failed','response.canceled'}
            terminal=events[-1][1].split('.')[1]
            code=events[-1][2].get('code')
            if terminal=='completed':
                answer=''.join(e[2]['delta'] for e in events if e[1]=='response.delta')
        else:
            response=await self.client.post('/v1/llm',json=data,headers=headers)
            rid=response.headers['x-request-id']
            terminal='completed' if response.status_code==200 else 'failed'
            code=response.json()['code'] if terminal=='failed' else None
            if terminal=='completed':
                answer=response.json()['data']['text']
                assert answer.strip()
                if mode=='structured':
                    assert response.json()['data']['parsed']=={'order_id':'A123','product':'充电器'}
        # End user-visible timing before disk observations and financial checks.
        elapsed_ms=(monotonic()-started)*1000
        quality=None
        anomaly=None
        if answer is not None:
            upstream,raw=await self.upstream_answer(rid,model,mode)
            assert answer==upstream, 'Gateway/upstream text mismatch '+rid
            quality=verified_quality(answer,upstream,require_facts=False)
            if not quality:
                anomaly=dict(request_id=rid,model=model,mode=mode,answer=answer,upstream=raw)
                (self.directory/('quality-'+rid+'.json')).write_text(json.dumps(anomaly,ensure_ascii=False))
                if quality_gate:
                    verified_quality(answer,upstream,require_facts=True)
        return dict(request_id=rid,status=terminal,error_code=code,elapsed_ms=elapsed_ms,
                    first_text_ms=first_text,create_ms=create_ms,events=events,quality_ok=quality,
                    quality_anomaly=anomaly)

    async def account_totals(self,headers=None):
        response=await self.client.get('/v1/billing/summary',headers=headers)
        assert response.status_code==200
        return response.json()['data']

    async def failure_evidence(self, rid, *, drained, downstream_text=None):
        from upstream_availability import classify_upstream_failure
        response=await self.client.get('/v1/traces/'+rid)
        if response.status_code==404:
            response=await self.client.get('/v1/traces/'+rid,headers=self.other_headers)
        assert response.status_code==200
        path=self.directory/('availability-'+rid+'.json')
        if not path.exists(): return None
        observation=json.loads(path.read_text())
        observation.update(drained=drained,accounting_verified=True,lifecycle_verified=True)
        return classify_upstream_failure(response.json()['data'],observation,downstream_text=downstream_text)

    async def assert_balances(self):
        for owner,headers in (('a',self.headers),('b',self.other_headers)):
            totals=await self.account_totals(headers)
            def independent_totals():
                with self.pg.connect(self.name,row_factory=dict_row) as sql:
                    return sql.execute(
                        """SELECT
                          (SELECT coalesce(sum(amount),0) FROM tb_billing_operation
                           WHERE agent_id=%s AND kind='deposit') AS deposited,
                          coalesce(sum(agent_charge),0) AS charged,
                          coalesce(sum(reserved_amount) FILTER (WHERE state IN ('reserved','pending')),0) AS reserved,
                          coalesce(sum(reserved_amount) FILTER (WHERE state='pending'),0) AS pending
                        FROM tb_billing_attempt WHERE agent_id=%s""", (owner,owner)).fetchone()
            saved=await asyncio.to_thread(independent_totals)
            assert all(Decimal(totals[key])==value for key,value in saved.items())
            with localcontext() as context:
                context.prec=80
                assert Decimal(totals['deposited'])-Decimal(totals['charged'])-Decimal(totals['reserved'])==Decimal(totals['available'])
                assert Decimal(totals['pending'])<=Decimal(totals['reserved'])

    async def verify(self,rid, *, expected_agent=None):
        def read():
            with self.pg.connect(self.name,row_factory=dict_row) as sql:
                r=sql.execute('SELECT * FROM tb_llm_request WHERE request_id=%s',(rid,)).fetchone()
                attempts=sql.execute('SELECT * FROM tb_llm_attempt WHERE request_pk=%s ORDER BY attempt_no',(r['id'],)).fetchall()
                events=sql.execute('SELECT seq,event_type,payload FROM tb_llm_stream_event WHERE request_pk=%s ORDER BY seq',(r['id'],)).fetchall()
                ledger=sql.execute('SELECT * FROM tb_billing_attempt WHERE request_pk=%s ORDER BY attempt_no',(r['id'],)).fetchall()
                return r,attempts,events,ledger
        async with asyncio.timeout(8):
            while True:
                row,attempts,events,ledger=await asyncio.to_thread(read)
                if row is not None and row['status'] in {'completed','failed','canceled'}:
                    break
                await asyncio.sleep(.1)
        if expected_agent is not None:
            assert row['agent_id']==expected_agent, 'response and request owner mismatch'
        assert row['status'] in {'completed','failed','canceled'}
        assert len(attempts)==len(ledger)
        assert [r['attempt_no'] for r in attempts]==[r['attempt_no'] for r in ledger]
        if row['operation']=='streaming':
            assert events, 'terminal stream must have a terminal business event'
        if events:
            assert [r['seq'] for r in events]==list(range(1,len(events)+1))
            assert events[-1]['event_type']=='response.'+row['status']
            assert sum(e['event_type'] in {'response.completed','response.failed','response.canceled'} for e in events)==1
        for attempt,bill in zip(attempts,ledger):
            assert bill['agent_id']==row['agent_id'], 'request and billing owner mismatch'
            assert bill['state']!='reserved'
            if bill['state']=='settled' and bill['quote'] is not None:
                q=bill['quote']; cache=bill['cached_input_tokens']
                assert bill['billing_observed_at'] is not None
                if cache is None:
                    assert q['hit']==q['miss'] or attempt['input_tokens']==0
                    cache=0
                with localcontext() as context:
                    context.prec=80
                    fee=(cache*Decimal(q['hit'])+(attempt['input_tokens']-cache)*Decimal(q['miss'])+
                         attempt['output_tokens']*Decimal(q['output']))/1_000_000
                assert fee==bill['agent_charge']==bill['supplier_cost']
            elif bill['state']=='pending':
                assert bill['agent_charge'] is None and bill['supplier_cost'] is None
        observation = None
        if self.backend == 'mock':
            observation = validate_mock_observation(self.mock_observation(rid), attempts=len(attempts),
                                                    model=row['requested_model'], operation=row['operation'], status=row['status'])
            for attempt, bill in zip(attempts, ledger):
                assert attempt['adapter'] == 'mock'
                assert bill['agent_charge'] == bill['supplier_cost'] == Decimal(0)
                if row['status'] == 'completed':
                    assert attempt['input_tokens'] == 10 and attempt['output_tokens'] == 8
        return {'provider_observation':observation,
                'request_id':rid,'status':row['status'],'error_code':row['error_code'],'attempts':len(attempts),'events':len(events),
                'bill_states':[b['state'] for b in ledger],
                'server_timing':json.loads(row['timing_snapshot']) if row['timing_snapshot'] else None}

    def metadata(self):
        memory = None
        if platform.system() == 'Darwin':
            memory = int(subprocess.run(['sysctl','-n','hw.memsize'], capture_output=True,
                                        text=True,check=True).stdout)
        return dict(system=platform.system(),release=platform.release(),machine=platform.machine(),
                    logical_cpus=os.cpu_count(),physical_memory_bytes=memory,python=sys.version,
                    dependencies={name:version(name) for name in ('fastapi','starlette','sqlalchemy','psycopg','uvicorn')},
                    upstream_wire_observation=True,workers=1,output_limit=128,request_timeout_seconds=30,
                    concurrency=self.concurrency,
                    recovery=real_registry().recovery.model_dump(),
                    models={k:v.model_dump(mode='json') for k,v in real_registry().endpoints.items()},
                    provider_host='api.deepseek.com',actual_configured_model='deepseek-flash',
                    database_image='postgres:18.6-bookworm')

    async def sample(self):
        def read():
            usage=subprocess.run(['ps','-p',str(self.process.pid),'-o','rss=,%cpu='],
                                 capture_output=True,text=True,check=True).stdout.split()
            observed=self.directory/'observed.json'
            state=json.loads(observed.read_text()) if observed.exists() else {}
            with self.pg.connect(self.name,row_factory=dict_row) as sql:
                values=sql.execute("""SELECT pg_database_size(current_database()) AS db_bytes,
                    (SELECT count(*) FROM tb_llm_request) AS requests,
                    (SELECT count(*) FROM tb_llm_stream_event) AS events,
                    (SELECT count(*) FROM tb_billing_attempt) AS billing_attempts,
                    (SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()) AS connections,
                    (SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND state='active') AS active_connections""").fetchone()
            return dict(state,**values,rss_kb=int(usage[0]),cpu_percent=float(usage[1]),
                        sampled_at=datetime.now(timezone.utc).isoformat())
        return await asyncio.to_thread(read)


@pytest_asyncio.fixture
async def f10_environment(request,tmp_path):
    assert request.config.getoption('--run-live'), 'F10 requires --run-live'
    pg=await asyncio.to_thread(lambda:PostgresServer().start())
    name=pg.create_database()
    env=Environment(pg,name,tmp_path)
    try:
        write_config(env.config,pg,name,real_registry())
        (env.config/'credentials.yaml').symlink_to(DEFAULT_CONFIG_DIR/'credentials.yaml')
        copy_pricing(env.config, DEFAULT_CONFIG_DIR)
        (tmp_path/'f10_bootstrap.py').write_text(BOOTSTRAP)
        await asyncio.to_thread(migrate,env.config)
        for agent in ('a','b'):
            await env.cli('create-agent',agent,'F10 '+agent)
            token=await env.cli('issue',agent)
            headers=CredentialHeaders(Authorization='Bearer '+token)
            if agent=='a': env.headers=headers
            else: env.other_headers=headers
            await env.cli('deposit',agent,'10000','--operation-id','initial-'+agent)
            await env.cli('deposit',agent,'10000','--operation-id','initial-'+agent)
        await env.start()
        yield env
    finally:
        await env.stop()
        await asyncio.to_thread(pg.drop_database,name)
        await asyncio.to_thread(pg.close)


@pytest_asyncio.fixture
async def mock_acceptance_environment(tmp_path):
    from f4_cases import registry
    pg=await asyncio.to_thread(lambda:PostgresServer().start())
    name=pg.create_database()
    env=Environment(pg,name,tmp_path,backend='mock')
    try:
        write_config(env.config,pg,name,registry())
        (tmp_path/'f10_bootstrap.py').write_text(MOCK_BOOTSTRAP)
        await asyncio.to_thread(migrate,env.config)
        for agent in ('a','b'):
            await env.cli('create-agent',agent,'BUG-002 '+agent)
            token=await env.cli('issue',agent)
            headers=CredentialHeaders(Authorization='Bearer '+token)
            if agent=='a': env.headers=headers
            else: env.other_headers=headers
            await env.cli('deposit',agent,'10000','--operation-id','initial-'+agent)
            await env.cli('deposit',agent,'10000','--operation-id','initial-'+agent)
        await env.start()
        yield env
    finally:
        await env.stop()
        try:
            if env.bootstrap_number:
                lifecycle=json.loads((tmp_path/'mock-lifecycle.json').read_text())
                assert sum(item['kind']=='provider_closed' for item in lifecycle)==env.bootstrap_number
        finally:
            await asyncio.to_thread(pg.drop_database,name)
            await asyncio.to_thread(pg.close)
