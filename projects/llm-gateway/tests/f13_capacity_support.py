from pricing_support import write_pricing
"""Owned six-entry real capacity harness; no private config read at collection."""
import asyncio
from copy import deepcopy
from decimal import Decimal, localcontext
import json

from psycopg.rows import dict_row
import pytest_asyncio
import yaml

from f10_support import BOOTSTRAP, CredentialHeaders, Environment
from f13_real_support import configuration, real_prices
from live.test_real_providers import real_registry
from postgres_support import PostgresServer, migrate
from services.credentials import load_credentials
from services.models import DEFAULT_CONFIG_DIR
from upstream_availability import classify_upstream_failure


def mixed_configuration(server, name):
    value = configuration(server, name)
    legacy = real_registry()
    value['providers'].update({key: item.model_dump(exclude_none=True)
                               for key, item in legacy.providers.items()})
    value['endpoints'].update({key: item.model_dump(mode='json')
                               for key, item in legacy.endpoints.items()})
    value['model_groups'].update({key: item.model_dump(mode='json', exclude_none=True)
                                  for key, item in legacy.model_groups.items()})
    value['concurrency'] = dict(max_requests=64, max_requests_per_agent=64)
    return value


def mixed_prices():
    value = real_prices()
    value['bindings'].update({'deepseek-main': 'deepseek', 'openai-main': 'deepseek'})
    value['versions'].append(dict(supplier='deepseek', model='deepseek-flash',
        version='flash-cny-20261103', effective_from='2026-11-03T00:00:00+08:00',
        peak=dict(hit='0.04', miss='2', output='8'),
        off_peak=dict(hit='0.02', miss='1', output='4'),
        calendar_until='2027-01-01', holidays=[]))
    return value


def raw_answer(raw, protocol):
    """Independently decode retained supplier JSON/SSE, never the typed answer."""
    def answer(value):
        if protocol == 'chat_completions':
            return value['choices'][0]['message']['content']
        return ''.join(part['text'] for item in value['output'] if item.get('type') == 'message'
                       for part in item.get('content', []) if part.get('type') == 'output_text')
    if raw.lstrip().startswith('{'):
        return answer(json.loads(raw))
    data, parts, final = [], [], None
    for line in raw.splitlines():
        if line.startswith('data:'):
            data.append(line.partition(':')[2].lstrip())
        elif not line and data:
            encoded = '\n'.join(data); data = []
            if encoded == '[DONE]':
                continue
            value = json.loads(encoded)
            if protocol == 'chat_completions':
                for choice in value.get('choices', []):
                    text = choice.get('delta', {}).get('content')
                    if isinstance(text, str): parts.append(text)
            elif value.get('type') == 'response.output_text.delta':
                parts.append(value['delta'])
            elif value.get('type') == 'response.completed':
                final = answer(value['response'])
    assert not data, 'incomplete raw supplier SSE frame'
    if protocol == 'responses':
        assert final is not None, 'raw supplier Responses terminal missing'
        if parts: assert ''.join(parts) == final, 'raw supplier deltas/final disagree'
        return final
    return ''.join(parts)


class MixedEnvironment(Environment):
    async def rows(self, rid):
        def read():
            with self.pg.connect(self.name, row_factory=dict_row) as sql:
                row = sql.execute('SELECT * FROM tb_llm_request WHERE request_id=%s', (rid,)).fetchone()
                attempts = sql.execute('SELECT * FROM tb_llm_attempt WHERE request_pk=%s ORDER BY attempt_no',
                                       (row['id'],)).fetchall()
                bills = sql.execute('SELECT * FROM tb_billing_attempt WHERE request_pk=%s ORDER BY attempt_no',
                                    (row['id'],)).fetchall()
            return row, attempts, bills
        return await asyncio.to_thread(read)

    async def upstream_answer(self, rid, model, mode):
        row, attempts, _ = await self.rows(rid)
        purposes = json.loads(row['timing_snapshot'])['attempt_purposes']
        generation = [item for item in attempts if item['status'] == 'completed'
                      and purposes.get(str(item['attempt_no']), 'generation') == 'generation']
        assert generation, 'completed request lacks a completed generation Attempt'
        selected = generation[-1]
        path = self.directory / ('availability-' + rid + '.json')
        async with asyncio.timeout(3):
            while not path.exists(): await asyncio.sleep(.01)
        document = json.loads(path.read_text())
        observed = next(item for item in document['attempts']
                        if item['attempt_no'] == selected['attempt_no'])
        assert observed['protocol'] == selected['api_protocol']
        assert observed['http_status'] == 200 and observed['read_closed'] is True
        text = raw_answer(observed['raw_text'], selected['api_protocol'])
        assert text == observed['typed_text'], 'raw supplier/typed generation text mismatch'
        return text, dict(request_id=rid, generation_attempt_no=selected['attempt_no'],
                          source='original_upstream_bytes', observation=document)

    async def verify(self, rid, *, expected_agent=None):
        verified = await super().verify(rid, expected_agent=expected_agent)
        row, attempts, bills = await self.rows(rid)
        metrics = json.loads(row['timing_snapshot'])
        config = yaml.safe_load((self.config / 'gateway.yaml').read_text())
        purposes = metrics.get('attempt_purposes', {})
        auto = metrics.get('auto_routing')
        group = row['requested_model']
        if group == 'auto-general':
            assert auto and auto['profile'] == group
            group = auto['resolved_group']
            if row['status'] == 'completed':
                assert group in config['auto_routes']['auto-general']['candidate_groups']
                assert auto['source'] in {'classifier', 'default_fallback', 'single_eligible_group'}
        assert [a['attempt_no'] for a in attempts] == list(range(1, len(attempts) + 1))
        for attempt, bill in zip(attempts, bills):
            purpose = purposes.get(str(attempt['attempt_no']), 'generation')
            assert purpose in {'classification', 'generation'}
            selected = config['auto_routes']['auto-general']['classifier_group'] if purpose == 'classification' else group
            assert attempt['model'] in config['model_groups'][selected]['candidates']
            endpoint = config['endpoints'][attempt['model']]
            provider = config['providers'][endpoint['provider']]
            assert (attempt['provider'], attempt['adapter'], attempt['api_protocol'], attempt['configured_model']) == (
                endpoint['provider'], provider['adapter'], provider['api_protocol'], endpoint['model_id'])
            if purpose == 'classification':
                assert row['requested_model'] == 'auto-general' and attempt['max_output_tokens'] == 128
            if bill['state'] != 'settled' or bill['quote'] is None:
                continue  # The inherited checks retain unknown Usage/cost as pending.
            quote = bill['quote']; rates = quote
            if quote.get('input_tiers'):
                tier = next(t for t in quote['input_tiers'] if attempt['input_tokens'] <= t['max_input_tokens'])
                rates = tier['off_peak' if quote['period'] == 'off_peak' else 'peak']
            cache = bill['cached_input_tokens']
            if cache is None:
                assert rates['hit'] == rates['miss'] or attempt['input_tokens'] == 0
                cache = 0
            with localcontext() as context:
                context.prec = 80
                actual = (cache * Decimal(rates['hit']) + (attempt['input_tokens'] - cache) * Decimal(rates['miss'])
                          + attempt['output_tokens'] * Decimal(rates['output'])) / 1_000_000
            assert actual == bill['agent_charge'] == bill['supplier_cost']
        verified.update(auto_routing=auto, attempt_purposes=purposes)
        return verified

    async def failure_evidence(self, rid, *, drained, downstream_text=None):
        direct = await super().failure_evidence(rid, drained=drained, downstream_text=downstream_text)
        if direct is not None:
            return direct
        response = await self.client.get('/v1/traces/' + rid)
        if response.status_code == 404:
            response = await self.client.get('/v1/traces/' + rid, headers=self.other_headers)
        assert response.status_code == 200
        trace = response.json()['data']
        classifiers = [a for a in trace['attempts'] if a['purpose'] == 'classification']
        generation = [a for a in trace['attempts'] if a['purpose'] == 'generation']
        # A failed classifier is deliberately not exempted through this view.
        if not classifiers or any(a['status'] != 'completed' or a['usage'] is None for a in classifiers):
            return None
        if not generation or any(a['status'] != 'failed' or a['usage'] is not None for a in generation):
            return None
        path = self.directory / ('availability-' + rid + '.json')
        if not path.exists(): return None
        observation = json.loads(path.read_text())
        originals = {a['attempt_no']: a for a in observation['attempts']}
        if any(a['attempt_no'] not in originals for a in trace['attempts']): return None
        for item in classifiers:
            raw = originals[item['attempt_no']]
            if raw['http_status'] != 200 or raw['read_closed'] is not True: return None
            if raw_answer(raw['raw_text'], item['api_protocol']) != raw['typed_text']: return None
        mapping = {index: item['attempt_no'] for index, item in enumerate(generation, 1)}
        derived = deepcopy(trace)
        derived['attempts'] = [dict(item, attempt_no=index) for index, item in enumerate(generation, 1)]
        derived['usage'] = dict(known_subtotal=None, unknown_attempts=list(mapping))
        view = dict(observation, attempts=[dict(originals[number], attempt_no=index) for index, number in mapping.items()],
                    drained=drained, accounting_verified=True, lifecycle_verified=True)
        certificate = classify_upstream_failure(derived, view, downstream_text=downstream_text)
        if certificate is not None:
            certificate.update(evidence_scope='derived_generation_view', original_attempt_numbers=mapping,
                classification_attempts=[a['attempt_no'] for a in classifiers],
                original_observation_file=path.name)
            (self.directory / ('capacity-failure-view-' + rid + '.json')).write_text(json.dumps(
                dict(original_trace=trace, original_observation=observation, derived_trace=derived,
                     derived_observation=view, certificate=certificate), ensure_ascii=False))
        return certificate

    def metadata(self):
        value = super().metadata()
        config = yaml.safe_load((self.config / 'gateway.yaml').read_text())
        value.update(models=config['endpoints'], model_groups=config['model_groups'],
                     auto_routes=config['auto_routes'], recovery=config['recovery'],
                     provider_host=['api.deepseek.com', 'dashscope.aliyuncs.com'],
                     actual_configured_model='mixed; see models', price_versions=mixed_prices()['versions'])
        return value


@pytest_asyncio.fixture
async def f13_capacity_environment(request, tmp_path):
    assert request.config.getoption('--run-live'), 'capacity requires explicit --run-live'
    pg = await asyncio.to_thread(lambda: PostgresServer().start())
    name = pg.create_database()
    env = MixedEnvironment(pg, name, tmp_path)
    env.config.mkdir()
    keyfile = env.config / 'credentials.yaml'
    try:
        (env.config / 'gateway.yaml').write_text(yaml.safe_dump(mixed_configuration(pg, name)))
        write_pricing(env.config, mixed_prices())
        keys = load_credentials(DEFAULT_CONFIG_DIR / 'credentials.yaml', {'deepseek-main-key', 'bailian-main-key'})
        keyfile.touch(mode=0o600)
        keyfile.write_text(yaml.safe_dump({'credentials': {
            ref: {'api_key': secret.get_secret_value()} for ref, secret in keys.items()}}))
        (tmp_path / 'f10_bootstrap.py').write_text(BOOTSTRAP)
        await asyncio.to_thread(migrate, env.config)
        for agent in ('a', 'b'):
            await env.cli('create-agent', agent, 'F13 mixed capacity ' + agent)
            token = await env.cli('issue', agent)
            headers = CredentialHeaders(Authorization='Bearer ' + token)
            if agent == 'a': env.headers = headers
            else: env.other_headers = headers
            await env.cli('deposit', agent, '10000', '--operation-id', 'initial-' + agent)
            await env.cli('deposit', agent, '10000', '--operation-id', 'initial-' + agent)
        await env.start()
        yield env
    finally:
        try:
            await env.stop()
        finally:
            keyfile.unlink(missing_ok=True)
            if env.listener is not None: env.listener.close()
            try: await asyncio.to_thread(pg.drop_database, name)
            finally: await asyncio.to_thread(pg.close)
