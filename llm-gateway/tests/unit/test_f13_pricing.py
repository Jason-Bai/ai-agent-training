from pricing_support import write_pricing
"""F13 A08/A09: independent Decimal goldens for length-tiered prices."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal

import pytest
import yaml

from contracts import Usage
from errors import GatewayError
from services.models import ConfigurationError
from services.prices import PriceBook, PriceQuote
from utils.json_codec import dumps, loads


NOW = datetime.fromisoformat('2026-11-03T09:30:00+08:00')


def rates(hit='1', miss='2', output='8'):
    return dict(hit=hit, miss=miss, output=output)


def document():
    low, high = rates(), rates('2', '6', '24')
    return {'bindings': {'bailian': 'account'}, 'versions': [dict(
        supplier='account', model='qwen3.7-plus', version='test-v1',
        effective_from='2026-01-01T00:00:00Z', peak=low, off_peak=low,
        input_tiers=[dict(max_input_tokens=256000, peak=low, off_peak=low),
                     dict(max_input_tokens=1000000, peak=high, off_peak=high)])]}


def load(tmp_path, definition=None):
    write_pricing(tmp_path, definition or document())
    return PriceBook.load(tmp_path)


def usage(inputs, outputs=20):
    return Usage(input_tokens=inputs, output_tokens=outputs, total_tokens=inputs+outputs)


def test_low_estimate_reserves_highest_tier_and_actual_usage_reselects_tier(tmp_path):
    quote=load(tmp_path).quote('bailian','qwen3.7-plus',NOW,100,16)
    assert quote.reserve_amount==Decimal('.000984')
    assert quote.cost(usage(256001),100)==Decimal('1.536086')
    restored=PriceQuote.from_snapshot(loads(dumps(quote.snapshot())))
    assert restored==quote
    assert restored.cost(usage(256001),100)==Decimal('1.536086')


@pytest.mark.parametrize('tokens,expected',[
    (0,'.000160'),(255999,'.512158'),(256000,'.512160'),
    (256001,'1.536486'),(999999,'6.000474'),(1000000,'6.000480')])
def test_tier_inclusive_upper_boundary(tokens,expected,tmp_path):
    quote=load(tmp_path).quote('bailian','qwen3.7-plus',NOW,100,16)
    assert quote.cost(usage(tokens),0)==Decimal(expected)


@pytest.mark.parametrize('tokens',[256000,256001])
@pytest.mark.parametrize('cache',['none','zero','partial','all'])
def test_actual_tier_applies_cache_split_without_changing_input_tier(tokens,cache,tmp_path):
    quote=load(tmp_path).quote('bailian','qwen3.7-plus',NOW,100,16)
    cached={'none':None,'zero':0,'partial':100,'all':tokens}[cache]
    cost=quote.cost(usage(tokens),cached)
    if cache=='none':assert cost is None
    else:
        hit,miss,out=(Decimal('1'),Decimal('2'),Decimal('8')) if tokens==256000 else (Decimal('2'),Decimal('6'),Decimal('24'))
        assert cost==(cached*hit+(tokens-cached)*miss+20*out)/Decimal(1000000)


def test_missing_cache_count_is_allowed_only_when_actual_tier_has_equal_rates(tmp_path):
    data=document();data['versions'][0]['input_tiers'][1]['peak']=rates('6','6','24')
    data['versions'][0]['input_tiers'][1]['off_peak']=rates('6','6','24')
    quote=load(tmp_path,data).quote('bailian','qwen3.7-plus',NOW,100,16)
    assert quote.cost(usage(256001),None)==Decimal('1.536486')
    assert quote.cost(usage(256000),None) is None


def test_unsupported_actual_length_remains_unsettled_and_estimate_is_rejected(tmp_path):
    book=load(tmp_path);quote=book.quote('bailian','qwen3.7-plus',NOW,100,16)
    assert quote.cost(usage(1000001),0) is None
    with pytest.raises(GatewayError) as caught:book.quote('bailian','qwen3.7-plus',NOW,1000001,16)
    assert caught.value.code=='price_unavailable'


@pytest.mark.parametrize('bounds',[[256000,256000],[1000000,256000],[0,1000000],
                                  [-1,1000000],[True,1000000],['256000',1000000],
                                  [256000.0,1000000],[None,1000000]])
def test_invalid_tier_bounds_are_rejected(tmp_path,bounds):
    data=document()
    for tier,bound in zip(data['versions'][0]['input_tiers'],bounds):tier['max_input_tokens']=bound
    with pytest.raises(ConfigurationError):load(tmp_path,data)


def test_old_fixed_price_snapshot_and_shape_remain_compatible(tmp_path):
    data=document();del data['versions'][0]['input_tiers']
    quote=load(tmp_path,data).quote('bailian','qwen3.7-plus',NOW,100,16)
    assert 'input_tiers' not in quote.snapshot()
    assert PriceQuote.from_snapshot(quote.snapshot())==quote
    assert quote.cost(usage(256001),0)==Decimal('.512162')


def test_tier_prices_preserve_decimal_precision(tmp_path):
    data=document();tier=data['versions'][0]['input_tiers'][1]
    tier['peak']=tier['off_peak']=rates('0.000000001','0.000000002','0.000000003')
    quote=load(tmp_path,data).quote('bailian','qwen3.7-plus',NOW,100,16)
    assert quote.cost(usage(256001),100)==Decimal('.000000000511962')


def test_tier_periods_are_not_hidden_by_equal_flat_base_rates(tmp_path):
    data=document();rule=data['versions'][0]
    rule['calendar_until']='2027-01-01';rule['input_tiers'][1]['off_peak']=rates('1','3','12')
    book=load(tmp_path,data)
    peak=book.quote('bailian','qwen3.7-plus',NOW,100,16)
    off=book.quote('bailian','qwen3.7-plus',NOW.replace(hour=13),100,16)
    assert peak.period=='peak' and off.period=='off_peak'
    assert peak.cost(usage(256001),0)==Decimal('1.536486')
    assert off.cost(usage(256001),0)==Decimal('.768243')
    assert peak.cost(usage(256001),0,ended_at=NOW.replace(hour=12)) is None


def test_known_future_version_and_its_tiers_are_included_only_within_deadline(tmp_path):
    data=document();future=deepcopy(data['versions'][0]);future.update(version='next',effective_from='2026-11-03T02:00:00Z')
    future['input_tiers'][1]['peak']=future['input_tiers'][1]['off_peak']=rates('10','60','240')
    data['versions'].append(future);book=load(tmp_path,data)
    before=book.quote('bailian','qwen3.7-plus',NOW,100,16,latest_end=NOW)
    after=book.quote('bailian','qwen3.7-plus',NOW,100,16,latest_end=NOW.replace(hour=10))
    assert before.reserve_amount==Decimal('.000984')
    assert after.reserve_amount==Decimal('.009840')
    assert after.cost(usage(256001),0,ended_at=NOW.replace(hour=10)) is None


def test_inactive_flat_prices_cannot_make_fixed_tiers_calendar_dependent(tmp_path):
    data=document();data['versions'][0]['off_peak']=rates('99','99','99')
    quote=load(tmp_path,data).quote('bailian','qwen3.7-plus',NOW,100,16)
    assert quote.period=='fixed'
    assert quote.cost(usage(256001),0)==Decimal('1.536486')
    assert quote.reserve_amount==Decimal('.000984')


@pytest.mark.parametrize('bounds',[[1000000,256000],[256000,256000]])
def test_restore_rejects_disordered_or_overlapping_tier_snapshot(tmp_path,bounds):
    snapshot=load(tmp_path).quote('bailian','qwen3.7-plus',NOW,100,16).snapshot()
    for tier,bound in zip(snapshot['input_tiers'],bounds):tier['max_input_tokens']=bound
    with pytest.raises(ValueError):PriceQuote.from_snapshot(snapshot)


def test_snapshot_mutation_cannot_change_an_existing_quote(tmp_path):
    quote=load(tmp_path).quote('bailian','qwen3.7-plus',NOW,100,16)
    snapshot=quote.snapshot();snapshot['input_tiers'][1]['peak']['miss']='1000'
    assert quote.cost(usage(256001),0)==Decimal('1.536486')
