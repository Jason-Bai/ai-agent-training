"""Bounded platform Schema subset and strict, non-coercing output acceptance."""
from dataclasses import dataclass
from copy import deepcopy
from hashlib import sha256
from decimal import Decimal
import math
import re
from urllib.parse import unquote

from jsonschema import Draft202012Validator, SchemaError
from jsonschema.validators import extend
from referencing import Registry
from referencing.exceptions import NoSuchResource
from starlette import status as http_status

from constants import (
    MAX_SCHEMA_BYTES, MAX_SCHEMA_EXPANDED_DEPTH, MAX_SCHEMA_EXPANDED_NODES,
    MAX_SCHEMA_RAW_DEPTH, MAX_SCHEMA_RAW_NODES,
)
from errors import GatewayError
from utils.json_codec import dumps, loads


KEYWORDS = frozenset({
    '$schema', '$defs', '$ref', 'type', 'properties', 'required',
    'additionalProperties', 'items', 'minItems', 'maxItems', 'minLength',
    'maxLength', 'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum',
    'enum', 'const', 'anyOf', 'title', 'description', 'default',
})
TYPES = frozenset({'object', 'array', 'string', 'integer', 'number', 'boolean', 'null'})
DIALECT = 'https://json-schema.org/draft/2020-12/schema'


@dataclass(frozen=True, repr=False)
class PreparedSchema:
    schema: dict
    canonical: str
    fingerprint: str
    raw_nodes: int
    expanded_nodes: int


def _invalid_schema():
    return GatewayError('invalid_response_schema', 'response_schema 不符合平台支持规则', http_status.HTTP_400_BAD_REQUEST)


def _raw_resources(schema):
    # Bound depth before calling recursive serializers or the standard validator.
    stack = [(iter((schema,)), 1)]
    lower_bound_bytes = 0
    while stack:
        values, depth = stack[-1]
        try:
            value = next(values)
        except StopIteration:
            stack.pop()
            continue
        # At least one encoded byte per JSON value; stop before walking a huge
        # annotation array or allocating a full serialized copy.
        lower_bound_bytes += 1
        if isinstance(value, (dict, list)):
            if depth > MAX_SCHEMA_RAW_DEPTH:
                raise ValueError
            if isinstance(value, dict):
                for key in value:
                    if not isinstance(key, str):
                        raise ValueError
                    lower_bound_bytes += len(key.encode('utf-8'))
                    if lower_bound_bytes > MAX_SCHEMA_BYTES:
                        raise ValueError
                children = value.values()
            else:
                children = value
            # The iterator keeps memory bounded by depth rather than width.
            stack.append((iter(children), depth + 1))
        elif isinstance(value, str):
            lower_bound_bytes += len(value.encode('utf-8'))
        elif isinstance(value, Decimal) and not value.is_finite():
            raise ValueError
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError
        elif value is not None and not isinstance(value, (bool, int, float, Decimal)):
            raise ValueError
        if lower_bound_bytes > MAX_SCHEMA_BYTES:
            raise ValueError


def _children(node, path):
    children = []
    for keyword in ('properties', '$defs'):
        if keyword in node:
            if not isinstance(node[keyword], dict):
                raise ValueError
            children.extend((path + (keyword, name), child, keyword == '$defs')
                            for name, child in node[keyword].items())
    if 'items' in node:
        children.append((path + ('items',), node['items'], False))
    if 'anyOf' in node:
        if not isinstance(node['anyOf'], list):
            raise ValueError
        children.extend((path + ('anyOf', str(index)), child, False)
                        for index, child in enumerate(node['anyOf']))
    return children


def _schema_nodes(schema):
    nodes = {}
    stack = [((), schema)]
    while stack:
        path, node = stack.pop()
        if not isinstance(node, dict) or set(node) - KEYWORDS:
            raise ValueError
        if len(nodes) >= MAX_SCHEMA_RAW_NODES:
            raise ValueError
        nodes[path] = node
        if 'type' in node and (not isinstance(node['type'], str) or node['type'] not in TYPES):
            raise ValueError
        if '$schema' in node and (path or node['$schema'] != DIALECT):
            raise ValueError
        if 'additionalProperties' in node and type(node['additionalProperties']) is not bool:
            raise ValueError
        if '$ref' in node and set(node) - {'$ref', 'title', 'description', 'default'}:
            raise ValueError
        stack.extend((child_path, child) for child_path, child, _ in _children(node, path))
    return nodes


def _references(nodes):
    references = {}
    for path, node in nodes.items():
        if '$ref' not in node:
            continue
        ref = node['$ref']
        if not isinstance(ref, str) or not ref.startswith('#/'):
            raise ValueError
        pointer = unquote(ref[2:], errors='strict')
        tokens = pointer.split('/')
        if any(re.search(r'~(?![01])', token) for token in tokens):
            raise ValueError
        target = tuple(token.replace('~1', '/').replace('~0', '~') for token in tokens)
        if target not in nodes:
            raise ValueError
        references[path] = target
    return references


def _expanded_resources(nodes, references):
    count = 0
    # Active paths detect cycles; shared definitions are counted at every use.
    stack = [((), 0, frozenset(), False)]
    while stack:
        path, depth, active, counted_here = stack.pop()
        if path in active:
            raise ValueError
        active = active | {path}
        if path in references:
            stack.append((references[path], depth, active, counted_here))
            continue
        count += 1
        if count > MAX_SCHEMA_EXPANDED_NODES:
            raise ValueError
        node = nodes[path]
        # type/properties/items can describe a container without an explicit
        # type. anyOf and refs constrain the same JSON position, so their
        # container is counted at most once; properties/items enter new values.
        container = bool(node.get('type') in {'object', 'array'} or 'properties' in node or 'items' in node)
        if container and not counted_here:
            depth += 1
        if depth > MAX_SCHEMA_EXPANDED_DEPTH:
            raise ValueError
        for child_path, _, definition in _children(node, path):
            if definition:
                stack.append((child_path, 0, active, False))
            else:
                same_position = child_path[len(path)] == 'anyOf'
                stack.append((child_path, depth, active,
                              (counted_here or container) if same_position else False))

    return count


def _nullable_only(nodes, references):
    for path, node in nodes.items():
        if 'anyOf' not in node:
            continue
        branches = node['anyOf']
        if len(branches) != 2 or sum(branch == {'type': 'null'} for branch in branches) != 1:
            raise ValueError
        index = next(i for i, branch in enumerate(branches) if branch != {'type': 'null'})
        target = path + ('anyOf', str(index))
        # Cycle detection has already completed for every definition and use.
        while target in references:
            target = references[target]
        branch = nodes[target]
        if branch.get('type') not in TYPES - {'null'} or 'anyOf' in branch:
            raise ValueError


def _check_meta_schema(schema):
    # The standard meta-schema does not recognise Decimal as integer. Check
    # the four supported nonnegative-integer parameters exactly, then use a
    # small placeholder only in this private meta-validation copy. Never turn
    # an exponent such as 1E+1000000 into a million-digit Python integer.
    meta_schema = deepcopy(schema)
    for node in _schema_nodes(meta_schema).values():
        for keyword in ('minLength', 'maxLength', 'minItems', 'maxItems'):
            if keyword in node:
                value = node[keyword]
                if not _exact_integer(None, value) or value < 0:
                    raise ValueError
                node[keyword] = 0
    Draft202012Validator.check_schema(meta_schema)


def prepare_schema(schema: dict) -> PreparedSchema:
    """Validate before Provider access; preserve refs, annotations and array order."""
    try:
        if not isinstance(schema, dict) or schema.get('type') != 'object' or '$ref' in schema or 'anyOf' in schema:
            raise ValueError
        _raw_resources(schema)
        nodes = _schema_nodes(schema)
        references = _references(nodes)
        expanded_nodes = _expanded_resources(nodes, references)
        _nullable_only(nodes, references)
        _check_meta_schema(schema)
        canonical = dumps(schema, sort_keys=True)
        encoded = canonical.encode('utf-8')
        if len(encoded) > MAX_SCHEMA_BYTES:
            raise ValueError
    except (ValueError, TypeError, UnicodeError, SchemaError, RecursionError):
        raise _invalid_schema() from None
    return PreparedSchema(loads(canonical), canonical, sha256(encoded).hexdigest(),
                          len(nodes), expanded_nodes)


def _exact_integer(checker, value):
    if isinstance(value, Decimal):
        return value.is_finite() and value == value.to_integral_value()
    return Draft202012Validator.TYPE_CHECKER.is_type(value, 'integer')


ExactValidator = extend(Draft202012Validator, type_checker=
    Draft202012Validator.TYPE_CHECKER.redefine('integer', _exact_integer))


def _no_remote_resource(uri):
    raise NoSuchResource(ref=uri)


def validate_output(text: str, prepared: PreparedSchema) -> dict:
    """Parse one strict JSON value, then validate without coercion/default filling."""
    try:
        value = loads(text)
    except (ValueError, RecursionError):
        raise GatewayError('invalid_json', '模型输出不是合法 JSON', http_status.HTTP_502_BAD_GATEWAY) from None
    validator = ExactValidator(prepared.schema, registry=Registry(retrieve=_no_remote_resource))
    if not isinstance(value, dict) or not validator.is_valid(value):
        raise GatewayError('schema_validation_failed', '模型输出不符合 response_schema', http_status.HTTP_502_BAD_GATEWAY)
    return value
