"""JSON numbers stay decimal across HTTP, Schema, counting and Provider wire."""
from decimal import Decimal, DecimalException
import json


def _decimal(value):
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError('JSON numbers must be finite')
    return number


def _integer(value):
    try:
        return int(value)
    except ValueError:
        # Preserve large JSON integer literals without changing Python's global
        # security setting or expanding an exponent into a giant binary int.
        return _decimal(value)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON object key')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('Non-standard JSON constant')


def loads(value: str | bytes):
    try:
        return json.loads(value, parse_int=_integer, parse_float=_decimal, parse_constant=_invalid_constant,
                          object_pairs_hook=_object)
    except (ValueError, UnicodeError, DecimalException, RecursionError):
        # FastAPI recognises JSONDecodeError as invalid_request. Do not retain
        # private input or the original parser exception in diagnostics.
        raise json.JSONDecodeError('Invalid JSON', '', 0) from None


def dumps(value, *, sort_keys: bool = False, compact: bool = True) -> str:
    comma, colon = (',', ':') if compact else (', ', ': ')
    parts = []
    stack = [('value', value)]
    active = set()
    while stack:
        kind, item = stack.pop()
        if kind == 'raw':
            parts.append(item)
            continue
        if kind == 'close':
            token, identity = item
            parts.append(token)
            active.remove(identity)
            continue
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise ValueError('JSON numbers must be finite')
            parts.append(str(item))
        elif isinstance(item, (dict, list)):
            identity = id(item)
            if identity in active:
                raise ValueError('Circular JSON value')
            active.add(identity)
            mapping = isinstance(item, dict)
            parts.append('{' if mapping else '[')
            stack.append(('close', ('}' if mapping else ']', identity)))
            if mapping:
                if any(not isinstance(key, str) for key in item):
                    raise TypeError('JSON object keys must be strings')
                keys = sorted(item) if sort_keys else list(item)
                for index in range(len(keys) - 1, -1, -1):
                    key = keys[index]
                    stack.extend([('value', item[key]), ('raw', colon), ('value', key)])
                    if index:
                        stack.append(('raw', comma))
            else:
                for index in range(len(item) - 1, -1, -1):
                    stack.append(('value', item[index]))
                    if index:
                        stack.append(('raw', comma))
        else:
            parts.append(json.dumps(item, ensure_ascii=False, allow_nan=False))
    return ''.join(parts)
