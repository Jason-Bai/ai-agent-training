from pricing_support import copy_pricing
"""Owned-process F11 fixtures; gates observe real typed Provider resource ownership."""
import asyncio
from contextlib import asynccontextmanager
import json

import pytest_asyncio
import yaml

from f10_support import (Environment, CredentialHeaders, BOOTSTRAP, MOCK_BOOTSTRAP,
                         DEFAULT_CONFIG_DIR, real_registry)
from f4_cases import registry
from postgres_support import PostgresServer, write_config, migrate


GATED_BOOTSTRAP = '''
base_create_app = create_app
from contextvars import ContextVar
import asyncio, json
from contextlib import asynccontextmanager
current_f11 = ContextVar('f11_request', default=None)
def create_app():
    app = base_create_app()
    begin = app.state.storage.begin_attempt
    async def observed_begin(trace, attempt, **kwargs):
        await begin(trace, attempt, **kwargs)
        current_f11.set(trace.request_id)
    app.state.storage.begin_attempt = observed_begin
    def save(path, value):
        staging = path.with_suffix('.tmp')
        staging.write_text(json.dumps(value))
        staging.replace(path)
    def observed(rid, event):
        if rid is None: raise AssertionError('missing request association')
        path = ROOT / ('f11-provider-' + rid + '.json')
        values = json.loads(path.read_text()) if path.exists() else []
        values.append(event)
        save(path, values)
    def claim_gate():
        path = ROOT / 'f11-arm'
        if path.exists():
            path.unlink()
            return True
        return False
    async def hold(rid):
        save(ROOT / ('f11-held-' + rid + '.json'), {'request_id': rid})
        while not (ROOT / ('f11-release-' + rid)).exists():
            await asyncio.sleep(.01)
    def wrap(provider):
        complete = provider.complete
        stream = provider.stream
        async def gated_complete(*args, **kwargs):
            rid = current_f11.get()
            gate = claim_gate()
            observed(rid, 'complete_entered')
            result = await complete(*args, **kwargs)
            observed(rid, 'complete_result')
            if gate: await hold(rid)
            return result
        async def gated_stream(*args, **kwargs):
            rid = current_f11.get()
            gate = claim_gate()
            observed(rid, 'stream_entered')
            iterator = stream(*args, **kwargs)
            try:
                async for event in iterator:
                    yield event
                    if gate and event.delta:
                        gate = False
                        await hold(rid)
            finally:
                await iterator.aclose()
                observed(rid, 'stream_closed')
        provider.complete = gated_complete
        provider.stream = gated_stream
    original = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(app):
        async with original(app):
            for provider in {id(p): p for p in app.state.providers.values()}.values():
                wrap(provider)
            async def snapshots():
                while True:
                    limiter = getattr(app.state, 'concurrency_limiter', None)
                    save(ROOT / 'f11-counts.json', limiter.snapshot() if limiter else None)
                    await asyncio.sleep(.01)
            task = asyncio.create_task(snapshots())
            try: yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    app.router.lifespan_context = lifespan
    return app
'''


async def wait_file(path):
    async with asyncio.timeout(40):
        while not path.exists():
            await asyncio.sleep(.01)
    return json.loads(path.read_text())


async def counts(env, expected):
    async with asyncio.timeout(5):
        while True:
            value = json.loads((env.directory/'f11-counts.json').read_text())
            if value == expected:
                return value
            await asyncio.sleep(.01)


def provider_records(env, rid):
    path = env.directory/('f11-provider-'+rid+'.json')
    return json.loads(path.read_text()) if path.exists() else []


async def held_request(env):
    async with asyncio.timeout(40):
        while True:
            files = list(env.directory.glob('f11-held-*.json'))
            if files:
                return json.loads(files[-1].read_text())['request_id']
            await asyncio.sleep(.01)


def release(env, rid):
    (env.directory/('f11-release-'+rid)).touch()


@asynccontextmanager
async def environment(tmp_path, *, backend, limits):
    pg = await asyncio.to_thread(lambda: PostgresServer().start())
    name = pg.create_database()
    env = Environment(pg, name, tmp_path, backend=backend, concurrency=limits)
    try:
        write_config(env.config, pg, name, real_registry() if backend == 'real' else registry())
        document = yaml.safe_load((env.config/'gateway.yaml').read_text())
        document['concurrency'] = limits
        (env.config/'gateway.yaml').write_text(yaml.safe_dump(document))
        if backend == 'real':
            (env.config/'credentials.yaml').symlink_to(DEFAULT_CONFIG_DIR/'credentials.yaml')
            copy_pricing(env.config, DEFAULT_CONFIG_DIR)
        (tmp_path/'f10_bootstrap.py').write_text((BOOTSTRAP if backend == 'real' else MOCK_BOOTSTRAP)+GATED_BOOTSTRAP)
        await asyncio.to_thread(migrate, env.config)
        for agent in ('a', 'b'):
            await env.cli('create-agent', agent, 'F11 '+agent)
            token = await env.cli('issue', agent)
            headers = CredentialHeaders(Authorization='Bearer '+token)
            if agent == 'a': env.headers = headers
            else: env.other_headers = headers
            await env.cli('deposit', agent, '10000', '--operation-id', 'initial-'+agent)
        await env.start()
        await wait_file(tmp_path/'f11-counts.json')
        yield env
    finally:
        # Release every owned gate even when an assertion fails.
        for path in tmp_path.glob('f11-held-*.json'):
            release(env, json.loads(path.read_text())['request_id'])
        await env.stop()
        await asyncio.to_thread(pg.drop_database, name)
        await asyncio.to_thread(pg.close)


@pytest_asyncio.fixture
async def f11_mock_environment(request, tmp_path):
    async with environment(tmp_path, backend='mock', limits=request.param) as env:
        yield env


@pytest_asyncio.fixture
async def f11_real_environment(request, tmp_path):
    assert request.config.getoption('--run-live')
    async with environment(tmp_path, backend='real', limits=request.param) as env:
        yield env
