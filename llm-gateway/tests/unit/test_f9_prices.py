from pricing_support import write_pricing
from datetime import datetime
from decimal import Decimal

import pytest

from contracts import Usage
from errors import GatewayError, ProviderCallError
from providers.base import parse_response, parse_responses_response
from pathlib import Path

PRICE_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures/f9_prices"


def test_flash_sample_quote():
    from services.prices import PriceBook, PriceQuote
    book = PriceBook.load(PRICE_FIXTURE_DIR)
    now = datetime.fromisoformat('2026-11-03T09:30:00+08:00')
    quote = book.quote('deepseek-main', 'deepseek-flash', now, 250, 100)
    usage = Usage(input_tokens=200, output_tokens=20, total_tokens=220)
    assert quote.reserve_amount == Decimal('0.0013')
    assert quote.cost(usage, 150) == Decimal('0.000266')
    assert quote.cost(usage, None) is None
    assert quote.cost(usage, -1) is None
    assert quote.cost(usage, 150, ended_at=now.replace(hour=12)) is None
    assert PriceQuote.from_snapshot(quote.snapshot()) == quote
    assert book.quote('openai-main', 'deepseek-flash', now, 250, 100) == quote
    with pytest.raises(GatewayError, match='价格'):
        book.quote('new-provider', 'deepseek-flash', now, 250, 100)


@pytest.mark.parametrize('chat', [True, False])
@pytest.mark.parametrize('bad_result', [True, False])
@pytest.mark.parametrize('cached', [4, 0, True, '4', 12, None])
def test_split_survives_independently_from_text(chat, bad_result, cached):
    if chat:
        report = dict(prompt_tokens=10, completion_tokens=5, total_tokens=15,
                      prompt_cache_hit_tokens=cached, prompt_cache_miss_tokens=6 if cached else 10)
        raw = dict(model='deepseek-flash', usage=report, choices=[] if bad_result else [
            dict(finish_reason='stop', message=dict(role='assistant', content='ok'))])
        parse = parse_response
    else:
        report = dict(input_tokens=10, output_tokens=5, total_tokens=15,
                      input_tokens_details={'cached_tokens': cached})
        raw = dict(object='response', model='deepseek-flash', status='completed',
                   usage=report, output=[] if bad_result else [dict(type='message', role='assistant',
                   status='completed', content=[dict(type='output_text', text='ok')])])
        parse = parse_responses_response
    if bad_result:
        with pytest.raises(ProviderCallError) as caught:
            parse(raw)
        result = caught.value
    else:
        result = parse(raw)
    assert result.usage.total_tokens == 15
    assert result.cached_input_tokens == (cached if type(cached) is int and cached <= 10 else None)


def test_operator_prices_are_explicit_and_versioned(tmp_path):
    from services.prices import PriceBook
    config = """
bindings: {wire: fixture}
versions:
  - supplier: fixture
    model: flash
    version: v1
    effective_from: '2026-01-01T00:00:00Z'
    peak: &v1 {hit: '0.4', miss: '2', output: '8'}
    off_peak: *v1
  - supplier: fixture
    model: flash
    version: v2
    effective_from: '2026-11-03T00:00:00Z'
    peak: &v2 {hit: '1', miss: '1', output: '3'}
    off_peak: *v2
"""
    write_pricing(tmp_path, config)
    book = PriceBook.load(tmp_path)
    usage = Usage(input_tokens=200, output_tokens=20, total_tokens=220)
    before = datetime.fromisoformat('2026-11-02T23:59:59Z')
    after = datetime.fromisoformat('2026-11-03T00:00:00Z')
    quote1 = book.quote('wire', 'flash', before, 250, 100)
    quote2 = book.quote('wire', 'flash', after, 250, 100)
    assert quote1.cost(usage, 150, ended_at=before) == Decimal('0.000320')
    assert quote1.cost(usage, 150, ended_at=after) is None
    assert quote2.cost(usage, None) == Decimal('0.000260')
    assert quote1.cost(None, 0) is None


@pytest.mark.parametrize('at,period', [
    ('2026-11-03T00:59:59Z', 'off_peak'), ('2026-11-03T01:00:00Z', 'peak'),
    ('2026-11-03T04:00:00Z', 'off_peak'), ('2026-11-03T06:00:00Z', 'peak'),
    ('2026-11-03T10:00:00Z', 'off_peak'), ('2026-11-07T02:00:00Z', 'off_peak'),
    ('2027-01-01T02:00:00Z', 'unknown'),
])
def test_cny_periods_and_unmaintained_calendar(at, period):
    from services.prices import PriceBook
    quote = PriceBook.load(PRICE_FIXTURE_DIR).quote(
        'deepseek-main', 'deepseek-flash', datetime.fromisoformat(at), 250, 100)
    assert quote.period == period
    assert quote.reserve_amount == Decimal('0.0013')
    if period == 'unknown':
        assert quote.cost(Usage(input_tokens=100, output_tokens=1, total_tokens=101), 5) is None


def test_conflicting_chat_splits_do_not_erase_valid_token_counts():
    from providers.base import observe_reported_metadata

    observed = {}
    counts = dict(prompt_tokens=10, completion_tokens=5, total_tokens=15,
                  prompt_cache_hit_tokens=7, prompt_cache_miss_tokens=5)
    observe_reported_metadata(dict(usage=counts, model='x'), observed, chat=True)
    assert observed['cached_input_tokens'] is None
    assert observed['usage'].input_tokens == 10


def test_incremental_event_cannot_supply_cached_counts_for_a_later_terminal():
    from providers.base import ProviderStreamEvent
    # A cache split can only describe the total usage on its own event.
    event = ProviderStreamEvent(delta='text', cached_input_tokens=7)
    assert event.cached_input_tokens is None


def test_r1_quote_reserves_for_each_known_version_before_deadline(tmp_path):
    from services.prices import PriceBook
    write_pricing(tmp_path, """
bindings: {chat: account}
versions:
  - {supplier: account, model: x, version: old, effective_from: '2026-01-01T00:00:00Z', peak: &a {hit: '1', miss: '1', output: '1'}, off_peak: *a}
  - {supplier: account, model: x, version: new, effective_from: '2026-11-03T00:00:00Z', peak: &b {hit: '10', miss: '10', output: '10'}, off_peak: *b}
  - {supplier: account, model: x, version: later, effective_from: '2026-11-04T00:00:00Z', peak: &c {hit: '50', miss: '50', output: '50'}, off_peak: *c}
""")
    book = PriceBook.load(tmp_path)
    start = datetime.fromisoformat('2026-11-02T23:59:58Z')
    boundary = datetime.fromisoformat('2026-11-03T00:00:00Z')
    assert book.quote('chat','x',start,100,100, latest_end=start).reserve_amount == Decimal('.0002')
    assert book.quote('chat','x',start,100,100, latest_end=boundary).reserve_amount == Decimal('.002')
