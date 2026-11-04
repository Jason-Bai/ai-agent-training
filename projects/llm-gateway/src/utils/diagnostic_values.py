"""Representable metadata for one persisted Provider invocation."""
from contracts import Usage


def metadata_text(value) -> str | None:
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        return None
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        return None
    return value


def attempt_usage(value) -> Usage | None:
    if not isinstance(value, Usage):
        return None
    counts = tuple(getattr(value, field, None) for field in ('input_tokens', 'output_tokens', 'total_tokens'))
    if not all(type(count) is int and 0 <= count < 2**63 for count in counts):
        return None
    return value if counts[0] + counts[1] == counts[2] else None
