from copy import deepcopy

import pytest

from errors import ProviderCallError
from providers.base import parse_response, validate_result


SUCCESS = {"id":"fake", "object":"chat.completion", "created":0,"model":"deepseek-flash", "choices":[{"index":0,"message":{"role":"assistant","content":"TEST_REPLY"},"finish_reason":"stop"}], "usage":{"prompt_tokens":120,"completion_tokens":30,"total_tokens":150}}


def test_success_is_normalized_and_validated():
    result = parse_response(deepcopy(SUCCESS))
    assert result.text == 'TEST_REPLY'
    assert result.actual_model == 'deepseek-flash'
    assert result.usage.model_dump() == {'input_tokens':120,'output_tokens':30,'total_tokens':150}
    assert validate_result(result) is None


@pytest.mark.parametrize('reason,code,status', [('length','output_truncated',502),('content_filter','provider_content_filtered',502),('aborted','provider_unavailable',503),('insufficient_system_resource','provider_unavailable',503),('unknown','provider_invalid_response',502)])
def test_non_success_completion_preserves_metadata(reason,code,status):
    raw=deepcopy(SUCCESS);raw['choices'][0]['finish_reason']=reason
    with pytest.raises(ProviderCallError) as exc:
        validate_result(parse_response(raw))
    assert (exc.value.code,exc.value.status_code)==(code,status)
    assert exc.value.finish_reason == reason
    assert exc.value.usage.total_tokens == 150
    assert exc.value.actual_model == 'deepseek-flash'


@pytest.mark.parametrize('usage', [None,{}, {'prompt_tokens':True,'completion_tokens':30,'total_tokens':31}, {'prompt_tokens':'120','completion_tokens':30,'total_tokens':150}, {'prompt_tokens':120,'completion_tokens':-1,'total_tokens':119}, {'prompt_tokens':120,'completion_tokens':30,'total_tokens':151}])
def test_usage_is_unknown_or_strict(usage):
    raw=deepcopy(SUCCESS);raw['usage']=usage
    if usage is None:
        assert parse_response(raw).usage is None
    else:
        with pytest.raises(ProviderCallError) as exc: parse_response(raw)
        assert exc.value.code=='provider_invalid_response'


@pytest.mark.parametrize('path,value', [('model',None),('model',' '),('choices',[]),('choices',{}),('choices',None),('text',None),('text',' '),('text',123),('finish',None),('finish',False),('role','user'),('tool_calls',[{'id':'unexpected'}])])
def test_malformed_response_is_rejected(path,value):
    raw=deepcopy(SUCCESS)
    if path in {'model','choices'}:raw[path]=value
    elif path=='text':raw['choices'][0]['message']['content']=value
    elif path=='finish':raw['choices'][0]['finish_reason']=value
    elif path=='role':raw['choices'][0]['message']['role']=value
    else:raw['choices'][0]['message']['tool_calls']=value
    with pytest.raises(ProviderCallError) as exc: validate_result(parse_response(raw))
    assert exc.value.code=='provider_invalid_response'


def test_raw_exception_text_is_never_retained():
    error=ProviderCallError('PRIVATE_SECRET')
    assert 'PRIVATE_SECRET' not in str(error)
    assert 'PRIVATE_SECRET' not in repr(error)
    assert 'PRIVATE_SECRET' not in repr(error.__dict__)
