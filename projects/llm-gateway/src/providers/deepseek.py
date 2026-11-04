import httpx2
from starlette import status as http_status

from errors import ProviderCallError
from providers.base import ProviderResult, error_for_status, parse_response
from services.models import ModelConfig
from contracts import Message


class DeepSeekProvider:
    """Direct HTTP implementation, with no SDK or transport retries."""
    def __init__(self, base_url: str, api_key: str):
        self.client = httpx2.AsyncClient(
            base_url=base_url.rstrip('/')+'/',
            headers={'Authorization': f'Bearer {api_key}'},
            transport=httpx2.AsyncHTTPTransport(retries=0, trust_env=False),
            trust_env=False,
            follow_redirects=False,
        )

    async def complete(self, model: ModelConfig, messages: list[Message], *,
                       max_output_tokens: int, timeout_seconds: int,
                       response_schema: dict | None = None) -> ProviderResult:
        constraints = {'response_format': {'type': 'json_object'}} if response_schema is not None else {}
        try:
            response = await self.client.post('chat/completions', json={
                'model': model.model_id,
                'messages': [m.model_dump() for m in messages],
                'max_tokens': max_output_tokens,
                'thinking': {'type': 'disabled'},
                **constraints,
            }, timeout=timeout_seconds)
        except httpx2.TimeoutException:
            raise ProviderCallError(code='provider_timeout', status_code=http_status.HTTP_504_GATEWAY_TIMEOUT) from None
        except httpx2.RequestError:
            raise ProviderCallError(code='provider_connection_error') from None
        if not response.is_success:
            raise error_for_status(response.status_code, response.headers)
        try:
            raw = response.json()
        except ValueError:
            raise ProviderCallError(code='provider_invalid_response') from None
        return parse_response(raw)

    async def stream(self, model, messages, *, max_output_tokens, timeout_seconds):
        from providers.streaming import chat_events, failure
        from pydantic import ValidationError
        observed = {}
        try:
            async with self.client.stream('POST', 'chat/completions', json={
                'model': model.model_id, 'messages': [m.model_dump() for m in messages],
                'max_tokens': max_output_tokens, 'thinking': {'type': 'disabled'},
                'stream': True, 'stream_options': {'include_usage': True},
            }, timeout=timeout_seconds) as response:
                if not response.is_success:
                    error = error_for_status(response.status_code, response.headers)
                    yield failure(error.code, transient=error.transient,
                                  retry_after_seconds=error.retry_after_seconds,
                                  upstream_http_status=error.upstream_http_status, **observed)
                    return
                async for event in chat_events(response.aiter_lines(), observed):
                    yield event
        except httpx2.TimeoutException:
            yield failure('provider_timeout', **observed)
        except httpx2.RequestError:
            yield failure('provider_connection_error', **observed)
        except (ValueError, KeyError, TypeError, UnicodeError, ValidationError):
            yield failure('provider_invalid_response', **observed)

    async def aclose(self):
        await self.client.aclose()
