"""Controlled local classification 503 followed by actual Bailian Plus output.

The local failure is not a supplier incident. The failed classifier's consumption
stays unknown even though its controlled server never calls a supplier.
"""
from dataclasses import replace
from decimal import Decimal, localcontext
import json

import pytest

from f13_real_support import (MODELS, PRICE_VERSION, real_prices, f13_live_pg,
                              f13_real_environment, save_evidence)
from providers.bailian import BailianProvider
from services.models import ProviderConfig
from services.prices import Catalog, PriceBook
from test_f12_real_routing import upstream

pytestmark = pytest.mark.live


def test_controlled_classifier_503_generates_once_with_actual_plus_default(f13_real_environment,record_property):
    env = f13_real_environment; state = env.application.state
    previous_registry = state.model_registry; previous_providers = state.providers
    previous_prices = state.storage.prices
    with upstream(503,{}) as (url,calls):
        # This client is separate from every fixture-owned real supplier client.
        # Its only credential is a literal fake value, never a copied real Key.
        controlled = BailianProvider(url+'/v1','fake-local-f13-classifier-key')
        env.observe_provider(controlled)
        safe_headers = []
        async def assert_local_credential(request):
            assert request.headers['authorization'] == 'Bearer fake-local-f13-classifier-key'
            assert request.url.host == '127.0.0.1'
            safe_headers.append(dict(localhost=True,fake_key_only=True))
        controlled.client._client.event_hooks.setdefault('request',[]).append(assert_local_credential)
        try:
            endpoint = previous_registry.endpoints['light-endpoint'].model_copy(
                update={'provider':'classifier-controlled'})
            group = previous_registry.model_groups['light'].model_copy(
                update={'candidates':('classifier-endpoint',),
                        'routing_description':'仅用于测试分类器入口，不能成为任务生成候选。'})
            profile = previous_registry.auto_routes['auto-general'].model_copy(
                update={'classifier_group':'classifier-only'})
            state.model_registry = replace(previous_registry,
                endpoints={**previous_registry.endpoints,'classifier-endpoint':endpoint},
                providers={**previous_registry.providers,'classifier-controlled':ProviderConfig(
                    adapter='bailian',api_protocol='responses',base_url=url+'/v1',
                    credential_ref='fake-local-classifier-only')},
                model_groups={**previous_registry.model_groups,'classifier-only':group},
                auto_routes={**previous_registry.auto_routes,'auto-general':profile})
            state.providers = {**previous_providers,'classifier-controlled':controlled}
            catalog = previous_prices.catalog.model_dump(mode='json')
            catalog['bindings']['classifier-controlled'] = 'controlled-local-only'
            rates = dict(hit='1',miss='1',output='1')
            catalog['versions'].append(dict(supplier='controlled-local-only',model=MODELS['light'],
                version='fake-local-fixture-v1',effective_from='2020-01-01T00:00:00Z',
                peak=rates,off_peak=rates))
            state.storage.prices = PriceBook(Catalog.model_validate(catalog))
            body = dict(model='auto-general',prompt={'name':'basic_text','version':'1.0.0'},
                messages=[{'role':'user','content':'已知订单A123的商品是充电器。只复述订单号和商品名称，不超过30字。'}],
                max_output_tokens=128,timeout_seconds=60)
            reply = env.client.post('/v1/llm',json=body)
            rid = reply.headers['x-request-id']; trace = env.trace(rid)
            bill_reply = env.client.get('/v1/billing/requests/'+rid)
            observation_path = env.directory/('availability-'+rid+'.json')
            observation = json.loads(observation_path.read_text()) if observation_path.exists() else None
            safe_local = dict(kind='controlled_local_http_503_not_supplier',calls=calls,
                              credential_checks=safe_headers)
            # Keep original failing evidence as well as successful evidence.
            save_evidence(env,rid,trace,bill_reply.json(),dict(response=reply.text,
                local_http=safe_local,upstream_observation=observation))
            record_property('f13_request_id',rid)
            assert reply.status_code == 200,reply.text
            assert bill_reply.status_code == 200,bill_reply.text
            assert len(calls) == len(safe_headers) == 1, 'classification or SDK retried'
            path,sent = calls[0]
            assert path == '/v1/responses'
            assert sent['model'] == MODELS['light'] and sent['max_output_tokens'] == 128
            assert sent['reasoning'] == {'effort':'none'} and sent['store'] is False
            assert sent['text']['format']['json_schema']['schema']['properties']['group']['enum'] == list(MODELS)
            data = reply.json()['data']; assert 'A123' in data['text'] and '充电器' in data['text']
            assert data['usage'] is None, 'unknown classifier usage must not become generation-only usage'
            assert trace['status'] == 'completed' and trace['requested_model'] == 'auto-general'
            auto = trace['auto_routing']
            assert auto['source'] == 'default_fallback' and auto['fallback_reason'] == 'provider_unavailable'
            assert auto['resolved_group'] == 'general'
            assert len(trace['attempts']) == 2
            classification,generation = trace['attempts']
            assert [attempt['purpose'] for attempt in trace['attempts']] == ['classification','generation']
            assert [attempt['attempt_no'] for attempt in trace['attempts']] == [1,2]
            assert classification['status'] == 'failed' and classification['error_code'] == 'provider_unavailable'
            assert classification['model'] == 'classifier-endpoint' and classification['provider'] == 'classifier-controlled'
            assert classification['usage'] is None and classification['actual_model'] is None
            assert generation['status'] == 'completed' and generation['model'] == 'general-endpoint'
            assert generation['configured_model'] == generation['actual_model'] == MODELS['general']
            assert generation['provider'] == 'bailian-main' and generation['adapter'] == 'bailian'
            usage = generation['usage']; assert usage is not None
            assert generation['input_token_kind'] == 'estimated'
            assert generation['counter_version'].startswith('qwen_text_estimate_v1:')
            assert generation['input_token_count'] >= usage['input_tokens']
            assert trace['usage']['unknown_attempts'] == [1]
            assert trace['usage']['known_subtotal'] == usage
            bill = bill_reply.json()['data']; first,second = bill['attempts']
            assert first['state'] == 'pending' and first['agent_charge'] is None and first['supplier_cost'] is None
            assert first['usage_complete'] is False
            assert second['state'] == 'settled' and second['usage_complete'] is True and second['usage_source'] == 'real'
            with env.server.connect(env.database_name) as sql:
                pk,status = sql.execute('SELECT id,status FROM tb_llm_request WHERE request_id=%s',(rid,)).fetchone()
                persisted = sql.execute('SELECT attempt_no,model,status,input_tokens,output_tokens,total_tokens FROM tb_llm_attempt WHERE request_pk=%s ORDER BY attempt_no',(pk,)).fetchall()
                charges = sql.execute('SELECT state,agent_charge,supplier_cost,quote,cached_input_tokens FROM tb_billing_attempt WHERE request_pk=%s ORDER BY attempt_no',(pk,)).fetchall()
            assert status == 'completed' and len(persisted) == len(charges) == 2
            assert persisted[0][1:3] == ('classifier-endpoint','failed') and persisted[0][3:] == (None,None,None)
            assert charges[0][:3] == ('pending',None,None)
            assert persisted[1][1:3] == ('general-endpoint','completed')
            assert persisted[1][3:] == (usage['input_tokens'],usage['output_tokens'],usage['total_tokens'])
            assert charges[1][3]['version'] == PRICE_VERSION
            cached = charges[1][4]; assert type(cached) is int and 0 <= cached <= usage['input_tokens']
            price = next(item for item in real_prices()['versions'] if item['model'] == MODELS['general'])
            rate = next(tier['peak'] for tier in price['input_tiers'] if usage['input_tokens'] <= tier['max_input_tokens'])
            with localcontext() as context:
                context.prec = 64
                expected = (cached*Decimal(rate['hit'])+(usage['input_tokens']-cached)*Decimal(rate['miss'])
                            +usage['output_tokens']*Decimal(rate['output']))/Decimal(1000000)
            assert charges[1][:3] == ('settled',expected,expected)
            assert Decimal(second['agent_charge']) == Decimal(second['supplier_cost']) == Decimal(bill['charged']) == expected
            assert observation is not None and len(observation['attempts']) == 2
            actual = observation['attempts'][1]
            assert actual['typed_text'] == data['text'] and actual['http_status'] == 200
            assert actual['read_closed'] is True
            assert state.concurrency_limiter.snapshot() == {'active_requests':0,'agents':{}}
            save_evidence(env,rid,trace,bill,dict(response=reply.text,local_http=safe_local,
                upstream_observation=observation,independent_generation_charge=str(expected),
                scope='controlled classifier 503 plus actual Bailian Plus generation; no supplier invoice assertion'))
        finally:
            state.model_registry = previous_registry
            state.providers = previous_providers
            state.storage.prices = previous_prices
            env.client.portal.call(controlled.aclose)
