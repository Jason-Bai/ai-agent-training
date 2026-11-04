"""Explicit real-supplier concurrency experiment and ten-minute sustained run."""
import asyncio
from datetime import datetime, timezone
import json
from time import monotonic

import pytest

from f10_metrics import summarize, validate_records
from f13_capacity_metrics import validate_gateway_outcomes
from f13_capacity_support import f13_capacity_environment


pytestmark = pytest.mark.live
KINDS = [(model, mode) for model in ('general-primary', 'general-backup', 'light', 'general', 'analysis', 'auto-general')
         for mode in ('text', 'structured', 'streaming')]
ACTIVE = ('active_generations', 'subscriptions', 'cleanup_tasks', 'cancellations', 'active_requests')


async def drained_sample(env):
    after = datetime.now(timezone.utc)
    async with asyncio.timeout(8):
        while True:
            sample = await env.sample()
            captured = sample.get('snapshot_at')
            fresh = captured is not None and datetime.fromisoformat(captured) >= after
            assert sample.get('observation_errors',0)==0, 'test observation failed'
            if fresh and all(key in sample and sample[key] == 0 for key in ACTIVE):
                assert sample['agent_requests'] == {}, 'Agent concurrency permits were not returned'
                assert sample['connections'] <= 6, 'database pool failed to return to configured ceiling'
                return sample
            await asyncio.sleep(.25)


async def phase(env, concurrency, duration, report):
    records, samples, verified = [], [], {}
    report.update(concurrency=concurrency, started_at=datetime.now(timezone.utc).isoformat(),
                  records=records, resource_samples=samples, verified=verified)
    before = await drained_sample(env)
    report['before'] = before
    started = monotonic()
    stop = asyncio.Event()
    sequence = 0

    async def slot(index):
        nonlocal sequence
        headers = env.headers if index % 2 == 0 else env.other_headers
        while monotonic() - started < duration:
            number = sequence
            sequence += 1
            model, mode = KINDS[number % len(KINDS)]
            dispatch_at = monotonic() - started
            result = await env.call(model, mode, headers=headers,quality_gate=False)
            result.update(model=model, mode=mode, agent='a' if index % 2 == 0 else 'b',
                          slot=index, dispatch_after_seconds=dispatch_at)
            records.append(result)

    async def monitor():
        while not stop.is_set():
            sample = await env.sample()
            assert sample['active_requests'] <= env.concurrency['max_requests']
            assert all(count <= env.concurrency['max_requests_per_agent'] for count in sample['agent_requests'].values())
            samples.append(sample)
            try:
                await asyncio.wait_for(stop.wait(), 5)
            except TimeoutError:
                pass

    monitor_task = asyncio.create_task(monitor())
    workers = [asyncio.create_task(slot(index)) for index in range(concurrency)]
    try:
        await asyncio.gather(*workers)
    finally:
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        stop.set()
        await monitor_task
    elapsed = monotonic() - started
    records.sort(key=lambda row: row['dispatch_after_seconds'])
    report.update(summarize(records, dispatch_seconds=duration, elapsed_seconds=elapsed))
    report['completed_at'] = datetime.now(timezone.utc).isoformat()
    # Cost and lifecycle correctness are checked independently of whether this
    # exploratory load tier was stable. Overload cannot excuse inconsistent rows.
    for record in records:
        verified[record['request_id']] = await env.verify(record['request_id'], expected_agent=record['agent'])
    validate_records(records, verified)
    await env.assert_balances()
    after = await drained_sample(env)
    for record in records:
        if record['status']=='failed':
            text=''.join(e[2]['delta'] for e in record['events'] if e[1]=='response.delta') if record['mode']=='streaming' else None
            verified[record['request_id']]['external_failure_evidence']=await env.failure_evidence(
                record['request_id'],drained=after,downstream_text=text)
    assert records, 'no business calls entered the load window'
    assert set(report['groups']) == {model + '/' + mode for model, mode in KINDS}, 'missing protocol/mode samples'
    report.update(after=after,
                  accounts={'a': await env.account_totals(env.headers), 'b': await env.account_totals(env.other_headers)})
    return report


async def test_real_capacity_and_ten_minute_sustained(f13_capacity_environment, record_property):
    env = f13_capacity_environment
    report = {'scope': 'single Gateway worker; DeepSeek Chat/Responses, three Bailian models and auto routing',
              'environment': env.metadata(), 'phase_seconds': 60, 'sustained_seconds': 600, 'phases': [], 'sustained': None,
              'acceptance_criterion': 'gateway_outcomes_v1', 'claims_100_percent_stability': False}
    try:
        for concurrency in (1, 5, 10, 20):
            result = {}
            report['phases'].append(result)
            await phase(env, concurrency, 60, result)
            (env.directory/'f10-capacity-progress.json').write_text(json.dumps(report, ensure_ascii=False))
            validate_gateway_outcomes(result, timeout_seconds=report['environment']['request_timeout_seconds'])
        measured = [item['concurrency'] for item in report['phases']]
        report['selected_concurrency'] = max(measured)
        report['selection_criterion'] = 'highest_measured_tier_with_correct_gateway_handling'
        report['sustained'] = {}
        await phase(env, max(measured), 600, report['sustained'])
        validate_gateway_outcomes(report['sustained'], timeout_seconds=report['environment']['request_timeout_seconds'])
        report['outcome'] = 'passed'
    finally:
        report.setdefault('outcome', 'failed')
        # Contains request IDs, generated test text and metadata, never headers
        # or credentials. JUnit retains the report even after test DB cleanup.
        rendered = json.dumps(report, ensure_ascii=False)
        (env.directory / 'f10-capacity.json').write_text(rendered)
        record_property('f10_capacity_report', rendered)
