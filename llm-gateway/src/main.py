from pathlib import Path
from typing import Mapping

from fastapi import FastAPI

from api.errors import register_error_handlers
from api.routes import router
from api.billing import router as billing_router
from api.identity import AgentIdentityMiddleware
from services.prices import PriceBook
from middleware.request_context import RequestContextMiddleware
from providers.base import Provider
from providers.factory import ProviderClients
from services.models import DEFAULT_CONFIG_DIR, ModelRegistry, load_registry
from services.prompts import DEFAULT_TEMPLATES, PromptRegistry
from services.classification import CLASSIFIER_TEMPLATE, validate_classifier_prompts
from services.token_budget import TokenBudget
from services.trace import TraceStore
from services.concurrency import ConcurrencyLimiter
from services.streaming_manager import StreamingManager
from settings import Settings, load_settings
from persistence.database import Database, load_database_config
from persistence.store import Store
from persistence.reconciliation import PersistenceReconciler
from lifecycle import application_lifespan


def create_app(
    *, settings: Settings | None = None, provider: Provider | None = None,
    providers: Mapping[str, Provider] | None = None,
    prompt_registry: PromptRegistry | None = None, model_registry: ModelRegistry | None = None,
    token_budget: TokenBudget | None = None, config_dir: Path = DEFAULT_CONFIG_DIR,
    database: Database | None = None,
) -> FastAPI:
    settings = load_settings() if settings is None else settings
    prompt_registry = PromptRegistry([*DEFAULT_TEMPLATES, CLASSIFIER_TEMPLATE]) if prompt_registry is None else prompt_registry
    model_registry = load_registry(config_dir) if model_registry is None else model_registry
    validate_classifier_prompts(model_registry, prompt_registry)
    models = model_registry.endpoints.values()
    token_budget = TokenBudget.from_models(models) if token_budget is None else token_budget
    token_budget.validate_models(models)
    clients = ProviderClients(model_registry, provider=provider, providers=providers, config_dir=config_dir)
    database = Database(load_database_config(config_dir), config_dir) if database is None else database
    storage = Store(database, PriceBook.load(config_dir))
    active = set()
    generations = set()
    reconciler = PersistenceReconciler(storage, active, generations)
    traces = TraceStore()
    limiter = ConcurrencyLimiter(model_registry.concurrency)
    manager = StreamingManager(storage, traces, model_registry, token_budget, clients.instances, reconciler.defer, generations,
                               awaiting_persistence=reconciler.awaiting_persistence, limiter=limiter,
                               prompt_registry=prompt_registry)
    app = FastAPI(title="LLM Gateway", redirect_slashes=False,
                  lifespan=application_lifespan(database, storage, clients, manager, reconciler))
    app.state.concurrency_limiter = limiter
    app.state.streaming_manager = manager
    app.state.settings = settings
    app.state.providers = clients.instances
    app.state.prompt_registry = prompt_registry
    app.state.model_registry = model_registry
    app.state.token_budget = token_budget
    app.state.traces = traces
    app.state.storage = storage
    app.state.defer_persistence = reconciler.defer
    register_error_handlers(app)
    app.include_router(router)
    app.include_router(billing_router)
    app.add_middleware(RequestContextMiddleware, traces=traces, storage=storage, active=active, defer=reconciler.defer)
    app.add_middleware(AgentIdentityMiddleware, database=database)
    return app
