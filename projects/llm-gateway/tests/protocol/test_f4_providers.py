from copy import deepcopy
from decimal import Decimal
import json

import pytest

from contracts import Message
from errors import ProviderCallError
from providers.base import validate_result
from providers.deepseek import DeepSeekProvider
from providers.openai_compatible import OpenAICompatibleProvider
from providers.mock import MockProvider
from f4_cases import registry as fixture_registry
from test_providers import server


RESPONSES = {'id':'fixture','object':'response','model':'deepseek-flash','status':'completed',
    'error':None,'incomplete_details':None,
    'output':[{'type':'message','role':'assistant','status':'completed',
               'content':[{'type':'output_text','text':'TEST_REPLY'}]}],
    'usage':{'input_tokens':120,'output_tokens':30,'total_tokens':150}}


async def test_sdk_uses_responses_route(server):
    # Chat response initially lets the old implementation finish; the route
    # assertion is the intended Red, not an import/response parsing failure.
    adapter=OpenAICompatibleProvider(server.url,'FAKE_CREDENTIAL_MARKER')
    try:
        try:
            await adapter.complete(fixture_registry().get_endpoint('general-primary'),[Message(role='user',content='Question')],max_output_tokens=64,timeout_seconds=2)
        except ProviderCallError:
            pass
        assert server.calls[0]['path']=='/responses'
    finally:
        await adapter.aclose()


@pytest.mark.parametrize('kind',[DeepSeekProvider,OpenAICompatibleProvider])
async def test_structured_outbound_contract(kind,server):
    if kind is OpenAICompatibleProvider: server.body=deepcopy(RESPONSES)
    adapter=kind(server.url,'FAKE_CREDENTIAL_MARKER')
    schema={'type':'object','properties':{'order_id':{'type':'string'}}}
    messages=[Message(role='system',content='Fixed'),Message(role='user',content='JSON data')]
    try:
        result=await adapter.complete(fixture_registry().get_endpoint('general-primary'),messages,max_output_tokens=64,timeout_seconds=2,response_schema=schema)
        validate_result(result)
        sent=server.calls[0]['body']
        if kind is DeepSeekProvider:
            assert sent['response_format']=={'type':'json_object'}
            assert sent['messages']==[m.model_dump() for m in messages]
        else:
            assert sent['text']=={'format':{'type':'json_schema','name':'gateway_response','schema':schema}}
            assert sent['input']==[m.model_dump() for m in messages]
            assert 'instructions' not in sent and 'messages' not in sent
            assert sent['store'] is False and sent['stream'] is False
            assert sent['reasoning']=={'effort':'none'}
            assert sent['max_output_tokens']==64
        assert len(server.calls)==1
    finally: await adapter.aclose()


@pytest.mark.parametrize('damage,code',[
    ('length','output_truncated'),('filter','provider_content_filtered'),('refusal','provider_refused'),
    ('failed','provider_error'),('failed_known','provider_rate_limited'),
    ('in_progress','provider_invalid_response'),('unknown','provider_invalid_response'),
    ('contradictory','provider_invalid_response'),('missing_reason','provider_invalid_response'),
    ('role','provider_invalid_response'),('tool','provider_invalid_response'),('empty','provider_invalid_response'),
    ('usage_bool','provider_invalid_response'),('usage_sum','provider_invalid_response'),
])
async def test_responses_states_are_not_json_parse_errors(server,damage,code):
    server.body=deepcopy(RESPONSES)
    body=server.body
    if damage in {'length','filter','missing_reason'}:
        body['status']='incomplete';body['incomplete_details']={'reason': {'length':'max_output_tokens','filter':'content_filter','missing_reason':None}[damage]}
    elif damage=='refusal':body['output'][0]['content'].append({'type':'refusal','refusal':'PRIVATE_REFUSAL'})
    elif damage.startswith('failed'):
        body['status']='failed';body['error']={'code':'rate_limit_exceeded' if damage=='failed_known' else 'unknown','message':'PRIVATE_ERROR'}
    elif damage in {'in_progress','unknown'}:body['status']=damage
    elif damage=='contradictory':body['error']={'code':'unknown','message':'PRIVATE_ERROR'}
    elif damage=='role':body['output'][0]['role']='user'
    elif damage=='tool':body['output'].append({'type':'function_call','arguments':'{}'})
    elif damage=='empty':body['output'][0]['content'][0]['text']='   '
    elif damage=='usage_bool':body['usage']['input_tokens']=True
    elif damage=='usage_sum':body['usage']['total_tokens']=151
    adapter=OpenAICompatibleProvider(server.url,'FAKE_CREDENTIAL_MARKER')
    try:
        with pytest.raises(ProviderCallError) as caught:
            result=await adapter.complete(fixture_registry().get_endpoint('general-primary'),[Message(role='user',content='Question')],max_output_tokens=64,timeout_seconds=2)
            validate_result(result)
        assert caught.value.code==code
        assert 'PRIVATE' not in str(caught.value)
        assert len(server.calls)==1
        if damage in {'length','filter','refusal','failed','failed_known'}:
            assert caught.value.usage.total_tokens==150
            assert caught.value.upstream_status==body['status']
    finally: await adapter.aclose()


async def test_response_text_parts_and_reasoning_are_separate(server):
    server.body=deepcopy(RESPONSES)
    server.body['output'].insert(0,{'type':'reasoning','content':[{'type':'reasoning_text','text':'PRIVATE_REASONING'}]})
    server.body['output'][1]['content']=[{'type':'output_text','text':' first '},{'type':'output_text','text':'second\n'}]
    adapter=OpenAICompatibleProvider(server.url,'FAKE_CREDENTIAL_MARKER')
    try:
        result=await adapter.complete(fixture_registry().get_endpoint('general-primary'),[Message(role='user',content='Question')],max_output_tokens=64,timeout_seconds=2)
        assert result.text==' first second\n' and result.finish_reason=='stop'
        assert result.upstream_status=='completed' and result.upstream_finish_reason is None
    finally: await adapter.aclose()


async def test_mock_copies_schema_without_changing_plain_options():
    mock=MockProvider();model=fixture_registry().get_endpoint('general-primary');messages=[Message(role='user',content='Q')]
    await mock.complete(model,messages,max_output_tokens=16,timeout_seconds=2)
    assert mock.call_options[0]=={'max_output_tokens':16,'timeout_seconds':2}
    schema={'type':'object'}
    await mock.complete(model,messages,max_output_tokens=16,timeout_seconds=2,response_schema=schema)
    schema['type']='array'
    assert mock.call_options[1]['response_schema']=={'type':'object'}


async def test_chat_length_keeps_priority_over_refusal(server):
    message=server.body['choices'][0]['message']
    message['refusal']='PRIVATE_REFUSAL'
    server.body['choices'][0]['finish_reason']='length'
    adapter=DeepSeekProvider(server.url,'FAKE_CREDENTIAL_MARKER')
    try:
        with pytest.raises(ProviderCallError) as caught:
            result=await adapter.complete(fixture_registry().get_endpoint('general-primary'),[Message(role='user',content='Q')],max_output_tokens=16,timeout_seconds=2)
            validate_result(result)
        assert caught.value.code=='output_truncated'
    finally:await adapter.aclose()


@pytest.mark.parametrize('value',[None,{},12])
async def test_failed_response_requires_valid_error_message(server,value):
    server.body=deepcopy(RESPONSES)
    server.body.update(status='failed',error={'code':'unknown','message':value})
    adapter=OpenAICompatibleProvider(server.url,'FAKE_CREDENTIAL_MARKER')
    try:
        with pytest.raises(ProviderCallError) as caught:
            await adapter.complete(fixture_registry().get_endpoint('general-primary'),[Message(role='user',content='Q')],max_output_tokens=16,timeout_seconds=2)
        assert caught.value.code=='provider_invalid_response'
    finally:await adapter.aclose()


async def test_sdk_emits_exact_schema_numbers_as_json_numbers(server):
    server.body=deepcopy(RESPONSES)
    schema={'type':'object','properties':{'n':{'minimum':Decimal('1.0000000000000001'),
                                           'const':Decimal('1E-400')}}}
    adapter=OpenAICompatibleProvider(server.url,'FAKE_CREDENTIAL_MARKER')
    try:
        result=await adapter.complete(fixture_registry().get_endpoint('general-backup'),[Message(role='user',content='JSON')],
                                      max_output_tokens=64,timeout_seconds=2,response_schema=schema)
        validate_result(result)
        wire=json.loads(server.calls[0]['raw_body'],parse_float=Decimal)
        actual=wire['text']['format']['schema']['properties']['n']
        assert actual['minimum']==Decimal('1.0000000000000001')
        assert actual['const']==Decimal('1E-400')
        assert isinstance(actual['minimum'],Decimal) and len(server.calls)==1
    finally:await adapter.aclose()
