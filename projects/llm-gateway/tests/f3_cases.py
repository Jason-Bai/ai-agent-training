from registry_helpers import ModelConfig, ModelRegistry
from services.models import ConcurrencyPolicy
from copy import deepcopy

from main import create_app
from providers.mock import MockProvider
from services.models import ProviderConfig, RecoveryPolicy
from services.token_budget import TokenBudget
from settings import Settings


MOCK_USAGE = {'input_tokens': 10, 'output_tokens': 1, 'total_tokens': 11}

MODEL_VALUES = dict(
    provider="mock-main", model_id="deepseek-flash", supports_streaming=True, thinking_mode="disabled", token_counter="deepseek_v41", structured_output_mode="none",
    context_window_tokens=1048576, provider_max_output_tokens=393216, default_output_tokens=1024,
    max_output_tokens=4096, safety_margin_tokens=1024, default_timeout_seconds=30, max_timeout_seconds=120,
)


def registry(changes=None):
    definitions = {}
    for name, provider in [("general-primary", "mock-main"), ("general-backup", "mock-backup")]:
        values = deepcopy(MODEL_VALUES)
        values["provider"] = provider
        values.update((changes or {}).get(name, {}))
        definitions[name] = ModelConfig(**values)
    return ModelRegistry(definitions, {name: ProviderConfig(adapter="mock", api_protocol="chat_completions") for name in ("mock-main", "mock-backup")}, recovery=RecoveryPolicy(max_attempts=1, max_attempts_per_endpoint=1), concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))


def make_app(*, primary=None, backup=None, changes=None, counter=None):
    providers = {"mock-main": primary or MockProvider(), "mock-backup": backup or MockProvider()}
    return create_app(settings=Settings(env="test", _env_file=None), providers=providers,
                      model_registry=registry(changes), token_budget=counter.as_budget() if counter is not None else None)


def primary_provider(app):
    return app.state.providers[app.state.model_registry.get_endpoint("general-primary").provider]


class CountingFixture:
    """Only isolates window arithmetic; official counter has independent golden tests."""
    version = "fixed-test-counter"

    def __init__(self, count=24, error=None):
        self.value = count
        self.error = error
        self.calls = 0

    def count(self, messages):
        self.calls += 1
        if self.error:
            raise self.error
        return self.value

    def supports(self, model):
        return model.model_id == "deepseek-flash" and model.thinking_mode == "disabled"

    def as_budget(self):
        return TokenBudget({"deepseek_v41": self})
