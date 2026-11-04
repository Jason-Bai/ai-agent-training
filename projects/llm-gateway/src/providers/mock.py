import asyncio
from copy import deepcopy

from contracts import Message, Usage
from providers.base import ProviderResult
from services.models import ModelConfig


# Synthetic report for the default successful Mock response.
_DEFAULT_USAGE = Usage(input_tokens=10, output_tokens=1, total_tokens=11)


class MockProvider:
    """Configured/test substitute with the same call and result contract."""
    def __init__(self, *, text='MOCK_REPLY', error: Exception | None = None,
                 result: ProviderResult | None = None, delay: float = 0, stream_events=None):
        self.text = text
        self.error = error
        self.result = result
        self.delay = delay
        self.calls: list[tuple[ModelConfig, list[Message]]] = []
        self.call_options: list[dict] = []
        self.close_calls = 0
        self.stream_events = stream_events
        self.stream_calls = []
        self.stream_options = []
        self.stream_next_calls = 0
        self.stream_close_calls = 0

    async def complete(self, model: ModelConfig, messages: list[Message], *,
                       max_output_tokens: int, timeout_seconds: int,
                       response_schema: dict | None = None) -> ProviderResult:
        self.calls.append((model, list(messages)))
        self.call_options.append({'max_output_tokens': max_output_tokens, 'timeout_seconds': timeout_seconds})
        if response_schema is not None:
            self.call_options[-1]['response_schema'] = deepcopy(response_schema)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result if self.result is not None else ProviderResult(text=self.text, usage=_DEFAULT_USAGE)

    async def stream(self, model, messages, *, max_output_tokens, timeout_seconds):
        from providers.base import ProviderStreamEvent
        from errors import ProviderCallError
        self.stream_calls.append((model, list(messages)))
        self.stream_options.append({'max_output_tokens': max_output_tokens, 'timeout_seconds': timeout_seconds})
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                if isinstance(self.error, ProviderCallError):
                    yield ProviderStreamEvent(status='failed', error_code=self.error.code, transient=self.error.transient,
                                              retry_after_seconds=self.error.retry_after_seconds,
                                              upstream_http_status=self.error.upstream_http_status,
                                              usage=self.error.usage, cached_input_tokens=self.error.cached_input_tokens, actual_model=self.error.actual_model,
                                              upstream_status=self.error.upstream_status,
                                              upstream_finish_reason=self.error.upstream_finish_reason)
                    return
                raise self.error
            events = self.stream_events
            if events is None:
                events = [
                    ProviderStreamEvent(delta=self.text),
                    ProviderStreamEvent(status='completed', upstream_terminal_received=True, usage=_DEFAULT_USAGE),
                ]
            for item in events:
                self.stream_next_calls += 1
                if isinstance(item, Exception):
                    if isinstance(item, ProviderCallError):
                        yield ProviderStreamEvent(status='failed', error_code=item.code, transient=item.transient,
                                                  retry_after_seconds=item.retry_after_seconds,
                                                  upstream_http_status=item.upstream_http_status,
                                                  usage=item.usage, cached_input_tokens=item.cached_input_tokens, actual_model=item.actual_model,
                                                  upstream_status=item.upstream_status,
                                                  upstream_finish_reason=item.upstream_finish_reason)
                        return
                    raise item
                if callable(item):
                    item = await item()
                    if item is None:
                        continue
                yield item
        finally:
            self.stream_close_calls += 1

    async def aclose(self):
        self.close_calls += 1
