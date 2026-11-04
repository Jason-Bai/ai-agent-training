from copy import deepcopy
import hashlib
import json

import pytest
from pydantic import BaseModel

from errors import GatewayError
from services.validation import prepare_schema, validate_output


def obj(**properties):
    return {'type': 'object', 'properties': properties}


def rejected(schema):
    with pytest.raises(GatewayError) as e:
        prepare_schema(schema)
    assert (e.value.code, e.value.status_code) == ('invalid_response_schema', 400)


@pytest.mark.parametrize('schema', [{}, {'type':'array'}, {'$ref':'#/$defs/X'}, {'type':'object','anyOf':[]}])
def test_invalid_root(schema):
    rejected(schema)


@pytest.mark.parametrize('keyword', ['oneOf','allOf','not','if','pattern','format','$id','$anchor','unknown','prefixItems'])
def test_unknown_keyword_rejected_even_unused_definition(keyword):
    rejected({'type':'object','$defs':{'Unused':{'type':'string',keyword:[]}}})


@pytest.mark.parametrize('field', [True,False,{'type':['string','null']},{'additionalProperties':{}},{'type':'array','items':[]},{'minLength':True},{'required':'x'},{'description':1}])
def test_invalid_keyword_types(field):
    rejected(obj(x=field))


def test_schema_canonicalization_and_copy():
    source=obj(z={'type':'string','description':' 中文 '}, a={'type':'integer','default':3})
    snapshot=deepcopy(source)
    p=prepare_schema(source)
    assert p.canonical==json.dumps(source,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    assert p.fingerprint==hashlib.sha256(p.canonical.encode()).hexdigest()
    assert prepare_schema(dict(reversed(list(source.items())))).fingerprint==p.fingerprint
    source['properties']['z']['type']='number'
    assert p.schema==snapshot
    assert validate_output('{}',p)=={}


def test_annotation_data_is_not_schema():
    prepare_schema(obj(format={'type':'string','default':{'format':'x','unknown':True},'enum':['format']}))


def test_schema_dialect():
    prepare_schema({'type':'object','$schema':'https://json-schema.org/draft/2020-12/schema'})
    rejected({'type':'object','$schema':'https://json-schema.org/draft-07/schema'})


def test_refs_and_node_counts():
    schema={'type':'object','$defs':{'Address':obj(street={'type':'string'})},'properties':{'a':{'$ref':'#/$defs/Address'},'b':{'$ref':'#/$defs/Address'}}}
    p=prepare_schema(schema)
    assert (p.raw_nodes,p.expanded_nodes)==(5,7)
    assert validate_output('{"a":{"street":"x"},"b":{"street":"y"}}',p)['a']['street']=='x'


def test_pointer_escapes_and_arbitrary_schema_target():
    p=prepare_schema({'type':'object','$defs':{'a~/b':{'type':'string'}},'properties':{'x':{'$ref':'#/$defs/a~0~1b'},'y':{'$ref':'#/properties/x'}}})
    assert validate_output('{"x":"a","y":"b"}',p)=={'x':'a','y':'b'}


@pytest.mark.parametrize('ref',['https://private.invalid/s','file:///private','#anchor','#/$defs/Missing','#/description','#/$defs/a~2b'])
def test_invalid_references(ref):
    rejected({'type':'object','description':'private','$defs':{'a~2b':{'type':'string'}},'properties':{'x':{'$ref':ref}}})


def test_cycles_including_unused():
    rejected({'type':'object','$defs':{'A':{'$ref':'#/$defs/B'},'B':{'$ref':'#/$defs/A'}}})
    rejected({'type':'object','properties':{'x':{'$ref':'#'}}})


def test_ref_siblings():
    base={'type':'object','$defs':{'X':{'type':'string'}},'properties':{'x':{'$ref':'#/$defs/X','description':'x','default':'x','title':'X'}}}
    prepare_schema(base)
    base['properties']['x']['minLength']=2
    rejected(base)


@pytest.mark.parametrize('branches', [[{'type':'string'},{'type':'integer'}],[{'type':'null'},{'type':'null'}],[{'type':'string'},{'type':'null'},{'type':'integer'}],[{}, {'type':'null'}]])
def test_general_unions_rejected(branches):
    rejected(obj(x={'anyOf':branches}))


def test_nullable_required_default_and_no_coercion():
    schema=obj(x={'anyOf':[{'type':'null'},{'type':'integer'}],'default':3})
    schema['required']=['x']
    p=prepare_schema(schema)
    assert validate_output('{"x":null}',p)=={'x':None}
    assert validate_output('{"x":1.0}',p)=={'x':1.0}
    for text in ['{}','{"x":true}','{"x":"1"}']:
        with pytest.raises(GatewayError) as e:validate_output(text,p)
        assert e.value.code=='schema_validation_failed'


@pytest.mark.parametrize('text',['{','{}x','{} {}','```json\n{}\n```','{"x":1,"x":2}','{"x":{"a":1,"a":2}}','{"x":NaN}','{"x":Infinity}','{"x":-Infinity}','{"x":1e999999999999999999999999}'])
def test_strict_json(text):
    with pytest.raises(GatewayError) as e:validate_output(text,prepare_schema(obj()))
    assert (e.value.code,e.value.status_code)==('invalid_json',502)
    assert text not in str(e.value)


@pytest.mark.parametrize('text',['[]','null','true','1','"text"'])
def test_valid_json_wrong_root_is_schema_error(text):
    with pytest.raises(GatewayError) as e:validate_output(text,prepare_schema(obj()))
    assert e.value.code=='schema_validation_failed'


def test_properties_and_additional_policy():
    p=prepare_schema(obj(x={'type':'string'}))
    assert validate_output(' {"extra":1} \n',p)=={'extra':1}
    p=prepare_schema({**obj(x={'type':'string'}),'additionalProperties':False})
    with pytest.raises(GatewayError):validate_output('{"extra":1}',p)


@pytest.mark.parametrize('rule,good,bad', [({'minLength':2},'中a','中'),({'maxLength':2},'中a','中ab'),({'minimum':2},2,1),({'maximum':2},2,3),({'exclusiveMinimum':2},3,2),({'exclusiveMaximum':2},1,2),({'enum':['a','b']},'a','c'),({'const':True},True,1),({'type':'array','items':{'type':'integer'},'minItems':1},[1],[]),({'type':'array','items':{'type':'integer'},'maxItems':1},[1],[1,2])])
def test_supported_constraints(rule,good,bad):
    p=prepare_schema(obj(x=rule))
    assert validate_output(json.dumps({'x':good}),p)=={'x':good}
    with pytest.raises(GatewayError) as e:validate_output(json.dumps({'x':bad}),p)
    assert e.value.code=='schema_validation_failed'


@pytest.mark.parametrize('size', [16383, 16384, 16385])
def test_size_exact_boundary_and_unicode(size):
    schema={'type':'object','description':'中'}
    overhead=len(json.dumps(schema,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())
    schema['description']+='a'*(size-overhead)
    if size > 16384:
        rejected(schema)
    else:
        assert len(prepare_schema(schema).canonical.encode())==size


def nested_objects(depth):
    result={'type':'object'}
    for _ in range(depth-1):result=obj(x=result)
    return result


@pytest.mark.parametrize('depth', [7, 8, 9])
def test_structure_depth(depth):
    schema=nested_objects(depth)
    rejected(schema) if depth > 8 else prepare_schema(schema)


@pytest.mark.parametrize('depth', [31, 32, 33])
def test_raw_depth(depth):
    data=0
    for _ in range(depth-1):data=[data]
    schema={'type':'object','default':data}
    rejected(schema) if depth > 32 else prepare_schema(schema)


@pytest.mark.parametrize('nodes', [255, 256, 257])
def test_raw_nodes(nodes):
    schema=obj(**{f'p{i}':{'type':'string'} for i in range(nodes-1)})
    if nodes > 256:
        rejected(schema)
    else:
        assert prepare_schema(schema).raw_nodes==nodes


@pytest.mark.parametrize('nodes', [255, 256, 257])
def test_expanded_nodes_boundaries(nodes):
    # root1 + definition2 + 126*2 uses + scalar1 == 256
    schema={'type':'object','$defs':{'X':obj(x={'type':'string'})},'properties':{f'p{i}':{'$ref':'#/$defs/X'} for i in range(126)}}
    for i in range(nodes-255):schema['properties'][f'extra{i}']={'type':'string'}
    if nodes > 256:
        rejected(schema)
    else:
        assert prepare_schema(schema).expanded_nodes==nodes


def test_referenced_structural_depth_and_unused_definition_depth():
    rejected({'type':'object','$defs':{'X':nested_objects(8)},'properties':{'x':{'$ref':'#/$defs/X'}}})
    rejected({'type':'object','$defs':{'Unused':nested_objects(9)}})


@pytest.mark.parametrize('value',[float('nan'),float('inf'),float('-inf')])
def test_nonfinite_schema(value):rejected(obj(x={'default':value}))


def test_pydantic_schema():
    class Product(BaseModel):
        name:str
        quantity:int
    class Order(BaseModel):
        product:Product
        note:str|None=None
    p=prepare_schema(Order.model_json_schema())
    assert validate_output('{"product":{"name":"充电器","quantity":1}}',p)['product']['quantity']==1


def test_large_shallow_annotation_rejected_before_serializing(monkeypatch):
    import services.validation as validation
    def no_full_serialization(*args, **kwargs):
        raise AssertionError('oversized raw data must be bounded before full serialization')
    monkeypatch.setattr(validation,'dumps',no_full_serialization)
    rejected({'type':'object','default':[0]*100000})


def test_implicit_object_structure_cannot_bypass_depth_limit():
    schema={'properties':{'p':{'type':'string'}}}
    for _ in range(8):schema={'properties':{'p':schema}}
    schema['type']='object'
    rejected(schema)


def test_implicit_array_structure_cannot_bypass_depth_limit():
    schema={'items':{'type':'string'}}
    for _ in range(8):schema={'items':schema}
    rejected({'type':'object','properties':{'p':schema}})


@pytest.mark.parametrize('field,text', [({'type':'integer'},'1.0000000000000001'),({'const':0},'1e-400')])
def test_exact_numbers_cannot_round_into_schema_acceptance(field,text):
    with pytest.raises(GatewayError) as e:
        validate_output('{"x":'+text+'}',prepare_schema(obj(x=field)))
    assert e.value.code=='schema_validation_failed'


def test_exact_decimal_return_and_finite_large_exponent():
    from decimal import Decimal
    p=prepare_schema(obj(x={'type':'number'}))
    for text in ['1.0000000000000001','1e-400','1e999']:
        value=validate_output('{"x":'+text+'}',p)['x']
        assert isinstance(value,Decimal)
        assert value==Decimal(text)
    assert validate_output('{"x":1.0}',prepare_schema(obj(x={'type':'integer'})))['x']==Decimal('1.0')


def test_exact_schema_numeric_limits():
    from decimal import Decimal
    p=prepare_schema(obj(x={'const':0.1}))
    assert validate_output('{"x":0.1}',p)['x']==Decimal('0.1')
    with pytest.raises(GatewayError):validate_output('{"x":0.10000000000000001}',p)


@pytest.mark.parametrize('field',[
    {'minItems':True},{'maxItems':-1},{'maxLength':'2'},{'minimum':True},
    {'maximum':'2'},{'exclusiveMinimum':False},{'exclusiveMaximum':None},
    {'enum':'values'},{'$defs':[]},{'properties':[]},{'title':3},
])
def test_keyword_value_types_remain_strict(field):
    rejected(obj(x=field))


def test_array_order_annotations_and_defaults_are_part_of_fingerprint():
    source=obj(x={'enum':['a','b'],'default':'a','description':' a '})
    baseline=prepare_schema(source).fingerprint
    for change in [{'enum':['b','a']},{'default':'b'},{'description':'a'}]:
        variant=deepcopy(source);variant['properties']['x'].update(change)
        assert prepare_schema(variant).fingerprint!=baseline


def test_nullable_reference_and_pydantic_unsupported_exports():
    from datetime import datetime
    p=prepare_schema({'type':'object','$defs':{'X':obj(x={'type':'string'})},
                      'properties':{'x':{'anyOf':[{'$ref':'#/$defs/X'},{'type':'null'}]}}})
    assert validate_output('{"x":null}',p)=={'x':None}
    assert validate_output('{"x":{"x":"ok"}}',p)['x']=={'x':'ok'}
    class WithFormat(BaseModel):
        at:datetime
    class WithUnion(BaseModel):
        item:str|int|None
    class Tree(BaseModel):
        children:list['Tree']
    for model in [WithFormat,WithUnion,Tree]:rejected(model.model_json_schema())


def test_nullable_branch_shares_its_wrappers_container_depth():
    # The wrapper and its object branch constrain the same JSON object.
    # Root1 + nested object branch7 is exactly the supported depth8.
    nullable={'type':'object','anyOf':[nested_objects(7),{'type':'null'}]}
    p=prepare_schema(obj(p=nullable))
    assert p.raw_nodes==10  # root + wrapper + 7 objects + null
    nullable['anyOf'][0]=nested_objects(8)
    rejected(obj(p=nullable))


def test_decimal_schema_preserves_exact_constraints_and_annotations():
    from decimal import Decimal
    exact=Decimal('0.10000000000000001')
    schema=obj(x={'type':'number','const':exact,'minimum':exact,'maximum':exact,
                  'enum':[exact],'default':exact})
    p=prepare_schema(schema)
    assert p.schema['properties']['x']['const']==exact
    assert validate_output('{"x":0.10000000000000001}',p)['x']==exact
    for value in ['0.1','0.10000000000000002']:
        with pytest.raises(GatewayError):validate_output('{"x":'+value+'}',p)
    reordered={'properties':schema['properties'],'type':'object'}
    assert prepare_schema(reordered).fingerprint==p.fingerprint
    assert '0.10000000000000001' in p.canonical


@pytest.mark.parametrize('keyword',['minLength','maxLength','minItems','maxItems'])
def test_decimal_integer_meta_constraints_preserve_value(keyword):
    from decimal import Decimal
    p=prepare_schema(obj(x={keyword:Decimal('1.0')}))
    assert p.schema['properties']['x'][keyword]==Decimal('1.0')
    assert '1.0' in p.canonical
    for invalid in [Decimal('1.1'),Decimal('-1'),True]:
        rejected(obj(x={keyword:invalid}))


def test_decimal_huge_integer_meta_limit_is_not_expanded():
    from decimal import Decimal
    huge=Decimal('1e1000000')
    p=prepare_schema(obj(x={'type':'string','maxLength':huge}))
    assert p.schema['properties']['x']['maxLength']==huge
    assert len(p.canonical)<200
    assert validate_output('{"x":"ok"}',p)=={'x':'ok'}


@pytest.mark.parametrize('number',['NaN','Infinity','-Infinity'])
def test_nonfinite_decimal_schema_rejected(number):
    from decimal import Decimal
    rejected(obj(x={'default':Decimal(number)}))
