"""Per-application compensation for executions whose persistence was interrupted."""
import asyncio
import logging

from persistence.database import StorageUnavailable


class PersistenceReconciler:
    def __init__(self, storage, active, generations):
        self.storage = storage
        self.active, self.generations = active, generations
        self.pending = {}

    def defer(self, trace, attempt=None):
        existing = self.pending.get(trace.request_id)
        if existing is not None and getattr(trace, '_stream_creation', False) and not getattr(existing[0], '_stream_creation', False):
            return
        self.pending[trace.request_id] = (trace, attempt)

    def awaiting_persistence(self, request_id):
        entry = self.pending.get(request_id)
        return entry is not None and not getattr(entry[0], '_stream_creation', False)

    async def flush(self):
        for request_id, entry in list(self.pending.items()):
            trace, attempt = entry
            if request_id in self.active or request_id in self.generations:
                continue
            if getattr(trace, '_cancel_requested', False) and not getattr(trace, '_persisted_terminal', False):
                continue
            try:
                if getattr(trace, '_stream_creation', False):
                    # An unacknowledged POST commit is not stopped generation.
                    # Preserve any valid request even if GET already started it.
                    row = await self.storage.get_stream(request_id)
                    if row is not None:
                        trace.status, trace.error_code = row['status'], row['error_code']
                        if self.pending.get(request_id) is entry:
                            self.pending.pop(request_id, None)
                        continue
                if not getattr(trace, '_persisted_terminal', False):
                    error = trace.error_code or 'storage_unavailable'
                    event = ('response.failed', {'request_id': request_id, 'code': error,
                             'message': '请求未能完成'}) if trace.operation == 'streaming' and getattr(trace, '_admitted', False) else None
                    status = await self.storage.finish(trace, attempt, error_code=error, event=event, reconciling=True)
                    trace.status = status
                    if attempt is not None and (attempt.status == 'running' or status == 'completed'):
                        attempt.status = status
                        if status == 'completed':
                            attempt.error_code = None
                    trace._persisted_terminal = True
                await self.storage.update_diagnostics(trace, attempt)
                if self.pending.get(request_id) is entry:
                    self.pending.pop(request_id, None)
            except StorageUnavailable:
                if asyncio.current_task().cancelling():
                    raise asyncio.CancelledError
            except Exception:
                logging.getLogger(__name__).error('持久化恢复失败 request_id=%s', request_id)

