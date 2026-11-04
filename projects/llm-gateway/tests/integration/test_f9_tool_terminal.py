"""Compare the same final metering evidence across ordinary and streaming Chat."""
import json
from decimal import Decimal

import pytest

from f6_cases import case, server
from f7_cases import body
from test_f9_paid_flow import account, primary_wire, backup_wire


@pytest.mark.parametrize('reason', ['tool_calls', None, 'unrecognized'])
@pytest.mark.parametrize('stream', [False, True])
async def test_tool_output_rejection_keeps_independent_final_metering(case, primary_wire, backup_wire,
                                                                    reason, stream):
    usage = {'prompt_tokens': 10, 'completion_tokens': 1, 'total_tokens': 11,
             'prompt_cache_hit_tokens': 7, 'prompt_cache_miss_tokens': 3}
    calls = [{'index': 0, 'id': 'tool', 'type': 'function',
              'function': {'name': 'unused', 'arguments': '{}'}}]
    output = {'tool_calls': calls, 'content': None}
    if stream:
        raw = {'object': 'chat.completion.chunk', 'model': 'deepseek-flash', 'usage': usage,
               'choices': [{'index': 0, 'delta': output, 'finish_reason': reason}]}
        primary_wire.raw = ('data: ' + json.dumps(raw) + '\n\n').replace('\\n', '\n').encode()
    else:
        primary_wire.body = {'object': 'chat.completion', 'model': 'deepseek-flash', 'usage': usage,
                            'choices': [{'index': 0, 'message': dict(output, role='assistant'),
                                         'finish_reason': reason}]}
    async with account(case[1], primary_wire, backup_wire, ('20', '30', '50')) as (_, client):
        path = '/v1/llm/stream' if stream else '/v1/llm'
        response = await client.post(path, json=body(max_output_tokens=100))
        rid = response.headers['x-request-id']
        if stream:
            events = await client.get(f'/v1/llm/stream/{rid}/events')
            assert 'provider_invalid_response' in events.text
            assert 'response.completed' not in events.text
        else:
            assert response.status_code == 502 and response.json()['code'] == 'provider_invalid_response'
        detail = (await client.get('/v1/billing/requests/' + rid)).json()['data']
        item, = detail['attempts']
        summary = (await client.get('/v1/billing/summary')).json()['data']
        final = reason == 'tool_calls'
        assert item['usage_complete'] is final
        assert item['state'] == ('settled' if final else 'pending')
        assert summary['usage']['real']['unknown_attempts'] == (0 if final else 1)
        assert len(primary_wire.calls) == 1 and not backup_wire.calls
        if final:
            assert Decimal(item['agent_charge']) == Decimal(item['supplier_cost']) == Decimal('.000280')
            assert Decimal(detail['reserved']) == 0
            assert Decimal(summary['available']) == Decimal('.999720')
        else:
            assert item['agent_charge'] is item['supplier_cost'] is None
            assert Decimal(detail['reserved']) > 0
