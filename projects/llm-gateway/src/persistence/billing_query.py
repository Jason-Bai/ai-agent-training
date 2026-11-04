"""PostgreSQL projections for per-Agent accounting. Monetary values use decimal strings."""
from decimal import Decimal

from sqlalchemy import text

from persistence.billing import balance
from utils.json_codec import loads


def money(value):
    return None if value is None else format(value, 'f')


async def snapshot(session):
    await session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))


async def read_summary(database, agent_id):
    async with database.transaction() as session:
        await snapshot(session)
        account = await balance(session, agent_id)
        rows = (await session.execute(text("""
            WITH observed AS (
                SELECT a.*, (a.input_tokens IS NOT NULL AND
                    (a.adapter='mock' OR b.billing_observed_at IS NOT NULL)) AS complete
                FROM tb_llm_request r JOIN tb_llm_attempt a ON a.request_pk=r.id
                LEFT JOIN tb_billing_attempt b
                  ON (a.request_pk,a.attempt_no)=(b.request_pk,b.attempt_no)
                WHERE r.agent_id=:aid
            )
            SELECT adapter='mock' AS mock, count(*) AS attempts,
                   count(*) FILTER (WHERE NOT complete) AS unknown,
                   sum(input_tokens) FILTER (WHERE complete) AS input_tokens,
                   sum(output_tokens) FILTER (WHERE complete) AS output_tokens,
                   sum(total_tokens) FILTER (WHERE complete) AS total_tokens
            FROM observed GROUP BY adapter='mock'
        """), {'aid': agent_id})).mappings().all()
    data = {k: money(v) if isinstance(v, Decimal) else v for k, v in account.items()}
    data['currency'] = 'CNY'
    usage = {k: {'attempts': 0, 'unknown_attempts': 0, 'known_subtotal': None}
             for k in ('real', 'mock')}
    for row in rows:
        subtotal = None if row['input_tokens'] is None else {
            k: int(row[k]) for k in ('input_tokens', 'output_tokens', 'total_tokens')}
        usage['mock' if row['mock'] else 'real'] = {
            'attempts': row['attempts'], 'unknown_attempts': row['unknown'], 'known_subtotal': subtotal}
    return dict(data, usage=usage)


# Limit the request page before joining billing attempts. The same projection is
# used for a single request so its totals always use the list's accounting rules.
def request_view(row):
    return {
        'request_id': str(row['request_id']), 'created_at': row['created_at'].isoformat(),
        'model': row['requested_model'], 'status': row['status'],
        'billing_status': row['billing_status'], 'currency': 'CNY',
        **{k: money(row[k]) for k in ('charged', 'reserved', 'pending')},
    }


async def request_page(session, agent_id, predicates, params):
    query = """
        WITH page AS (
            SELECT id,request_id,created_at,requested_model,status FROM tb_llm_request
            WHERE agent_id=:aid {filters}
            ORDER BY created_at DESC, id DESC LIMIT :limit
        )
        SELECT p.*, coalesce(sum(b.agent_charge),0) AS charged,
            coalesce(sum(b.reserved_amount) FILTER (WHERE b.state IN ('reserved','pending')),0) AS reserved,
            coalesce(sum(b.reserved_amount) FILTER (WHERE b.state='pending'),0) AS pending,
            CASE WHEN count(*) FILTER (WHERE b.state='pending')>0
                 THEN CASE WHEN count(*) FILTER (WHERE b.state IN ('settled','released','platform'))>0
                      THEN 'partially_pending' ELSE 'pending' END
                 WHEN count(*) FILTER (WHERE b.state='reserved')>0 THEN 'reserved'
                 WHEN count(b.attempt_no)>0 THEN 'settled' ELSE 'no_charge' END AS billing_status
        FROM page p LEFT JOIN tb_billing_attempt b ON b.request_pk=p.id
        GROUP BY p.id,p.request_id,p.created_at,p.requested_model,p.status
        ORDER BY p.created_at DESC,p.id DESC
    """.format(filters=''.join(' AND ' + sql for sql in predicates))
    return (await session.execute(text(query), dict(params, aid=agent_id))).mappings().all()


async def read_requests(database, agent_id, size, start, end, cursor):
    params, where = {'limit': size + 1}, []
    if start is not None:
        params['start'] = start
        where.append('created_at >= :start')
    if end is not None:
        params['end'] = end
        where.append('created_at < :end')
    if cursor is not None:
        params['stamp'], params['seq'] = cursor
        where.append('(created_at,id)<(:stamp,:seq)')
    async with database.transaction() as session:
        rows = await request_page(session, agent_id, where, params)
    next_cursor = None
    if len(rows) > size:
        last = rows[size - 1]
        next_cursor = f"{last['created_at'].isoformat()}|{last['id']}"
    return {'items': [request_view(r) for r in rows[:size]], 'next_cursor': next_cursor}


async def read_detail(database, agent_id, request_id):
    async with database.transaction() as session:
        await snapshot(session)
        rows = await request_page(session, agent_id, ['request_id=:rid'],
                                  {'rid':request_id,'limit':1})
        if not rows:
            return None
        attempts = (await session.execute(text("""
            SELECT a.attempt_no,a.provider,a.adapter,a.configured_model,a.status,a.reason,
                   a.input_tokens,a.output_tokens,a.total_tokens,b.cached_input_tokens,b.state,b.billing_observed_at,
                   b.reserved_amount,b.agent_charge,b.supplier_cost,b.quote,b.reason AS billing_reason
            FROM tb_llm_attempt a LEFT JOIN tb_billing_attempt b
            ON (a.request_pk,a.attempt_no)=(b.request_pk,b.attempt_no)
            WHERE a.request_pk=:pk ORDER BY a.attempt_no
        """), {'pk':rows[0]['id']})).mappings().all()
        metrics = await session.scalar(text('SELECT timing_snapshot FROM tb_llm_request WHERE id=:pk'),
                                       {'pk': rows[0]['id']})
        purposes = loads(metrics).get('attempt_purposes', {}) if metrics else {}
    result = request_view(rows[0])
    result['attempts'] = [dict(a, purpose=purposes.get(str(a['attempt_no']), 'generation'), usage_complete=a['input_tokens'] is not None and (a['adapter']=='mock' or a['billing_observed_at'] is not None), billing_observed_at=a['billing_observed_at'].isoformat() if a['billing_observed_at'] else None, usage_source='mock' if a['adapter']=='mock' else 'real',
                               **{k:money(a[k]) for k in ('reserved_amount','agent_charge','supplier_cost')})
                          for a in attempts]
    return result
