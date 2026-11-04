import asyncio

import httpx
import pytest

from main import create_app
from providers.mock import MockProvider


async def test_interrupted_response_is_not_confirmed_success(payload, app_settings):
    class InterruptedProvider(MockProvider):
        async def complete(self, model, messages, **options):
            raise asyncio.CancelledError()

    app = create_app(settings=app_settings, provider=InterruptedProvider())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            with pytest.raises(asyncio.CancelledError):
                await client.post("/v1/llm", json=payload)
    # O1 records interruption as failed, without inventing an F6 canceled state.
    record = next(iter(app.state.traces.requests.values()))
    assert record.status == "failed"
    assert record.attempts == 1
    assert app.state.traces.attempts[0].status == "failed"
