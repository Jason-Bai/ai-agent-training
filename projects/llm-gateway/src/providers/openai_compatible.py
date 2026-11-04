from openai import AsyncOpenAI, APIConnectionError, APIStatusError, APITimeoutError, DefaultAsyncHttpx2Client
import httpx2
from starlette import status as http_status

from contracts import Message
from errors import ProviderCallError
from utils.json_codec import dumps
from providers.base import ProviderResult, error_for_status, parse_responses_response
from services.models import ModelConfig


class OpenAICompatibleProvider:
    """SDK transport; normalize raw JSON before any SDK model coercion."""
    def __init__(self, base_url: str, api_key: str):
        self.client = AsyncOpenAI(
            base_url=base_url, api_key=api_key, max_retries=0,
            http_client=DefaultAsyncHttpx2Client(trust_env=False, follow_redirects=False),
        )

    async def complete(self, model: ModelConfig, messages: list[Message], *,
                       max_output_tokens: int, timeout_seconds: int,
                       response_schema: dict | None = None) -> ProviderResult:
        constraints = {} if response_schema is None else {'text': {'format': self._response_format(response_schema)}}
        try:
            # The resource helper's standard JSON encoder cannot emit Decimal.
            # The SDK's public request method accepts pre-encoded JSON bytes;
            # its authentication, deadlines, transport and error mapping remain.
            raw_response = await self.client.post(
                '/responses', cast_to=httpx2.Response,
                content=dumps(dict(model=model.model_id, input=[m.model_dump() for m in messages],
                                max_output_tokens=max_output_tokens, reasoning={'effort': 'none'},
                                stream=False, store=False, **constraints)).encode('utf-8'),
                options={'timeout': timeout_seconds},
            )
        except APITimeoutError:
            raise ProviderCallError(code='provider_timeout', status_code=http_status.HTTP_504_GATEWAY_TIMEOUT) from None
        except APIConnectionError:
            raise ProviderCallError(code='provider_connection_error') from None
        except APIStatusError as error:
            raise error_for_status(error.status_code, error.response.headers) from None
        try:
            raw = raw_response.json()
        except ValueError:
            raise ProviderCallError(code='provider_invalid_response') from None
        return parse_responses_response(raw)

    def _response_format(self, schema):
        return {'type': 'json_schema', 'name': 'gateway_response', 'schema': schema}

    def _stream_events(self, lines, observed):
        from providers.streaming import responses_events
        return responses_events(lines, observed)

    async def stream(self, model, messages, *, max_output_tokens, timeout_seconds):
        from providers.streaming import failure
        from pydantic import ValidationError
        observed = {}
        try:
            # Streaming raw response avoids SDK model coercion and full-body buffering.
            async with self.client.responses.with_streaming_response.create(
                model=model.model_id, input=[m.model_dump() for m in messages],
                max_output_tokens=max_output_tokens, reasoning={'effort': 'none'},
                stream=True, store=False, timeout=timeout_seconds,
            ) as response:
                async for event in self._stream_events(response.iter_lines(), observed):
                    yield event
        except APITimeoutError:
            yield failure('provider_timeout', **observed)
        except APIConnectionError:
            yield failure('provider_connection_error', **observed)
        except APIStatusError as error:
            normalized = error_for_status(error.status_code, error.response.headers)
            yield failure(normalized.code, transient=normalized.transient,
                          retry_after_seconds=normalized.retry_after_seconds,
                          upstream_http_status=normalized.upstream_http_status, **observed)
        except httpx2.TimeoutException:
            yield failure('provider_timeout', **observed)
        except httpx2.RequestError:
            yield failure('provider_connection_error', **observed)
        except (ValueError, KeyError, TypeError, UnicodeError, ValidationError):
            yield failure('provider_invalid_response', **observed)

    async def aclose(self):
        await self.client.close()
