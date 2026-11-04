"""Explicit F13 follow-up: pytest tests/f13_capacity_followup.py --run-live.

Not collected by the default test_*.py pattern. This supplements, and never
replaces, the failed twenty-concurrent measurement in the full regression.
"""
import json

import pytest

from f13_capacity_support import f13_capacity_environment
from f13_capacity_metrics import validate_gateway_outcomes
from live.test_f10_capacity import phase


pytestmark = pytest.mark.live


async def test_five_concurrent_sustained_with_transport_observation(
        f13_capacity_environment, record_property):
    env = f13_capacity_environment
    bootstrap = env.directory / 'f10_bootstrap.py'
    source = bootstrap.read_text()
    original = '    install(app, ROOT, MonkeyPatch())\n'
    assert source.count(original) == 1
    source = source.replace(original,
        '    install(app, ROOT, MonkeyPatch())\n'
        '    from transport_observation import install as install_transport\n'
        '    install_transport(app, ROOT, MonkeyPatch())\n')
    marker = 'observation_errors=len(OBSERVATION_ERRORS)'
    assert source.count(marker) == 1
    source = source.replace(marker, marker + '+len(app.state.transport_observation_errors)')
    await env.stop()
    bootstrap.write_text(source)
    await env.start()
    report = dict(scope='user-confirmed five-concurrent follow-up; original failures retained',
                  environment=env.metadata(), sustained_seconds=600, sustained={},
                  acceptance_criterion='gateway_outcomes_v1', claims_100_percent_stability=False)
    try:
        await phase(env, 5, 600, report['sustained'])
        observations = {}
        for record in report['sustained']['records']:
            rid = record['request_id']
            document = json.loads((env.directory / ('transport-' + rid + '.json')).read_text())
            assert document['request_id'] == rid
            assert len(document['attempts']) == report['sustained']['verified'][rid]['attempts']
            observations[rid] = document
        report['transport_observations'] = observations
        validate_gateway_outcomes(report['sustained'], timeout_seconds=report['environment']['request_timeout_seconds'])
        report['outcome'] = 'passed'
    finally:
        report.setdefault('outcome', 'failed')
        rendered = json.dumps(report, ensure_ascii=False)
        (env.directory / 'f13-capacity-followup.json').write_text(rendered)
        record_property('f13_capacity_followup', rendered)
