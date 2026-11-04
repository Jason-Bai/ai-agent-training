"""F13 capacity acceptance, after the phase's SQL/price/event verification."""
import math

from f10_metrics import UPSTREAM_AVAILABILITY, validate_records


def validate_gateway_outcomes(report, *, timeout_seconds):
    """Verify handling without certifying the cause of dependency failures.

    The caller retains the phase's raw forwarding, SQL, independent pricing,
    account and event checks. Availability failures stay in the original report.
    """
    rows, observed = report['records'], report['verified']
    validate_records(rows, observed)
    assert any(row['status']=='completed' for row in rows), 'no completed capacity baseline'
    after = report['after']
    assert after['observation_errors']==0, 'capacity observation failed'
    assert not after['agent_requests'], 'Agent permits were not released'
    for field in ('active_requests','active_generations','subscriptions','cancellations','cleanup_tasks'):
        assert after[field]==0, 'capacity resources were not released'
    for row in rows:
        evidence = observed[row['request_id']]
        timing = evidence['server_timing']
        assert evidence['error_code']==row['error_code'], 'response/database error mismatch'
        assert timing['cleanup_error_code'] is None, 'provider cleanup failed'
        assert timing['delivery_error_code'] is None, 'response delivery failed'
        assert evidence['attempts']>=1 and len(evidence['bill_states'])==evidence['attempts'], 'missing billing attempt'
        assert all(state in {'settled','pending'} for state in evidence['bill_states']), 'unsettled billing attempt'
        elapsed = row['elapsed_ms']-(row.get('create_ms') or 0)
        assert math.isfinite(elapsed) and 0<=elapsed<=timeout_seconds*1000+2000, 'request exceeded total deadline'
        if row['status']=='completed':
            assert row['error_code'] is None and timing['error_stage'] is None, 'inconsistent success'
        else:
            assert row['error_code'] in UPSTREAM_AVAILABILITY | {'request_timeout'}, 'unexpected capacity failure'
            assert timing['error_stage']=='provider', 'Gateway-local failure'
            if row['error_code']=='request_timeout':
                assert elapsed>=timeout_seconds*1000-1000, 'premature total deadline'
