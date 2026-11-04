"""Controlled dependencies around the real app lifespan and reconciliation."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import main
from services.models import ConcurrencyPolicy
from settings import Settings


@pytest.fixture
def wiring(monkeypatch, tmp_path):
    events = []
    database = SimpleNamespace()

    async def start():
        events.append('database.start')

    async def close_database():
        events.append('database.close')

    database.start, database.close = start, close_database

    class Storage:
        def __init__(self, *args):
            self.calls = []
            self.row = None
            self.failure = None
            self.on_finish = None

        async def recover(self):
            events.append('storage.recover')

        async def get_stream(self, request_id):
            self.calls.append(('get_stream', request_id))
            return self.row

        async def finish(self, trace, attempt, **kwargs):
            self.calls.append(('finish', trace.request_id, kwargs))
            if self.on_finish:
                await self.on_finish()
            if self.failure:
                raise self.failure
            return 'failed'

        async def update_diagnostics(self, trace, attempt):
            self.calls.append(('diagnostics', trace.request_id))

    class Clients:
        open_error = None

        def __init__(self, *args, **kwargs):
            self.instances = {}

        @asynccontextmanager
        async def open(self):
            events.append('providers.open')
            if self.open_error:
                raise self.open_error
            try:
                yield self.instances
            finally:
                events.append('providers.close')

    class Manager:
        def __init__(self, storage, traces, registry, budget, providers, defer, active, **kwargs):
            self.providers, self.defer, self.active = providers, defer, active
            self.awaiting_persistence = kwargs['awaiting_persistence']
            self.ticks = 0
            self.close_error = None

        async def reconcile(self):
            self.ticks += 1
            events.append('manager.reconcile')

        async def close(self):
            events.append('manager.close')
            if self.close_error:
                raise self.close_error

    monkeypatch.setattr(main, 'Store', Storage)
    monkeypatch.setattr(main, 'ProviderClients', Clients)
    monkeypatch.setattr(main, 'StreamingManager', Manager)
    # Price configuration is not the subject here; pass a valid empty document
    # for either side of the price-source migration.
    (tmp_path / 'gateway.yaml').write_text('pricing: {bindings: {}, versions: []}\n')
    registry = SimpleNamespace(auto_routes={}, endpoints={}, concurrency=ConcurrencyPolicy(max_requests=20, max_requests_per_agent=5))
    budget = SimpleNamespace(validate_models=lambda models: None)

    def make_app():
        return main.create_app(settings=Settings(env='test', _env_file=None), database=database,
                               model_registry=registry, token_budget=budget, config_dir=tmp_path)

    return SimpleNamespace(app=make_app(), make_app=make_app, database=database, events=events, clients=Clients)


def trace(request_id='request-1', **overrides):
    return SimpleNamespace(**(dict(request_id=request_id, operation='completion',
                                   status='running', error_code=None) | overrides))


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.001)
