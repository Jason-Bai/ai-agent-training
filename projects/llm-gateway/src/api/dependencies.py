from fastapi import Request

from services.gateway import GatewayService


def get_gateway(request: Request) -> GatewayService:
    state = request.app.state
    return GatewayService(state.providers, state.traces, state.prompt_registry, state.model_registry,
                          state.token_budget, storage=state.storage, defer=state.defer_persistence,
                          limiter=state.concurrency_limiter)
