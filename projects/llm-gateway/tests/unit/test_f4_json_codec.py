from decimal import Decimal
import json

import pytest

from utils.json_codec import dumps, loads


@pytest.mark.parametrize('literal',['1.0000000000000001','1e-400','1e999','-0.00','12345678901234567890.1'])
def test_decimal_round_trip_preserves_value_and_json_number_type(literal):
    parsed=loads('{"n":'+literal+'}')
    assert parsed['n']==Decimal(literal)
    encoded=dumps(parsed)
    assert loads(encoded)['n']==Decimal(literal)
    assert '"n":"' not in encoded


@pytest.mark.parametrize('raw',['{"n":NaN}','{"n":Infinity}','{"x":1,"x":2}','{} {}','{','{"x":{"a":1,"a":2}}'])
def test_bad_json_has_safe_decode_error(raw):
    with pytest.raises(json.JSONDecodeError) as caught:loads(raw)
    assert caught.value.doc=='' and raw not in str(caught.value)


@pytest.mark.parametrize('value',[Decimal('NaN'),Decimal('Infinity'),float('nan'),float('inf')])
def test_nonfinite_values_cannot_be_encoded(value):
    with pytest.raises(ValueError):dumps({'n':value})


def test_existing_json_bytes_and_reference_spacing_are_unchanged():
    value={'z':['中文',True,None,1.0],'a':{'x':'\n'}}
    assert dumps(value,sort_keys=True)==json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    assert dumps(value,compact=False)==json.dumps(value,ensure_ascii=False)
    assert loads(dumps(value))==value


def test_objects_sort_keys_but_keep_array_order_and_string_whitespace():
    value={'z':[2,1],'a':{'z':' a ','a':Decimal('0.1')}}
    assert dumps(value,sort_keys=True)=='{"a":{"a":0.1,"z":" a "},"z":[2,1]}'


def test_non_json_object_keys_are_rejected():
    with pytest.raises(TypeError):dumps({1:'wrong'})


def test_large_integer_literal_does_not_depend_on_python_int_string_limit():
    literal='9'*5000
    value=loads('{"n":'+literal+'}')
    assert value['n']==Decimal(literal)
    assert dumps(value)=='{"n":'+literal+'}'


def test_encoder_handles_deep_json_accepted_by_the_parser():
    literal='{"x":'+'['*600+'0'+']'*600+'}'
    assert dumps(loads(literal))==literal
