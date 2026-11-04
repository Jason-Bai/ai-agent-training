"""Test-only observation; leaves product behavior and real responses unchanged."""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import json
from time import monotonic

import httpx2


def install(app, directory, monkeypatch):
    current = ContextVar('upstream_observation', default=None)
    saved = {}

    def flush(sample):
        rid = sample['request_id']
        response = sample.pop('_response', None)
        if response is not None and response.is_closed:
            sample['read_closed'] = True
        sample['raw_text'] = b''.join(sample.pop('_bytes', [])).decode('utf-8') if '_bytes' in sample else sample['raw_text']
        document = {'request_id': rid, 'attempts': saved[rid]}
        path = directory / ('availability-' + rid + '.json')
        staging = path.with_suffix('.tmp')
        staging.write_text(json.dumps(document, ensure_ascii=False))
        staging.replace(path)

    original_begin = app.state.storage.begin_attempt
    async def begin(trace, attempt, **kwargs):
        await original_begin(trace, attempt, **kwargs)
        sample = dict(request_id=trace.request_id, attempt_no=attempt.number, protocol=attempt.api_protocol,
                      started_ms=0.0, ended_ms=None, http_status=None, http_headers_ms=None,
                      read_closed=False, read_cancelled=False, raw_text='', typed_text='',
                      transport_chunks=0, provider_error_code=None, upstream_http_status=None, transient=None,
                      _bytes=[])
        saved.setdefault(trace.request_id, []).append(sample)
        current.set(sample)
    monkeypatch.setattr(app.state.storage, 'begin_attempt', begin)

    old_raw, old_bytes = httpx2.Response.aiter_raw, httpx2.Response.aiter_bytes
    async def raw(response, *args, **kwargs):
        sample = current.get()
        try:
            async for chunk in old_raw(response, *args, **kwargs):
                if sample is not None: sample['transport_chunks'] += 1
                yield chunk
        except asyncio.CancelledError:
            if sample is not None: sample['read_cancelled'] = True
            raise
        finally:
            if sample is not None: sample['read_closed'] = True

    async def decoded(response, *args, **kwargs):
        sample = current.get() if not response.is_stream_consumed else None
        async for chunk in old_bytes(response, *args, **kwargs):
            if sample is not None: sample['_bytes'].append(chunk)
            yield chunk
    monkeypatch.setattr(httpx2.Response, 'aiter_raw', raw)
    monkeypatch.setattr(httpx2.Response, 'aiter_bytes', decoded)

    def wrap(provider):
        complete, stream = provider.complete, provider.stream
        async def observed_complete(*args, **kwargs):
            sample = current.get()
            sample['_started'] = monotonic()
            try:
                result = await complete(*args, **kwargs)
                sample['typed_text'] = result.text
                return result
            except BaseException as exc:
                for key in ('provider_error_code', 'upstream_http_status', 'transient'):
                    sample[key] = getattr(exc, 'code' if key == 'provider_error_code' else key, None)
                raise
            finally:
                sample['ended_ms'] = (monotonic()-sample.pop('_started'))*1000
                flush(sample)

        async def observed_stream(*args, **kwargs):
            sample = current.get()
            sample['_started'] = monotonic()
            iterator = stream(*args, **kwargs)
            try:
                async for item in iterator:
                    sample['typed_text'] += item.delta
                    if item.status == 'failed':
                        sample.update(provider_error_code=item.error_code, upstream_http_status=item.upstream_http_status,
                                      transient=item.transient)
                    yield item
            finally:
                await iterator.aclose()
                sample['ended_ms'] = (monotonic()-sample.pop('_started'))*1000
                flush(sample)
        monkeypatch.setattr(provider, 'complete', observed_complete)
        monkeypatch.setattr(provider, 'stream', observed_stream)
        client = getattr(provider.client, '_client', provider.client)
        async def headers(response):
            sample = current.get()
            if sample is not None:
                sample['http_status'] = response.status_code
                sample['http_headers_ms'] = (monotonic()-sample['_started'])*1000
                sample['_response'] = response
        client.event_hooks.setdefault('response', []).append(headers)

    previous = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(app):
        async with previous(app):
            for provider in {id(p):p for p in app.state.providers.values()}.values(): wrap(provider)
            yield
    monkeypatch.setattr(app.router, 'lifespan_context', lifespan)
    return wrap


async def snapshot_failures(app, directory):
    """Retain failed live-test evidence before its disposable database is removed."""
    from sqlalchemy import text
    from persistence.trace_query import read_trace
    from utils.json_codec import dumps
    manager = app.state.streaming_manager
    def resources():
        counts=app.state.concurrency_limiter.snapshot()
        return dict(active_requests=counts['active_requests'],agent_requests=counts['agents'],
                    active_generations=len(manager.tasks),subscriptions=len(manager.subscriptions),
                    cleanup_tasks=len(manager.cleanup_tasks),cancellations=len(manager.cancellations))
    until=monotonic()+8
    while resources()['active_requests'] and monotonic()<until:
        await asyncio.sleep(.02)
    for rid, trace in app.state.traces.requests.items():
        if trace.status!='failed': continue
        saved=await read_trace(app.state.storage.database,rid,trace.agent_id)
        # Releasing the execution permit precedes the deferred writer's commit.
        # Observe the database until terminal or the original shared deadline;
        # never turn a still-running database row into a fabricated failure.
        terminal={'completed','failed','canceled'}
        while saved.status not in terminal and monotonic()<until:
            await asyncio.sleep(min(.02,max(0,until-monotonic())))
            saved=await read_trace(app.state.storage.database,rid,trace.agent_id)
        async with app.state.storage.database.transaction() as session:
            row=(await session.execute(text('SELECT id,status,error_code FROM tb_llm_request WHERE request_id=:rid'),{'rid':rid})).mappings().one()
            pk={'pk':row['id']}
            bills=(await session.execute(text('SELECT attempt_no,state,reserved_amount,agent_charge,supplier_cost FROM tb_billing_attempt WHERE request_pk=:pk ORDER BY attempt_no'),pk)).mappings().all()
            events=(await session.execute(text('SELECT seq,event_type,payload FROM tb_llm_stream_event WHERE request_pk=:pk ORDER BY seq'),pk)).mappings().all()
        committed=saved.status in terminal and row['status'] in terminal
        consistent=committed and saved.status==row['status'] and not any(b['state']=='reserved' for b in bills)
        reason=None if consistent else ('terminal_not_committed' if not committed else 'terminal_snapshot_inconsistent')
        value=dict(request_id=rid,trace=saved.model_dump(mode='json'),drained=resources(),
                   sql_request=dict(row),bills=[dict(x) for x in bills],events=[dict(x) for x in events],
                   evidence_complete=consistent,evidence_incomplete_reason=reason)
        (directory/('failure-scene-'+rid+'.json')).write_text(dumps(value))
