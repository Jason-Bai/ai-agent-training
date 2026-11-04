"""Ledger mutations run in the caller's request/attempt or CLI transaction."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from errors import GatewayError
from persistence.tables import agent, billing_attempt as bill, billing_operation as op, request
from utils.diagnostic_values import attempt_usage

ACTIVE = ('reserved', 'pending')


def _money(value):
    try:
        number = Decimal(value) if isinstance(value, (str, Decimal, int)) and not isinstance(value, bool) else None
    except InvalidOperation:
        number = None
    if number is None or not number.is_finite() or number < 0 or number.adjusted() > 20 or number.as_tuple().exponent < -24:
        raise GatewayError('invalid_amount', '金额超出范围', 422)
    return number


async def _lock_agent(session, agent_id):
    if not await session.scalar(select(agent.c.agent_id).where(agent.c.agent_id == agent_id).with_for_update()):
        raise GatewayError('agent_not_found', '未找到 Agent', 404)


async def balance(session, agent_id):
    """One statement also works in a read-only snapshot."""
    deposits = select(func.coalesce(func.sum(op.c.amount), 0)).where(
        op.c.agent_id == agent_id, op.c.kind == 'deposit').scalar_subquery()
    totals = select(
        deposits.label('deposited'), func.coalesce(func.sum(bill.c.agent_charge), 0).label('charged'),
        func.coalesce(func.sum(bill.c.reserved_amount).filter(bill.c.state.in_(ACTIVE)), 0).label('reserved'),
        func.coalesce(func.sum(bill.c.reserved_amount).filter(bill.c.state == 'pending'), 0).label('pending'),
        func.count().filter(bill.c.state == 'pending').label('pending_attempts'),
    ).where(bill.c.agent_id == agent_id).subquery()
    return dict((await session.execute(select(totals, (
        totals.c.deposited - totals.c.charged - totals.c.reserved).label('available')))).mappings().one())


async def _operation(session, *, operation_id, **fields):
    if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 200:
        raise GatewayError('invalid_operation', '操作编号不合法', 422)
    added = await session.scalar(pg_insert(op).values(operation_id=operation_id, **fields)
        .on_conflict_do_nothing().returning(op.c.operation_id))
    if added:
        return True
    previous = (await session.execute(select(op).where(op.c.operation_id == operation_id))).mappings().one()
    for key, value in fields.items():
        if previous[key] != value:
            raise GatewayError('billing_operation_conflict', '操作编号已用于其他内容', 409)
    return False


async def deposit(session, agent_id, operation_id, amount):
    amount = _money(amount)
    if not amount:
        raise GatewayError('invalid_amount', '拨款必须大于零', 422)
    await _lock_agent(session, agent_id)
    await _operation(session, operation_id=operation_id, kind='deposit', agent_id=agent_id,
                     amount=amount, reason='manual_deposit', details={})
    return await balance(session, agent_id)


async def reserve(session, row, record, quote):
    owner = row['agent_id']
    if not owner:
        return
    mock = record.adapter == 'mock'
    if not mock and quote is None:
        raise GatewayError('price_unavailable', '无法确认预留价格', 503)
    amount = Decimal(0) if mock else quote.reserve_amount
    await _lock_agent(session, owner)
    if not mock and (await balance(session, owner))['available'] < amount:
        raise GatewayError('insufficient_budget', 'Agent 可用预算不足', 402)
    await session.execute(insert(bill).values(request_pk=row['id'], attempt_no=record.number,
        agent_id=owner, state='reserved', reserved_amount=amount, quote=None if mock else quote.snapshot()))


async def settle(session, row, record):
    if not row['agent_id'] or record is None:
        return
    from services.prices import PriceQuote
    await _lock_agent(session, row['agent_id'])
    key = (bill.c.request_pk == row['id'], bill.c.attempt_no == record.number)
    entry = (await session.execute(select(bill).where(*key))).mappings().one_or_none()
    if not entry or entry['state'] != 'reserved':
        return
    usage = attempt_usage(record.usage)
    cached = getattr(record, 'cached_input_tokens', None)
    if not usage or type(cached) is not int or not 0 <= cached <= usage.input_tokens:
        cached = None
    observed_at = getattr(record, 'billing_observed_at', None)
    if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
        observed_at = None
    cost = None
    state, reason = 'pending', 'insufficient_pricing_evidence'
    if record.adapter == 'mock':
        state, reason, cost = 'settled', 'mock', Decimal(0)
    elif record.provider_entered is False and usage is None:
        state, reason, cost = 'released', 'provider_not_called', Decimal(0)
    elif usage and observed_at is not None and record.actual_model == record.configured_model:
        quote = PriceQuote.from_snapshot(entry['quote'])
        cost = quote.cost(usage, cached, ended_at=observed_at)
        if cost is not None:
            state, reason = 'settled', 'reported_usage'
    await session.execute(update(bill).where(*key).values(state=state, reason=reason,
        agent_charge=cost, supplier_cost=cost, cached_input_tokens=cached, billing_observed_at=observed_at))


async def recover_billing(session, row):
    """A lost process cannot certify that its registered attempt never left the process."""
    if not row['agent_id']:
        return
    await _lock_agent(session, row['agent_id'])
    key = (bill.c.request_pk == row['id'], bill.c.state == 'reserved')
    from persistence.tables import attempt
    mock_attempts = select(attempt.c.attempt_no).where(
        attempt.c.request_pk == row['id'], attempt.c.adapter == 'mock')
    await session.execute(update(bill).where(*key, bill.c.attempt_no.in_(mock_attempts)).values(
        state='settled', agent_charge=0, supplier_cost=0, reason='mock'))
    await session.execute(update(bill).where(*key).values(state='pending', reason='execution_interrupted'))


async def resolve(session, request_id, attempt_no, operation_id, action, amount=None, reason=''):
    if action not in ('settle', 'zero', 'platform', 'supplier') or not reason.strip():
        raise GatewayError('invalid_disposition', '需要有效的处置方式和依据', 422)
    if action not in ('settle', 'supplier') and amount is not None and _money(amount) != 0:
        raise GatewayError('invalid_amount', '释放预留无需金额', 422)
    confirmed = _money(amount) if action in ('settle', 'supplier') else Decimal(0)
    row = (await session.execute(select(request.c.id, request.c.agent_id).where(
        request.c.request_id == UUID(str(request_id))))).mappings().one_or_none()
    if not row or not row['agent_id']:
        raise GatewayError('not_found', '未找到请求', 404)
    await _lock_agent(session, row['agent_id'])
    key = (bill.c.request_pk == row['id'], bill.c.attempt_no == attempt_no)
    entry = (await session.execute(select(bill).where(*key))).mappings().one_or_none()
    if not entry:
        raise GatewayError('not_found', '未找到尝试', 404)
    # Insert receipt and mutate ledger atomically. Replayed commands need no pending row.
    added = await _operation(session, operation_id=operation_id, agent_id=row['agent_id'],
        kind=action, amount=confirmed, request_pk=row['id'], attempt_no=attempt_no, reason=reason, details={})
    if added and action == 'supplier':
        if entry['state'] != 'platform' or entry['supplier_cost'] is not None:
            raise GatewayError('billing_state_conflict', '仅可核实平台承担的未知成本', 409)
        await session.execute(update(bill).where(*key).values(supplier_cost=confirmed))
    elif added:
        if entry['state'] != 'pending':
            raise GatewayError('billing_state_conflict', '仅待核实尝试允许处置', 409)
        await session.execute(update(bill).where(*key).values(state={
            'settle':'settled','zero':'released','platform':'platform'}[action],
            agent_charge=confirmed, supplier_cost=None if action == 'platform' else confirmed,
            reason='manual_' + action))
    return await balance(session, row['agent_id'])


async def reconcile_statement(session, operation_id, supplier, currency, start, end, amount, reason):
    """Compare an account-period statement; leave individual allocations untouched."""
    if (not supplier.strip() or currency != 'CNY' or start.tzinfo is None or end.tzinfo is None
            or start >= end or not reason.strip()):
        raise GatewayError('invalid_statement', '账单账户、期间、币种或依据不合法', 422)
    amount = _money(amount)
    # Serialize identical receipts even when the matching known subtotal subsequently changes.
    details = {'supplier':supplier, 'currency':currency, 'start':start.astimezone(timezone.utc).isoformat(),
               'end':end.astimezone(timezone.utc).isoformat()}
    added = await _operation(session, operation_id=operation_id, kind='statement', amount=amount,
                             reason=reason, details=details)
    totals = select(func.sum(bill.c.supplier_cost).label('subtotal'),
                    func.count().filter(bill.c.supplier_cost.is_(None)).label('unknown_attempts')).where(
        bill.c.quote['supplier'].astext == supplier, bill.c.quote['currency'].astext == currency,
        bill.c.created_at >= start, bill.c.created_at < end).subquery()
    result = (await session.execute(select(totals, (amount - func.coalesce(totals.c.subtotal, 0))
        .label('unassigned_difference')))).mappings().one()
    return dict(result, operation_id=operation_id, newly_recorded=added)
