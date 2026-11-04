import json

import pytest

from contracts import Message
from errors import ProviderCallError
from providers.deepseek import DeepSeekProvider
from f3_cases import registry
from test_providers import adapter_type, server


MAX = 2**63 - 1


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('damage', [
    'none', 'missing_model', 'nul_model', 'surrogate_model', 'oversize_usage',
    'model_and_usage', 'nul_reason', 'surrogate_reason', 'missing_usage',
])
async def test_independent_verified_fields_from_wire(server, adapter_type, stream, damage):
    chat = adapter_type is DeepSeekProvider
    number = MAX + int(damage in {'oversize_usage', 'model_and_usage'})
    model = {'nul_model':'bad\x00model', 'surrogate_model':'bad\ud800name',
             'model_and_usage':None, 'missing_model':None}.get(damage, 'reported')
    reason = {'nul_reason':'bad\x00end', 'surrogate_reason':'bad\ud800end'}.get(damage)
    if chat:
        envelope = {'object':'chat.completion.chunk' if stream else 'chat.completion',
                    'choices':[{'index':0, 'finish_reason':reason or 'stop',
                                'delta' if stream else 'message':{'role':'assistant','content':'ok'}}],
                    'usage':{'prompt_tokens':number-1, 'completion_tokens':1, 'total_tokens':number}}
    else:
        envelope = {'object':'response', 'status':'incomplete' if reason else 'completed',
                    'incomplete_details':{'reason':reason} if reason else None,
                    'output':[{'type':'message','role':'assistant','status':'completed',
                               'content':[{'type':'output_text','text':'ok'}]}],
                    'usage':{'input_tokens':number-1, 'output_tokens':1, 'total_tokens':number}}
    if model is not None:
        envelope['model'] = model
    if damage == 'missing_usage':
        del envelope['usage']
    wire = {'type':'response.' + envelope['status'], 'response':envelope} if stream and not chat else envelope
    data = json.dumps(wire)
    server.raw = (('data: ' + data + '\n\n' + ('data: [DONE]\n\n' if chat else '')) if stream else data).encode()
    valid = damage in {'none', 'missing_usage'}
    adapter = adapter_type(server.url, 'FAKE_TEST_ONLY')
    config = registry().get_endpoint('general-primary')
    messages = [Message(role='user',content='hello')]
    options = dict(max_output_tokens=32,timeout_seconds=2)
    try:
        if stream:
            events = [e async for e in adapter.stream(config,messages,**options)]
            terminal = events[-1]
            assert terminal.status == ('completed' if valid else 'failed')
            if not valid:
                assert terminal.error_code == 'provider_invalid_response'
        elif valid:
            terminal = await adapter.complete(config,messages,**options)
        else:
            with pytest.raises(ProviderCallError) as error:
                await adapter.complete(config,messages,**options)
            terminal = error.value
            assert terminal.code == 'provider_invalid_response'
        assert terminal.actual_model == ('reported' if model == 'reported' else None)
        reported = None if terminal.usage is None else terminal.usage.total_tokens
        assert reported == (None if damage in {'oversize_usage','model_and_usage','missing_usage'} else MAX)
        assert len(server.calls) == 1
    finally:
        await adapter.aclose()


@pytest.mark.parametrize('field', ['actual_model','finish_reason','upstream_status','upstream_finish_reason','usage'])
@pytest.mark.parametrize('stream', [False, True])
def test_typed_per_attempt_metadata_safe(field, stream):
    from pydantic import ValidationError
    from contracts import Usage
    from providers.base import ProviderResult, ProviderStreamEvent
    if stream and field == 'finish_reason':
        # Stream events expose only upstream_finish_reason, enforced by a literal.
        field = 'upstream_finish_reason'
    args = {'status':'completed','upstream_terminal_received':True} if stream else {'text':'ok'}
    aggregate = Usage(input_tokens=MAX,output_tokens=1,total_tokens=MAX+1)
    assert aggregate.total_tokens == MAX+1
    args[field] = aggregate if field == 'usage' else 'bad\x00metadata'
    with pytest.raises(ValidationError):
        (ProviderStreamEvent if stream else ProviderResult)(**args)


async def test_mismatching_responses_terminal_is_failure_with_verified_facts(server):
    from providers.openai_compatible import OpenAICompatibleProvider
    adapter = OpenAICompatibleProvider(server.url, 'FAKE_TEST_ONLY')
    raw = {'type':'response.completed','response':{'object':'response','status':'incomplete','model':'reported',
           'usage':{'input_tokens':60,'output_tokens':25,'total_tokens':85}}}
    server.raw = ('data: ' + json.dumps(raw) + '\n\n').encode()
    try:
        result = [e async for e in adapter.stream(registry().get_endpoint('general-primary'),
                        [Message(role='user',content='question')],max_output_tokens=32,timeout_seconds=2)]
        assert len(server.calls) == len(result) == 1
        assert result[0].error_code == 'provider_invalid_response'
        assert result[0].actual_model == 'reported' and result[0].usage.total_tokens == 85
    finally:
        await adapter.aclose()
