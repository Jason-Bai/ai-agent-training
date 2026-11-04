"""Capacity calculations and acceptance checks, independent of live transport."""
from collections import Counter, defaultdict
import math


# These classify resource/upstream availability during exploratory phases.
# Protocol, content and accounting failures always block acceptance.
CAPACITY_ERRORS = {
    'provider_rate_limited', 'provider_timeout', 'provider_unavailable',
    'provider_connection_error', 'request_timeout', 'storage_unavailable',
}


def distribution(values):
    samples = [value for value in values if value is not None]
    if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in samples):
        raise ValueError('invalid measured duration')
    samples.sort()
    result = {'samples': len(samples)}
    for name, fraction in [('p50', .50), ('p95', .95), ('p99', .99)]:
        result[name] = samples[math.ceil(len(samples) * fraction) - 1] if samples else None
    result['maximum'] = samples[-1] if samples else None
    return result


def summarize(records, *, dispatch_seconds, elapsed_seconds):
    if dispatch_seconds <= 0 or elapsed_seconds < dispatch_seconds:
        raise ValueError('invalid phase interval')
    groups = defaultdict(list)
    for record in records:
        groups[record['model'] + '/' + record['mode']].append(record)
    successful = sum(record['status'] == 'completed' for record in records)
    return {
        'quality_evaluated': sum(type(row.get('quality_ok')) is bool for row in records),
        'quality_anomalies': [row['request_id'] for row in records if row.get('quality_ok') is False],
        'started': len(records), 'successes': successful,
        'success_rate': successful / len(records) if records else None,
        'dispatch_seconds': dispatch_seconds, 'elapsed_including_drain_seconds': elapsed_seconds,
        'dispatch_rate_per_second': len(records) / dispatch_seconds,
        'drained_rate_per_second': len(records) / elapsed_seconds,
        'failures': dict(Counter(record['error_code'] for record in records if record['status'] != 'completed')),
        'groups': {key: dict(count=len(rows), tail_percentiles_limited=len(rows) < 100, failures=dict(Counter(
            row['error_code'] for row in rows if row['status'] != 'completed')),
            **{metric: distribution(row[metric] for row in rows)
               for metric in ('elapsed_ms', 'first_text_ms', 'create_ms')}) for key, rows in sorted(groups.items())},
    }


def validate_records(records, observed):
    identifiers = [record['request_id'] for record in records]
    assert len(set(identifiers)) == len(identifiers), 'duplicate business request ID'
    assert set(identifiers) == set(observed), 'missing observed request'
    for record in records:
        rid = record['request_id']
        assert observed[rid]['status'] == record['status'], 'response/database terminal mismatch'
        assert record['status'] == 'completed' or (
            record['status'] == 'failed' and record['error_code'] in CAPACITY_ERRORS
        ), f"unexpected capacity outcome {record['status']} / {record['error_code']}"


UPSTREAM_AVAILABILITY = {'provider_unavailable','provider_rate_limited',
                         'provider_timeout','provider_connection_error'}


def sustained_supported(report):
    if not any(row['status']=='completed' for row in report['records']):
        return False
    for row in report['records']:
        if row['status']=='completed':
            continue
        observed=report['verified'].get(row['request_id'],{})
        proof=observed.get('external_failure_evidence') or {}
        if (row['status']!='failed' or row['error_code'] not in UPSTREAM_AVAILABILITY | {'request_timeout'}
                or observed.get('error_code')!=row['error_code']
                or (observed.get('server_timing') or {}).get('error_stage')!='provider'
                or proof.get('request_id')!=row['request_id']
                or proof.get('kind') not in {'upstream_stream_stall','upstream_http_error'}):
            return False
    return True


def validate_sustained(report):
    assert sustained_supported(report), 'unverified upstream failure or Gateway failure'
