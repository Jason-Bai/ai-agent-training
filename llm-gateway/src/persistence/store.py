import asyncio
from datetime import datetime, timezone, timedelta
from uuid import UUID

from sqlalchemy import select, insert, update, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from errors import GatewayError
from persistence import billing
from persistence.tables import request, attempt as attempt_table, event as event_table
from utils.json_codec import dumps, loads
from utils.diagnostic_values import attempt_usage, metadata_text
from constants import SUBSCRIPTION_PAGE_SIZE

TERMINAL = {'completed', 'failed', 'canceled'}


def now():
    return datetime.now(timezone.utc)


def request_values(trace):
    fields = ('requested_model', 'prompt_name', 'prompt_version', 'prompt_fingerprint',
              'schema_fingerprint', 'input_token_count', 'max_output_tokens', 'counter_version',
              'structured_output_mode', 'timeout_seconds')
    values = {name: getattr(trace, name) for name in fields}
    # Rejected client identifiers can contain characters PostgreSQL TEXT cannot store.
    for name in ('requested_model', 'prompt_name', 'prompt_version'):
        values[name] = metadata_text(values[name])
    values['agent_id'] = getattr(trace, 'agent_id', None)
    return values


def auto_selection_values(trace):
    selected = trace.auto_routing
    if (not selected or selected.get('profile') != trace.requested_model
            or selected.get('resolved_group') is None):
        return {}
    return {name: getattr(trace, name) for name in (
        'prompt_fingerprint', 'schema_fingerprint', 'structured_output_mode',
        'input_token_count', 'counter_version')}


def timing(trace):
    return dumps({'ttft_ms': trace.ttft_ms, 'generation_duration_ms': trace.generation_duration_ms,
                  'stream_duration_ms': trace.stream_duration_ms,
                  'delivery_error_code': trace.delivery_error_code,
                  'cleanup_error_code': trace.cleanup_error_code, 'error_stage': trace.error_stage,
                  'recovery_wait_ms': trace.recovery_wait_ms,
                  'skipped_candidates': getattr(trace, '_skipped_candidates', None),
                  'recovery_stop_reason': getattr(trace, '_recovery_stop_reason', None),
                  'routing': trace.routing,
                  'input_token_kind': trace.input_token_kind,
                  'attempt_token_kinds': getattr(trace, '_attempt_token_kinds', {}),
                  'attempt_purposes': getattr(trace, '_attempt_purposes', {}),
                  'auto_routing': trace.auto_routing,
                  'source': 'process_monotonic_observations'})


def attempt_values(trace, record):
    result = {name: metadata_text(getattr(record, name)) for name in
              ('actual_model', 'finish_reason', 'upstream_status', 'upstream_finish_reason')}
    for name in ('latency_ms', 'provider_duration_ms', 'error_code', 'provider_error_code'):
        result[name] = getattr(record, name)
    result['cleanup_error_code'] = trace.cleanup_error_code
    result['provider_entered'] = record.provider_entered
    usage = attempt_usage(record.usage)
    for field in ('input_tokens', 'output_tokens', 'total_tokens'):
        result[field] = getattr(usage, field) if usage is not None else None
    return result


class Store:
    def __init__(self, database, prices=None):
        self.database = database
        self.prices = prices

    async def _locked(self, session, request_id):
        return (await session.execute(select(request).where(request.c.request_id == UUID(str(request_id))).with_for_update())).mappings().one_or_none()

    async def create_pending(self, trace, snapshot):
        async with self.database.transaction() as session:
            await session.execute(insert(request).values(
                request_id=UUID(trace.request_id), operation='streaming', status='pending',
                created_at=now(), last_event_seq=0, input_snapshot=dumps(snapshot),
                timing_snapshot=timing(trace),
                **request_values(trace)))

    @staticmethod
    def _stream_selection(request_id):
        return select(request).where(request.c.request_id == UUID(str(request_id)),
            request.c.operation == 'streaming', request.c.input_snapshot.is_not(None))

    async def get_stream(self, request_id):
        async with self.database.transaction() as session:
            row = (await session.execute(self._stream_selection(request_id))).mappings().one_or_none()
            return None if row is None else dict(row)

    async def claim_stream(self, request_id, started_at, deadline_at, *, deadline):
        async with self.database.transaction(deadline=deadline) as session:
            winner = await session.scalar(update(request).where(
                request.c.request_id == UUID(str(request_id)), request.c.operation == 'streaming',
                request.c.input_snapshot.is_not(None), request.c.status == 'pending').values(
                status='running', started_at=started_at, deadline_at=deadline_at).returning(request.c.id))
            return winner is not None

    async def accept_cancel(self, request_id):
        async with self.database.transaction() as session:
            row = await self._locked(session, request_id)
            if row is None or row['operation'] != 'streaming' or row['input_snapshot'] is None:
                return None
            if row['status'] in {'pending', 'running'}:
                await session.execute(update(request).where(request.c.id == row['id']).values(status='canceling'))
                await self._event(session, row, 'response.canceling', {'request_id': str(request_id)})
                return 'canceling'
            return row['status']

    async def finish_cancel(self, trace, attempt=None, *, error_code=None):
        async with self.database.transaction() as session:
            row = await self._locked(session, trace.request_id)
            if row is None or row['operation'] != 'streaming' or row['input_snapshot'] is None:
                return None
            if row['status'] != 'canceling':
                return row['status']
            status = 'failed' if error_code else 'canceled'
            timestamp = now()
            await session.execute(update(request).where(request.c.id == row['id']).values(
                status=status, ended_at=timestamp, error_code=error_code,
                error_message='取消清理失败' if error_code else None, timing_snapshot=timing(trace),
                **auto_selection_values(trace)))
            # An Attempt can already exist even when its in-memory owner has
            # not been registered. Update existing rows, never fabricate one.
            values = {'status': status, 'ended_at': timestamp, 'error_code': error_code}
            if trace.cleanup_error_code is not None:
                values['cleanup_error_code'] = trace.cleanup_error_code
            if attempt is not None:
                values.update({key: value for key, value in attempt_values(trace, attempt).items()
                               if value is not None and key != 'error_code'})
            selection = update(attempt_table).where(attempt_table.c.request_pk == row['id'],
                                                    attempt_table.c.status == 'running')
            if attempt is not None:
                selection = selection.where(attempt_table.c.attempt_no == attempt.number)
            await session.execute(selection.values(**values))
            if attempt is not None:
                await billing.settle(session, row, attempt)
            await billing.recover_billing(session, row)
            payload = {'request_id': trace.request_id}
            if error_code:
                payload.update(code=error_code, message='取消清理失败')
            await self._event(session, row, 'response.' + status, payload)
            return status

    async def read_events(self, request_id, after_seq, limit=SUBSCRIPTION_PAGE_SIZE):
        async with self.database.transaction() as session:
            row = (await session.execute(self._stream_selection(request_id))).mappings().one_or_none()
            if row is None:
                return None, []
            # Bound to the observed counter: a later commit is read next time.
            events = (await session.execute(select(event_table).where(
                event_table.c.request_pk == row['id'], event_table.c.seq > after_seq,
                event_table.c.seq <= row['last_event_seq']).order_by(event_table.c.seq).limit(
                min(limit, SUBSCRIPTION_PAGE_SIZE)))).mappings().all()
            return dict(row), [dict(event) for event in events]

    async def prepare(self, trace, snapshot, *, deadline=None):
        timestamp = now()
        remaining = deadline - asyncio.get_running_loop().time() if deadline is not None else trace.timeout_seconds
        deadline_at = timestamp + timedelta(seconds=remaining) if remaining is not None else None
        async with self.database.transaction(deadline=deadline) as session:
            await session.execute(insert(request).values(
                request_id=UUID(trace.request_id), operation=trace.operation, status='running',
                created_at=timestamp, deadline_at=deadline_at,
                last_event_seq=0 if trace.operation == 'streaming' else None,
                input_snapshot=None if trace.operation == 'provider_probe' else dumps(snapshot), **request_values(trace)))

    async def begin_attempt(self, trace, attempt, *, deadline=None):
        kinds = getattr(trace, '_attempt_token_kinds', {})
        kinds[str(attempt.number)] = attempt.input_token_kind
        trace._attempt_token_kinds = kinds
        purposes = getattr(trace, '_attempt_purposes', {})
        purposes[str(attempt.number)] = attempt.purpose
        trace._attempt_purposes = purposes
        async with self.database.transaction(deadline=deadline) as session:
            row = await self._locked(session, trace.request_id)
            if row is None or row['status'] != 'running' or (row['input_snapshot'] is None and row['operation'] != 'provider_probe'):
                raise ValueError('Request is not admitted for execution')
            timestamp = now()
            await session.execute(insert(attempt_table).values(
                request_pk=row['id'], attempt_no=attempt.number, model=attempt.model,
                provider=attempt.provider, adapter=attempt.adapter, api_protocol=attempt.api_protocol,
                configured_model=attempt.configured_model, status='running', started_at=timestamp,
                input_snapshot=dumps(attempt.input_snapshot) if row['operation'] != 'provider_probe' and getattr(attempt, 'input_snapshot', None) is not None else None,
                prompt_fingerprint=attempt.prompt_fingerprint, input_token_count=attempt.input_token_count,
                counter_version=attempt.counter_version, max_output_tokens=attempt.max_output_tokens,
                reason=attempt.reason, provider_entered=attempt.provider_entered))
            if row['agent_id'] is not None:
                try:
                    quote = None if attempt.adapter == 'mock' else self.prices.quote(
                        attempt.provider, attempt.configured_model, timestamp,
                        attempt.input_token_count, attempt.max_output_tokens, latest_end=row['deadline_at'])
                    await billing.reserve(session, row, attempt, quote)
                except GatewayError as exc:
                    trace.error_stage = 'billing_admission'
                    trace._recovery_stop_reason = exc.code
                    raise
            await session.execute(update(request).where(request.c.id == row['id']).values(
                started_at=func.coalesce(request.c.started_at, timestamp), timing_snapshot=timing(trace),
                **auto_selection_values(trace)))

    async def finish_attempt(self, trace, attempt, *, deadline=None):
        """Save one invocation outcome; request termination has a separate owner."""
        if attempt.status not in {'completed', 'failed', 'canceled'}:
            raise ValueError('Attempt must have an outcome')
        async with self.database.transaction(deadline=deadline) as session:
            row = await self._locked(session, trace.request_id)
            if row is None:
                raise ValueError('Request is not admitted for execution')
            if row['status'] != 'running':
                return row['status']
            await session.execute(update(attempt_table).where(
                attempt_table.c.request_pk == row['id'], attempt_table.c.attempt_no == attempt.number,
                attempt_table.c.status == 'running').values(
                status=attempt.status, ended_at=now(), **attempt_values(trace, attempt)))
            await billing.settle(session, row, attempt)
            return row['status']

    async def _event(self, session, row, name, payload):
        seq = row['last_event_seq'] + 1
        await session.execute(insert(event_table).values(request_pk=row['id'], seq=seq,
            event_type=name, payload=dumps(payload), created_at=now()))
        await session.execute(update(request).where(request.c.id == row['id']).values(last_event_seq=seq))
        return seq

    async def append_event(self, trace, name, payload, *, deadline=None):
        async with self.database.transaction(deadline=deadline) as session:
            row = await self._locked(session, trace.request_id)
            if row is None or row['status'] != 'running' or row['input_snapshot'] is None or name != 'response.delta':
                raise ValueError('Request cannot append a delta')
            return await self._event(session, row, name, payload)

    async def finish(self, trace, attempt=None, *, error_code=None, event=None, deadline=None, reconciling=False):
        async with self.database.transaction(deadline=deadline) as session:
            row = await self._locked(session, trace.request_id)
            if reconciling and row is not None and row['status'] in TERMINAL:
                # A previous writer committed the outcome. The subsequent failure
                # belongs to the response path, even if both codes happen to match.
                if error_code == 'storage_unavailable' or error_code != row['error_code']:
                    trace.delivery_error_code = error_code
                return row['status']
            if row is None:
                # Entry rejection: only validated metadata, never an execution snapshot.
                timestamp = now()
                await session.execute(pg_insert(request).values(request_id=UUID(trace.request_id),
                    operation=trace.operation, status='failed', created_at=timestamp, ended_at=timestamp,
                    error_code=error_code or trace.error_code or 'internal_error', error_message='请求未能完成',
                    last_event_seq=0 if trace.operation == 'streaming' else None,
                    timing_snapshot=timing(trace), **request_values(trace)).on_conflict_do_nothing(index_elements=['request_id']))
                row = await self._locked(session, trace.request_id)
            if row['status'] in TERMINAL or row['status'] == 'canceling':
                return row['status']
            status = 'failed' if error_code else 'completed'
            timestamp = now()
            await session.execute(update(request).where(request.c.id == row['id']).values(
                status=status, ended_at=timestamp, error_code=error_code,
                error_message='请求未能完成' if error_code else None, timing_snapshot=timing(trace),
                **auto_selection_values(trace)))
            if attempt is not None:
                await session.execute(update(attempt_table).where(
                    attempt_table.c.request_pk == row['id'], attempt_table.c.attempt_no == attempt.number,
                    attempt_table.c.status == 'running').values(
                    status=attempt.status if attempt.status in TERMINAL else status,
                    ended_at=timestamp, **attempt_values(trace, attempt)))
                await billing.settle(session, row, attempt)
            await billing.recover_billing(session, row)
            if event is not None:
                name, payload = event
                if name != 'response.' + status or row['input_snapshot'] is None:
                    raise ValueError('Terminal event must match request status')
                await self._event(session, row, name, payload)
            return status

    async def update_diagnostics(self, trace, attempt=None):
        async with self.database.transaction() as session:
            row = await self._locked(session, trace.request_id)
            if row is None or row['status'] not in TERMINAL:
                return
            # Execution diagnostics belong to the committed terminal row.
            # Only these observations can arrive after the winning transaction.
            saved = loads(row['timing_snapshot']) if row['timing_snapshot'] else {}
            for field in ('ttft_ms', 'generation_duration_ms', 'stream_duration_ms',
                          'cleanup_error_code', 'delivery_error_code'):
                value = getattr(trace, field)
                if value is not None and saved.get(field) is None:
                    saved[field] = value
            await session.execute(update(request).where(request.c.id == row['id']).values(timing_snapshot=dumps(saved)))
            trace.status, trace.error_code = row['status'], row['error_code']
            trace.error_stage = saved.get('error_stage')
            trace.delivery_error_code = saved.get('delivery_error_code')
            if attempt is not None:
                # Only diagnostics acquired after terminal commit may change here.
                diagnostics = {
                    'cleanup_error_code': func.coalesce(attempt_table.c.cleanup_error_code, trace.cleanup_error_code),
                    'latency_ms': func.coalesce(attempt_table.c.latency_ms, attempt.latency_ms),
                }
                await session.execute(update(attempt_table).where(
                    attempt_table.c.request_pk == row['id'], attempt_table.c.attempt_no == attempt.number).values(
                    **diagnostics))

    async def _recover_row(self, session, row):
        if row['status'] not in {'running', 'canceling'}:
            return row['status']
        timestamp = now()
        await session.execute(update(request).where(request.c.id == row['id']).values(
            status='failed', error_code='execution_interrupted', error_message='执行已中断', ended_at=timestamp))
        await session.execute(update(attempt_table).where(attempt_table.c.request_pk == row['id'],
            attempt_table.c.status == 'running').values(status='failed', error_code='execution_interrupted', ended_at=timestamp))
        await billing.recover_billing(session, row)
        if row['operation'] == 'streaming' and row['input_snapshot'] is not None:
            await self._event(session, row, 'response.failed', {'request_id': str(row['request_id']),
                'code': 'execution_interrupted', 'message': '执行已中断'})
        return 'failed'

    async def recover(self):
        # Startup only, after the previous single worker has stopped.
        async with self.database.transaction() as session:
            rows = (await session.execute(select(request).where(request.c.status.in_(['running','canceling'])).with_for_update())).mappings().all()
            for row in rows:
                await self._recover_row(session, row)

    async def reconcile(self, request_id):
        # Caller guarantees this execution has stopped. Never scan other requests.
        async with self.database.transaction() as session:
            row = await self._locked(session, request_id)
            return None if row is None else await self._recover_row(session, row)
