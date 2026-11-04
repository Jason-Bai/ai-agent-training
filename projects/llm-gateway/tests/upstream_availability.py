"""Test-only, fail-closed classification; callers must retain complete wire evidence.

A certificate proves one observed failure and local cleanup. It does not prove
supplier-wide downtime. Missing/unsupported evidence always remains unclassified.
"""
import json
import math


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _frames(raw):
    """Parse every completed SSE message, rejecting truncated data at EOF."""
    messages, data, name, keepalive = [], [], None, False
    for line in raw.removeprefix('\ufeff').splitlines():
        if not line:
            if data:
                messages.append((name, json.loads('\n'.join(data))))
            data, name = [], None
        elif line.startswith(':'):
            keepalive = True
        else:
            field, separator, value = line.partition(':')
            value = value.removeprefix(' ')
            if field == 'data' and separator:
                data.append(value)
            elif field == 'event' and separator:
                name = value
            elif field not in {'id', 'retry'}:
                raise ValueError('unrecognized SSE field')
    if data or name is not None:
        raise ValueError('unterminated SSE message')
    return messages, keepalive


def _unfinished_text(raw, protocol):
    messages, keepalive = _frames(raw)
    initial, deltas = False, []
    for name, payload in messages:
        if not isinstance(payload, dict) or payload.get('usage') is not None:
            raise ValueError('invalid message or reported usage')
        if protocol == 'responses':
            kind = payload.get('type')
            if name is not None and name != kind:
                raise ValueError('event/data mismatch')
            if kind in {'response.created', 'response.in_progress'}:
                body = payload.get('response')
                if (not isinstance(body, dict) or body.get('object') != 'response'
                        or body.get('status') != 'in_progress' or not isinstance(body.get('model'), str)
                        or not body['model'] or body.get('usage') is not None
                        or body.get('output') not in (None, [])):
                    raise ValueError('invalid initial response')
                initial = True
            elif kind == 'response.output_text.delta':
                if not initial or not isinstance(payload.get('delta'), str):
                    raise ValueError('invalid delta')
                deltas.append(payload['delta'])
            elif kind in {'response.output_item.added', 'response.content_part.added'}:
                item = payload.get('item' if kind == 'response.output_item.added' else 'part')
                if (not initial or not isinstance(item, dict)
                        or item.get('type') not in {'message', 'output_text'}
                        or item.get('status') not in {None, 'in_progress'}
                        or item.get('usage') is not None
                        or item.get('text') not in (None, '')
                        or item.get('content') not in (None, [])):
                    raise ValueError('invalid output item')
            else:
                # Even a completed frame the adapter missed must block exemption.
                raise ValueError('terminal or unsupported response event')
        else:
            if payload.get('object') != 'chat.completion.chunk' or payload.get('error') is not None:
                raise ValueError('invalid chat chunk')
            choices = payload.get('choices')
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError('invalid choices')
            choice = choices[0]
            if (not isinstance(choice, dict) or type(choice.get('index')) is not int
                    or choice['index'] != 0 or choice.get('finish_reason') is not None):
                raise ValueError('terminal or invalid choice')
            delta = choice.get('delta')
            if (not isinstance(delta, dict) or delta.get('role', 'assistant') != 'assistant'
                    or delta.get('tool_calls') or delta.get('refusal')):
                raise ValueError('invalid delta')
            text = delta.get('content')
            if text is not None and not isinstance(text, str):
                raise ValueError('non-text delta')
            initial = True
            deltas.append(text or '')
    text = ''.join(deltas)
    if not initial or not (text or keepalive):
        raise ValueError('no initial nonterminal progress')
    return text, len(messages), keepalive


def _error_body_only(raw):
    if not raw.strip():
        return True
    if any(line.startswith(('data:', 'event:')) for line in raw.splitlines()):
        return False
    try:
        body = json.loads(raw)
    except ValueError:
        # Non-JSON error pages (including reverse-proxy HTML) are legitimate.
        return not raw.lstrip().startswith(('{', '['))
    return (isinstance(body, dict) and isinstance(body.get('error'), (dict, str))
            and not any(key in body for key in ('output', 'choices', 'delta', 'content', 'usage'))
            and body.get('status') not in {'completed', 'in_progress', 'incomplete'})


def classify_upstream_failure(trace: dict, observation: dict, *, downstream_text: str | None = None) -> dict | None:
    """Certify one stalled Attempt or up to three explicit HTTP-error Attempts.

    ``accounting_verified`` and ``lifecycle_verified`` are results of independent
    SQL/event checks, never substitutes for raw/typed/delivery correspondence.
    Mixed/unknown failures, probes and EOF errors remain unclassified.
    """
    try:
        if (not isinstance(trace, dict) or not isinstance(observation, dict)
                or not trace['request_id'] or trace['request_id'] != observation['request_id']
                or trace['status'] != 'failed' or trace['error_stage'] != 'provider'
                or trace['operation'] not in {'completion', 'streaming'}
                or trace['delivery_error_code'] is not None
                or trace.get('cleanup_error_code') is not None
                or observation['accounting_verified'] is not True
                or observation['lifecycle_verified'] is not True):
            return None
        cancellation = trace['cancellation']
        if cancellation['cleanup_error_code'] is not None or cancellation['requested_at'] is not None:
            return None
        drained = observation['drained']
        if any(type(drained[key]) is not int or drained[key] != 0 for key in (
                'active_requests', 'active_generations', 'cleanup_tasks', 'cancellations', 'subscriptions')):
            return None
        if not isinstance(drained['agent_requests'], dict) or any(
                type(count) is not int or count != 0 for count in drained['agent_requests'].values()):
            return None
        count = len(trace['attempts'])
        if count != len(observation['attempts']) or not 1 <= count <= 3:
            return None
        if count > 1:
            # Reuse the complete single-Attempt checks for every HTTP failure;
            # never extrapolate evidence from the final Attempt to earlier ones.
            if (trace['usage']['unknown_attempts'] != list(range(1, count + 1))
                    or trace['attempts'][-1]['error_code'] != trace['error_code']):
                return None
            summaries = []
            for number, (item, observed) in enumerate(zip(trace['attempts'], observation['attempts']), 1):
                if (type(item['attempt_no']) is not int or item['attempt_no'] != number
                        or type(observed['attempt_no']) is not int or observed['attempt_no'] != number
                        or item['error_code'] not in {'provider_unavailable', 'provider_rate_limited'}):
                    return None
                one_trace = dict(trace, error_code=item['error_code'],
                                 attempts=[dict(item, attempt_no=1)],
                                 usage=dict(trace['usage'], unknown_attempts=[1]))
                one_observation = dict(observation, attempts=[dict(observed, attempt_no=1)])
                summary = classify_upstream_failure(one_trace, one_observation, downstream_text=downstream_text)
                if summary is None or summary['kind'] != 'upstream_http_error':
                    return None
                summaries.append(dict(summary, attempt_no=number))
            return dict(summaries[-1], attempt_count=count, attempts=summaries)
        attempt, wire = trace['attempts'][0], observation['attempts'][0]
        protocol = wire['protocol']
        if (protocol not in {'responses', 'chat_completions'} or attempt['api_protocol'] != protocol
                or type(attempt['attempt_no']) is not int or attempt['attempt_no'] != 1
                or type(wire['attempt_no']) is not int or wire['attempt_no'] != 1
                or attempt['status'] != 'failed' or attempt['provider_entered'] is not True
                or attempt['cleanup_error_code'] is not None
                or attempt['error_code'] != trace['error_code']
                or attempt['finish_reason'] is not None or attempt['upstream_finish_reason'] is not None
                or attempt['upstream_status'] not in {None, 'in_progress'}
                or attempt['usage'] is not None or trace['usage']['known_subtotal'] is not None
                or trace['usage']['unknown_attempts'] != [1]
                or wire['read_closed'] is not True
                or not isinstance(wire['raw_text'], str) or not isinstance(wire['typed_text'], str)):
            return None
        start, end, headers = wire['started_ms'], wire['ended_ms'], wire['http_headers_ms']
        if (not all(_number(value) for value in (start, end, headers))
                or not 0 <= start <= headers <= end):
            return None
        code, status = trace['error_code'], wire['http_status']
        result = {'request_id': trace['request_id'], 'attempt_no': 1, 'protocol': protocol,
                  'error_code': code, 'upstream_http_status': status, 'provider_elapsed_ms': end - start}
        if code == 'request_timeout':
            timeout = trace['timeout_seconds']
            duration = trace['timing']['request_duration_ms']
            if (trace['operation'] != 'streaming' or type(status) is not int or status != 200
                    or wire['read_cancelled'] is not True or not _number(timeout) or timeout <= 0
                    or not _number(duration) or not timeout * 1000 - 1000 <= duration <= timeout * 1000 + 2000
                    or not timeout * 1000 - 1000 <= end - start <= timeout * 1000 + 1000
                    or attempt['provider_error_code'] is not None or wire['provider_error_code'] is not None
                    or wire['upstream_http_status'] is not None or wire['transient'] is not None):
                return None
            text, count, keepalive = _unfinished_text(wire['raw_text'], protocol)
            if text != wire['typed_text'] or (downstream_text is not None and text != downstream_text):
                return None
            result.update(kind='upstream_stream_stall', raw_frame_count=count,
                          text_characters=len(text), keepalive_observed=keepalive)
        elif type(status) is int and status in {429, 500, 502, 503, 504}:
            expected = 'provider_rate_limited' if status == 429 else 'provider_unavailable'
            if (code != expected or wire['provider_error_code'] != expected
                    or attempt['provider_error_code'] not in {None, expected}
                    or wire['upstream_http_status'] != status or wire['transient'] is not (status != 429)
                    or wire['typed_text'] != '' or downstream_text not in {None, ''}
                    or not _error_body_only(wire['raw_text'])):
                return None
            result.update(kind='upstream_http_error', attempt_count=1)
        else:
            return None
        return result
    except (KeyError, TypeError, ValueError, IndexError):
        return None
