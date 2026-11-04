"""Versioned CNY tariffs, expressed per million tokens; never read provider keys."""
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, StringConstraints, field_validator, model_validator
from typing import Annotated

from contracts import Usage
from errors import GatewayError
from services.models import ConfigurationError, Definition, read_yaml
from utils.diagnostic_values import attempt_usage


SHANGHAI = ZoneInfo('Asia/Shanghai')
Price = Annotated[str, StringConstraints(pattern=r'^(0|[1-9][0-9]{0,8})(\.[0-9]{1,9})?$')]


class Rates(Definition):
    hit: Price
    miss: Price
    output: Price


class InputTier(Definition):
    max_input_tokens: int = Field(gt=0)
    peak: Rates
    off_peak: Rates


def _tier_rates(tiers, input_tokens, period):
    tier = next((tier for tier in tiers if input_tokens <= tier.max_input_tokens), None)
    return None if tier is None else (tier.off_peak if period == 'off_peak' else tier.peak)


def _validate_tier_order(tiers):
    bounds = [tier.max_input_tokens for tier in tiers]
    if any(left >= right for left, right in zip(bounds, bounds[1:])):
        raise ValueError('Input tiers must have strictly increasing upper bounds')


class Tariff(Definition):
    supplier: str
    model: str
    version: str
    effective_from: datetime
    peak: Rates
    off_peak: Rates
    calendar_until: date | None = None
    holidays: list[date] = Field(default_factory=list)
    input_tiers: list[InputTier] = Field(default_factory=list)

    @model_validator(mode='after')
    def ordered_tiers(self):
        _validate_tier_order(self.input_tiers)
        return self

    @field_validator('effective_from', mode='before')
    @classmethod
    def timestamp(cls, value):
        stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(stamp, datetime) or stamp.tzinfo is None:
            raise ValueError('effective_from requires an explicit timezone')
        return stamp

    @field_validator('calendar_until', mode='before')
    @classmethod
    def calendar_date(cls, value):
        return date.fromisoformat(value) if isinstance(value, str) else value

    @field_validator('holidays', mode='before')
    @classmethod
    def closed_dates(cls, values):
        if not isinstance(values, list):
            raise ValueError('invalid holiday list')
        return [date.fromisoformat(v) if isinstance(v, str) else v for v in values]


class Catalog(Definition):
    bindings: dict[str, str]
    versions: list[Tariff]


@dataclass(frozen=True)
class PriceQuote:
    supplier: str
    model: str
    version: str
    currency: str
    period: str
    hit: str
    miss: str
    output: str
    input_count: int
    output_cap: int
    reserve_amount: Decimal
    valid_until: str | None
    input_tiers: tuple[InputTier, ...] = ()

    def snapshot(self):
        value = asdict(self) | {'reserve_amount': str(self.reserve_amount)}
        if self.input_tiers:
            value['input_tiers'] = [tier.model_dump(mode='json') for tier in self.input_tiers]
        else:
            value.pop('input_tiers')
        return value

    @classmethod
    def from_snapshot(cls, value):
        tiers = tuple(InputTier.model_validate(tier) for tier in value.get('input_tiers', []))
        _validate_tier_order(tiers)
        return cls(**(value | {'reserve_amount': Decimal(value['reserve_amount']),
                              'input_tiers': tiers}))

    def cost(self, usage: Usage | None, cached_input_tokens, *, ended_at=None):
        if attempt_usage(usage) is None or self.period == 'unknown':
            return None
        if self.valid_until and ended_at and ended_at >= datetime.fromisoformat(self.valid_until):
            return None
        rates = (_tier_rates(self.input_tiers, usage.input_tokens, self.period) if self.input_tiers
                 else Rates(hit=self.hit, miss=self.miss, output=self.output))
        if rates is None:
            return None
        if type(cached_input_tokens) is not int or not 0 <= cached_input_tokens <= usage.input_tokens:
            if Decimal(rates.hit) != Decimal(rates.miss) and usage.input_tokens:
                return None
            cached_input_tokens = 0
        with localcontext() as context:
            context.prec = 64
            return (cached_input_tokens * Decimal(rates.hit)
                    + (usage.input_tokens - cached_input_tokens) * Decimal(rates.miss)
                    + usage.output_tokens * Decimal(rates.output)) / 1_000_000


class PriceBook:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        starts = set()
        versions = set()
        for rule in catalog.versions:
            key = (rule.supplier, rule.model)
            if (*key, rule.effective_from) in starts or (*key, rule.version) in versions:
                raise ConfigurationError('gateway.yaml.pricing 价格版本重叠')
            starts.add((*key, rule.effective_from))
            versions.add((*key, rule.version))

    @classmethod
    def load(cls, config_dir: Path):
        path = Path(config_dir) / 'gateway.yaml'
        if not path.exists():
            return cls(Catalog(bindings={}, versions=[]))
        try:
            document = read_yaml(path)
            return cls(Catalog.model_validate(document['pricing']) if 'pricing' in document
                       else Catalog(bindings={}, versions=[]))
        except ValueError:
            raise ConfigurationError('gateway.yaml.pricing 价格配置不合法') from None

    def quote(self, provider, model, started_at, input_tokens, max_output_tokens, *, latest_end=None):
        supplier = self.catalog.bindings.get(provider)
        rules = sorted((r for r in self.catalog.versions if r.supplier == supplier and r.model == model),
                       key=lambda r: r.effective_from)
        eligible = [r for r in rules if r.effective_from <= started_at]
        if not eligible:
            raise GatewayError('price_unavailable', '缺少本次调用适用价格', 503)
        rule = eligible[-1]
        period, boundary = self._period(rule, started_at)
        next_version = next((r.effective_from for r in rules if r.effective_from > started_at), None)
        if next_version and (boundary is None or next_version < boundary):
            boundary = next_version
        rates = rule.off_peak if period == 'off_peak' else rule.peak
        if rule.input_tiers:
            rates = _tier_rates(rule.input_tiers, input_tokens, period)
            if rates is None:
                raise GatewayError('price_unavailable', '缺少本次输入长度适用价格', 503)
        candidates = [rule, *(r for r in rules if r.effective_from > started_at
                              and (latest_end is None or r.effective_from <= latest_end))]
        possible_rates = [rate for tariff in candidates for tier in (tariff.input_tiers or [tariff])
                          for rate in (tier.peak, tier.off_peak)]
        maximum_input = max(Decimal(s) for rate in possible_rates for s in (rate.hit, rate.miss))
        maximum_output = max(Decimal(rate.output) for rate in possible_rates)
        with localcontext() as context:
            context.prec = 64
            reserve = (input_tokens * maximum_input + max_output_tokens * maximum_output) / 1_000_000
        return PriceQuote(supplier, model, rule.version, 'CNY', period, rates.hit, rates.miss, rates.output,
                          input_tokens, max_output_tokens, reserve, boundary.isoformat() if boundary else None,
                          tuple(rule.input_tiers))

    @staticmethod
    def _period(rule, stamp):
        if all(tier.peak == tier.off_peak for tier in (rule.input_tiers or [rule])):
            return 'fixed', None
        local = stamp.astimezone(SHANGHAI)
        # Weekends and known statutory holidays are entirely off peak.
        day = local.date()

        def at(hour):
            return datetime.combine(day, time(hour), SHANGHAI)

        if local.weekday() < 5 and day not in rule.holidays:
            for begin, end in ((9, 12), (14, 18)):
                if local < at(begin):
                    return 'off_peak', at(begin)
                if local < at(end):
                    known = rule.calendar_until is not None and day < rule.calendar_until
                    return ('peak' if known else 'unknown'), at(end)
        # Next possible tariff change is the next working weekday at 09:00.
        day += timedelta(days=1)
        while day.weekday() >= 5 or day in rule.holidays:
            day += timedelta(days=1)
        return 'off_peak', at(9)
