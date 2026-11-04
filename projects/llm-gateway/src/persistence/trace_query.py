"""Read one consistent, metadata-only projection without acquiring lifecycle locks."""
import json

from sqlalchemy import select, text

from contracts import Usage
from persistence.tables import attempt, event, request
from trace_contracts import TraceAttempt, TraceData
from utils.json_codec import loads


_END_REASONS = {'stop', 'length', 'refusal', 'content_filter', 'tool_calls', 'aborted',
                'insufficient_system_resource', 'max_output_tokens'}


def _usage_summary(attempts):
    known = [a.usage for a in attempts if a.usage is not None]
    return {
        'known_subtotal': {key: sum(getattr(u, key) for u in known) for key in Usage.model_fields} if known else None,
        'unknown_attempts': [a.attempt_no for a in attempts if a.usage is None],
    }


async def read_trace(database, request_id, agent_id):
    # PostgreSQL's default READ COMMITTED can mix two commits between these SELECTs.
    async with database.transaction() as session:
        await session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        summary_columns = [request.c[name] for name in TraceData.model_fields if name in request.c]
        row = (await session.execute(select(request.c.id, *summary_columns, request.c.timing_snapshot)
               .where(request.c.request_id == request_id, request.c.agent_id == agent_id))).mappings().one_or_none()
        if row is None:
            return None
        metrics = json.loads(row['timing_snapshot']) if row['timing_snapshot'] else {}
        skipped = metrics.get('skipped_candidates')
        if skipped is None and row['operation'] != 'provider_probe':
            # PostgreSQL JSON operators reject legal escaped NULs in historical TEXT
            # snapshots. Decode only when metadata is missing, never project raw data.
            snapshot = await session.scalar(select(request.c.input_snapshot).where(request.c.id == row['id']))
            if snapshot:
                skipped = loads(snapshot).get('skipped_candidates')
        columns = [attempt.c[name] for name in TraceAttempt.model_fields if name in attempt.c]
        rows = (await session.execute(select(*columns, attempt.c.input_tokens, attempt.c.output_tokens,
                    attempt.c.total_tokens).where(attempt.c.request_pk == row['id'])
                    .order_by(attempt.c.attempt_no))).mappings().all()
        requested_at = None
        if row['operation'] == 'streaming' and row['status'] in {'canceling', 'canceled', 'failed'}:
            requested_at = await session.scalar(select(event.c.created_at).where(
                event.c.request_pk == row['id'], event.c.event_type == 'response.canceling'
            ).order_by(event.c.seq.desc()).limit(1))

    attempts = []
    for item in rows:
        usage = None if item['total_tokens'] is None else Usage(**{k: item[k] for k in Usage.model_fields})
        values = {col.name: item[col.name] for col in columns}
        # Registration persists false before dispatch. A running process or a
        # crash can leave that placeholder even after a real Provider call.
        if values['provider_entered'] is False:
            values['provider_entered'] = None
        # Upstream strings may contain arbitrary text even for a rejected reply.
        for key in ('finish_reason', 'upstream_finish_reason'):
            if values[key] not in _END_REASONS:
                values[key] = None
        if (values['upstream_status'] not in {'in_progress', 'completed', 'failed', 'incomplete'}
                and not (values['adapter'] == 'bailian' and values['upstream_status'] == 'queued')):
            values['upstream_status'] = None
        attempts.append(TraceAttempt(**values, usage=usage,
            purpose=metrics.get('attempt_purposes', {}).get(str(item['attempt_no']), 'generation'),
            input_token_kind=metrics.get('attempt_token_kinds', {}).get(str(item['attempt_no']))))
    elapsed = None
    if row['ended_at'] is not None:
        elapsed = (row['ended_at'] - row['created_at']).total_seconds() * 1000
    stage = metrics.get('error_stage') if row['status'] == 'failed' else None
    if row['error_code'] == 'execution_interrupted':
        stage = 'recovery'
    if row['error_code'] == 'cancel_cleanup_failed':
        stage = 'cleanup'
    return TraceData(
        **{col.name: row[col.name] for col in summary_columns}, error_stage=stage,
        delivery_error_code=metrics.get('delivery_error_code'), attempts=attempts,
        usage=_usage_summary(attempts),
        timing=dict(request_duration_ms=elapsed, ttft_ms=metrics.get('ttft_ms'),
                    generation_duration_ms=metrics.get('generation_duration_ms'),
                    stream_duration_ms=metrics.get('stream_duration_ms')),
        recovery=dict(stop_reason=metrics.get('recovery_stop_reason'), wait_ms=metrics.get('recovery_wait_ms'),
                      skipped_candidates=None if skipped is None else [
                          {'model': item['model'], 'code': item['code']} for item in skipped]),
        routing=metrics.get('routing'),
        auto_routing=metrics.get('auto_routing'),
        input_token_kind=metrics.get('input_token_kind'),
        cancellation=dict(requested_at=requested_at, ended_at=row['ended_at'] if requested_at else None,
                          cleanup_error_code=metrics.get('cleanup_error_code')),
    )
