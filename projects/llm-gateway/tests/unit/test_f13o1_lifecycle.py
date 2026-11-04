"""Characterization tests first run against the original main.py closures."""
import pytest

from f13o1_support import wiring
from persistence.database import StorageUnavailable
from services.models import ConfigurationError


async def test_start_and_shutdown_keep_resource_order(wiring):
    app = wiring.app
    async with app.router.lifespan_context(app):
        assert wiring.events[:3] == ['database.start', 'storage.recover', 'providers.open']
    order = [event for event in wiring.events if event != 'manager.reconcile']
    assert order == ['database.start', 'storage.recover', 'providers.open',
                     'manager.close', 'providers.close', 'database.close']
    assert wiring.events[-2:] == ['manager.reconcile', 'database.close']


@pytest.mark.parametrize('stage', ['start', 'recover'])
async def test_failed_database_start_or_recovery_never_opens_provider(wiring, stage):
    async def fail():
        raise StorageUnavailable()
    if stage == 'start':
        wiring.database.start = fail
    else:
        wiring.app.state.storage.recover = fail
    with pytest.raises(ConfigurationError, match='数据库连接、结构版本或启动恢复检查失败'):
        async with wiring.app.router.lifespan_context(wiring.app):
            pytest.fail('startup must reject')
    assert 'providers.open' not in wiring.events
    assert wiring.events[-1] == 'database.close'


async def test_manager_close_failure_still_closes_provider_and_database(wiring):
    wiring.app.state.streaming_manager.close_error = RuntimeError('close failure')
    with pytest.raises(RuntimeError, match='close failure'):
        async with wiring.app.router.lifespan_context(wiring.app):
            pass
    assert wiring.events[-1] == 'database.close'
    assert wiring.events.index('manager.close') < wiring.events.index('providers.close')


async def test_provider_open_failure_cleans_recovery_task_and_database(wiring):
    wiring.clients.open_error = RuntimeError('provider open failed')
    with pytest.raises(RuntimeError, match='provider open failed'):
        async with wiring.app.router.lifespan_context(wiring.app):
            pytest.fail('startup must reject')
    assert 'manager.close' not in wiring.events
    assert wiring.events[-2:] == ['manager.reconcile', 'database.close']


async def test_final_reconciliation_is_bounded_and_database_is_closed(wiring, monkeypatch):
    import asyncio
    import lifecycle
    monkeypatch.setattr(lifecycle, 'DATABASE_SHUTDOWN_GRACE_SECONDS', .02)

    async def blocked():
        await asyncio.Event().wait()
    wiring.app.state.streaming_manager.reconcile = blocked

    async def run():
        async with wiring.app.router.lifespan_context(wiring.app):
            pass
    await asyncio.wait_for(run(), 1)
    assert wiring.events[-1] == 'database.close'


@pytest.mark.parametrize('variant', ['valid', 'missing', 'unsupported', 'missing-variable', 'extra-variable'])
def test_classification_startup_validation_stays_with_classification_component(variant):
    from types import SimpleNamespace
    from services.classification import CLASSIFIER_TEMPLATE, validate_classifier_prompts
    from services.prompts import PromptRegistry
    profile = SimpleNamespace(classifier_prompt=SimpleNamespace(name='task_router', version='1.0.0'))
    models = SimpleNamespace(auto_routes={'auto': profile})
    if variant in {'valid', 'missing'}:
        registry = PromptRegistry([CLASSIFIER_TEMPLATE] if variant == 'valid' else [])
    else:
        specs = {'candidate_groups': object(), 'task_material': object()}
        if variant == 'missing-variable':
            specs.pop('candidate_groups')
        if variant == 'extra-variable':
            specs['extra'] = object()
        registry = SimpleNamespace(get=lambda *args: SimpleNamespace(
            supports_structured_output=variant != 'unsupported', variable_specs=specs))
    if variant == 'valid':
        validate_classifier_prompts(models, registry)
    else:
        with pytest.raises(ConfigurationError, match='分类Prompt未注册|分类Prompt协议不兼容'):
            validate_classifier_prompts(models, registry)
