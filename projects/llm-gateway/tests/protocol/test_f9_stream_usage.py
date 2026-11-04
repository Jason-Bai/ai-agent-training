
import pytest

from contracts import Message
from f3_cases import registry
from providers.deepseek import DeepSeekProvider
from providers.openai_compatible import OpenAICompatibleProvider
from test_f5_providers import chat, frame, response, stream_server


@pytest.mark.parametrize('adapter_type', [DeepSeekProvider, OpenAICompatibleProvider])
@pytest.mark.parametrize('damage', ['none', 'conflict', 'after_report'])
async def test_cash_evidence_on_terminal_and_protocol_failure(adapter_type, damage, stream_server):
    if adapter_type is DeepSeekProvider:
        report = dict(prompt_tokens=10, completion_tokens=5, total_tokens=15,
                      prompt_cache_hit_tokens=7, prompt_cache_miss_tokens=3 if damage != 'conflict' else 4)
        stream_server.data = frame(chat('ok', 'stop', report))
        if damage != 'after_report':
            stream_server.data += frame('[DONE]')
    else:
        msg = response()
        msg['usage']['input_tokens_details'] = {'cached_tokens': 7 if damage != 'conflict' else True}
        if damage == 'after_report':
            msg['output'] = []
        stream_server.data = frame(dict(type='response.completed', response=msg), 'response.completed')
    provider = adapter_type(stream_server.url, 'fixture')
    try:
        events = [event async for event in provider.stream(
            registry().get_endpoint('general-primary'), [Message(role='user', content='A123')],
            max_output_tokens=32, timeout_seconds=3)]
    finally:
        await provider.aclose()
    terminal = events[-1]
    assert terminal.status == ('failed' if damage == 'after_report' else 'completed')
    assert terminal.usage.input_tokens == 10
    assert terminal.cached_input_tokens == (None if damage == 'conflict' else 7)
    assert all(event.cached_input_tokens is None for event in events[:-1])
