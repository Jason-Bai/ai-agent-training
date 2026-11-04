"""Pure tests of the acceptance script and its independent observations."""

import pytest

import f10_support
from f4_cases import registry


@pytest.mark.parametrize('structured', [False, True])
async def test_acceptance_script_preserves_business_facts_and_reports_usage(structured):
    implementation = f10_support.AcceptanceMockProvider
    provider = implementation()
    result = await provider.complete(registry().endpoints['general-primary'], [],
                                     max_output_tokens=128, timeout_seconds=30,
                                     response_schema={'type': 'object'} if structured else None)
    assert 'A123' in result.text and '充电器' in result.text
    assert result.usage.input_tokens == 10 and result.usage.output_tokens == 8
    if structured:
        import json
        assert json.loads(result.text) == {'order_id': 'A123', 'product': '充电器'}


async def test_script_stream_close_is_observed_on_partial_consumption():
    records = []
    provider = f10_support.AcceptanceMockProvider(records.append)
    stream = provider.stream(registry().endpoints['general-primary'], [],
                             max_output_tokens=128, timeout_seconds=30)
    first = await anext(stream)
    assert first.delta
    await stream.aclose()
    assert [record['kind'] for record in records] == ['call', 'delta', 'stream_closed']
    assert provider.stream_close_calls == 1
    await provider.aclose()
    assert records[-1]['kind'] == 'provider_closed' and provider.close_calls == 1


@pytest.mark.parametrize('records', [
    [],
    [{'kind':'call','source':'real'}],
    [{'kind':'call','source':'scripted_mock_typed_provider'}, {'kind':'call'}],
])
def test_mock_observer_rejects_missing_or_false_call_evidence(records):
    validate = f10_support.validate_mock_observation
    with pytest.raises(AssertionError):
        validate(records, attempts=1, model='general-primary', operation='streaming', status='canceled')


@pytest.mark.parametrize('model', ['general-primary', 'general-backup'])
async def test_stream_observation_accepts_real_script_and_rejects_missing_close(model):
    from contracts import Message
    records = []
    provider = f10_support.AcceptanceMockProvider(records.append)
    messages = [Message(**item) for item in f10_support.payload(model)['messages']]
    events = [event async for event in provider.stream(registry().endpoints[model], messages,
                             max_output_tokens=128, timeout_seconds=30)]
    assert ''.join(event.delta for event in events) == '订单 A123 的商品是充电器。'
    assert events[-1].status == 'completed' and events[-1].upstream_terminal_received
    assert events[-1].usage.total_tokens == 18
    checked = f10_support.validate_mock_observation(records, attempts=1, model=model,
                                                    operation='streaming', status='completed')
    assert checked == dict(source='scripted_mock_typed_provider', calls=1, stream_closes=1)
    with pytest.raises(AssertionError, match='not closed exactly once'):
        f10_support.validate_mock_observation(records[:-1], attempts=1, model=model,
                                              operation='streaming', status='completed')


def test_pending_cancellation_observation_requires_zero_calls():
    assert f10_support.validate_mock_observation([], attempts=0, model='general-primary',
        operation='streaming', status='canceled')['calls'] == 0
    with pytest.raises(AssertionError, match='pending cancellation'):
        f10_support.validate_mock_observation([{'kind':'delta','text':'unexpected'}], attempts=0,
            model='general-primary', operation='streaming', status='canceled')


@pytest.mark.parametrize('timeout', [29.9, 30, 0, -1, 30.1, '29.9', None, True])
async def test_mock_observer_checks_remaining_timeout_budget(timeout):
    from contracts import Message
    records = []
    provider = f10_support.AcceptanceMockProvider(records.append)
    messages = [Message(**item) for item in f10_support.payload()['messages']]
    await provider.complete(registry().endpoints['general-primary'], messages,
                            max_output_tokens=128, timeout_seconds=timeout)
    kwargs = dict(attempts=1, model='general-primary', operation='completion', status='completed')
    if type(timeout) in (int, float) and 0 < timeout <= 30:
        f10_support.validate_mock_observation(records, **kwargs)
    else:
        with pytest.raises(AssertionError):
            f10_support.validate_mock_observation(records, **kwargs)


async def test_stream_close_remains_associated_with_call_context_across_tasks():
    import asyncio
    from contextvars import ContextVar
    request_id = ContextVar('observer_test_request_id', default=None)
    records = {}

    def observe(value):
        records.setdefault(request_id.get(), []).append(value['kind'])

    provider = f10_support.AcceptanceMockProvider(observe)
    streams = []
    for rid in ('request-a', 'request-b'):
        token = request_id.set(rid)
        try:
            stream = provider.stream(registry().endpoints['general-primary'], [],
                                     max_output_tokens=128, timeout_seconds=29.9)
            assert (await anext(stream)).delta
            streams.append(stream)
        finally:
            request_id.reset(token)
    # This cleanup task deliberately has no executing request ContextVar.
    async def cleanup():
        assert request_id.get() is None
        await asyncio.gather(*(stream.aclose() for stream in streams))
    await asyncio.create_task(cleanup())
    assert records == {
        'request-a': ['call', 'delta', 'stream_closed'],
        'request-b': ['call', 'delta', 'stream_closed'],
    }
