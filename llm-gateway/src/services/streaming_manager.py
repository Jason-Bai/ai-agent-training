"""Application-owned generation; HTTP subscribers never own the Provider task."""
import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from time import monotonic

from contracts import Message, StreamRequest
from constants import STREAM_CLEANUP_TIMEOUT_SECONDS, DATABASE_SHUTDOWN_GRACE_SECONDS
from errors import GatewayError, ProviderCallError
from persistence.database import StorageUnavailable
from persistence.store import TERMINAL, request_values
from services.trace import log_storage_failure
from services.streaming import PreparedStream, StreamingExecution, StreamFailure
from utils.json_codec import dumps, loads


@dataclass
class Cancellation:
    ready: asyncio.Future
    row: dict
    phase: str = 'new'
    last_logged_stage: str | None = None
    task: asyncio.Task | None = None
    trace: object = None
    attempt: object = None
    generation: asyncio.Task | None = None
    stop_sent: bool = False
    error_code: str | None = None


class StreamingManager:
    def __init__(self, storage, traces, registry, token_budget, providers, defer, active, *, awaiting_persistence, limiter,
                 prompt_registry=None):
        self.storage, self.traces, self.registry = storage, traces, registry
        self.token_budget, self.providers, self.defer = token_budget, providers, defer
        self.active = active
        self.awaiting_persistence = awaiting_persistence
        self.executions = {}
        self.tasks = {}
        self.claims = {}
        self.orphans = set()
        self.closing = False
        self.subscriptions = set()
        self.cancellations = {}
        self.cleanup_tasks = set()
        self.limiter = limiter
        self.permits = {}
        self.prompt_registry = prompt_registry

    @asynccontextmanager
    async def _claim_lock(self, request_id):
        lock, users = self.claims.get(request_id, (asyncio.Lock(), 0))
        self.claims[request_id] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            _, users = self.claims[request_id]
            if users == 1:
                del self.claims[request_id]
            else:
                self.claims[request_id] = (lock, users - 1)

    async def _read_stream(self, request_id):
        try:
            return await self.storage.get_stream(request_id)
        except StorageUnavailable:
            log_storage_failure(request_id, 'get_stream')
            raise

    async def subscribe(self, request_id, after_seq):
        async with self._claim_lock(request_id):
            row = await self._read_stream(request_id)
            if row is None:
                raise GatewayError('not_found', '流式请求不存在', 404)
            if after_seq > row['last_event_seq']:
                raise GatewayError('invalid_event_cursor', '事件游标超过当前最后序号', 400)
            if row['status'] in TERMINAL:
                return row, None
            if self.closing:
                raise GatewayError('execution_interrupted', '服务正在停止', 503)
            if row['status'] == 'pending':
                permit = self.limiter.acquire(row['agent_id'], request_id)
                self.permits[request_id] = permit
                started = monotonic()
                loop_start = asyncio.get_running_loop().time()
                timestamp = datetime.now(timezone.utc)
                timeout = row['timeout_seconds']
                deadline = loop_start + timeout
                # Until commit has been acknowledged, no task may call Provider.
                try:
                    claimed = await self.storage.claim_stream(request_id, timestamp,
                        timestamp + timedelta(seconds=timeout), deadline=deadline)
                except BaseException as exc:
                    self._release_permit(request_id)
                    if isinstance(exc, StorageUnavailable):
                        log_storage_failure(request_id, 'claim_stream')
                    self.orphans.add(request_id)
                    raise
                if claimed:
                    trace = self.traces.start_request(request_id, operation='streaming')
                    for field in request_values(trace):
                        setattr(trace, field, row[field])
                    trace.status, trace.error_code = 'running', None
                    metadata = loads(row['timing_snapshot']) if row['timing_snapshot'] else {}
                    trace.routing = metadata.get('routing')
                    trace.input_token_kind = metadata.get('input_token_kind')
                    trace._attempt_token_kinds = metadata.get('attempt_token_kinds', {})
                    trace._attempt_purposes = metadata.get('attempt_purposes', {})
                    trace.auto_routing = metadata.get('auto_routing')
                    trace._admitted = True
                    trace.stream_started_at = started
                    self.active.add(request_id)
                    coroutine = self._generate(row, trace, deadline)
                    try:
                        task = asyncio.create_task(coroutine)
                        self.tasks[request_id] = task
                        self.orphans.discard(request_id)
                        task.add_done_callback(lambda done: self._task_done(request_id, trace, done))
                    except BaseException:
                        coroutine.close()
                        self._release_permit(request_id)
                        self.active.discard(request_id)
                        self.orphans.add(request_id)
                        raise
                    # Registration precedes any suspension or visibility to another GET.
                    row = dict(row, status='running', started_at=timestamp,
                               deadline_at=timestamp + timedelta(seconds=timeout))
                else:
                    self._release_permit(request_id)
                    row = await self._read_stream(request_id)
            if (row['status'] in {'running', 'canceling'} and request_id not in self.tasks
                    and request_id not in self.cancellations
                    and not self.awaiting_persistence(request_id)):
                self.orphans.add(request_id)
                await self.storage.reconcile(request_id)
                self.orphans.discard(request_id)
                row = await self._read_stream(request_id)
            trace = self.traces.requests.get(request_id) if request_id in self.tasks else None
            if trace is None:
                return row, None

            def first_text(sent_at):
                if trace.first_text_sent_at is None:
                    trace.first_text_sent_at = sent_at
                    # The producer's final defer captures the same object. If it
                    # already stopped, queue only persisted-terminal diagnostics.
                    if request_id not in self.active:
                        self.defer(trace, next((a for a in reversed(self.traces.attempts)
                                               if a.request_id == request_id), None))
            return row, first_text

    async def cancel(self, request_id):
        async with self._claim_lock(request_id):
            row = await self._read_stream(request_id)
            if row is None:
                raise GatewayError('not_found', '流式请求不存在', 404)
            if row['status'] in TERMINAL:
                return row['status']
            if self.closing:
                raise GatewayError('execution_interrupted', '服务正在停止', 503)
            operation = self.cancellations.get(request_id)
            if operation is None:
                ready = asyncio.get_running_loop().create_future()
                # A disconnected HTTP caller must not leave an unobserved error.
                ready.add_done_callback(lambda value: value.exception() if not value.cancelled() else None)
                operation = Cancellation(ready, row)
                self.cancellations[request_id] = operation
                self._start_cancel(request_id, operation)
            if row['status'] == 'canceling':
                return 'canceling'
        return await asyncio.shield(operation.ready)

    def _start_cancel(self, request_id, operation):
        coroutine = self._cancel_operation(request_id, operation)
        try:
            operation.task = asyncio.create_task(coroutine)
        except Exception:
            coroutine.close()
            if not operation.ready.done():
                operation.ready.set_exception(GatewayError('internal_error', '服务内部错误', 500))
            return
        operation.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    async def _cancel_operation(self, request_id, operation):
        try:
            async with self._claim_lock(request_id):
                if operation.phase in {'new', 'uncertain'}:
                    stage = 'accept_cancel' if operation.phase == 'new' else 'get_stream'
                    if operation.phase == 'new':
                        # Keep reconciliation responsibility even if commit ACK is lost.
                        operation.phase = 'uncertain'
                        status = await self.storage.accept_cancel(request_id)
                    else:
                        row = await self.storage.get_stream(request_id)
                        status = row['status'] if row is not None else None
                    if status is None:
                        raise GatewayError('not_found', '流式请求不存在', 404)
                    if status != 'canceling':
                        if not operation.ready.done():
                            operation.ready.set_result(status)
                        self.cancellations.pop(request_id, None)
                        return
                    trace = self.traces.requests.get(request_id)
                    restored_trace = trace is None or getattr(trace, '_stream_creation', False)
                    if restored_trace:
                        trace = self.traces.start_request(request_id, operation='streaming')
                        for field in request_values(trace):
                            setattr(trace, field, operation.row[field])
                    trace.status, trace.error_code = 'canceling', None
                    metadata = loads(operation.row['timing_snapshot']) if operation.row['timing_snapshot'] else {}
                    if restored_trace:
                        trace.routing = metadata.get('routing')
                        trace.input_token_kind = metadata.get('input_token_kind')
                        trace._attempt_token_kinds = metadata.get('attempt_token_kinds', {})
                        trace._attempt_purposes = metadata.get('attempt_purposes', {})
                        trace.auto_routing = metadata.get('auto_routing')
                    trace._cancel_requested = True
                    trace._admitted = True
                    if operation.row['timing_snapshot'] and not hasattr(trace, '_skipped_candidates'):
                        trace._skipped_candidates = loads(operation.row['timing_snapshot']).get('skipped_candidates')
                    operation.trace = trace
                    operation.generation = self.tasks.get(request_id)
                    operation.phase = 'accepted'
                    execution = self.executions.get(request_id)
                    if execution is not None:
                        execution.cancel_requested = True
                    if not operation.ready.done():
                        operation.ready.set_result('canceling')
            if operation.phase == 'accepted':
                generation = operation.generation
                if generation is not None and not generation.done():
                    if not operation.stop_sent:
                        operation.stop_sent = True
                        generation.cancel()
                    done, _ = await asyncio.wait({generation}, timeout=STREAM_CLEANUP_TIMEOUT_SECONDS + DATABASE_SHUTDOWN_GRACE_SECONDS)
                    if not done:
                        operation.trace.cleanup_error_code = 'stream_cleanup_timeout'
                trace = operation.trace
                operation.attempt = next((a for a in reversed(self.traces.attempts)
                                          if a.request_id == request_id), None)
                operation.error_code = 'cancel_cleanup_failed' if trace.cleanup_error_code else None
                operation.phase = 'stopped'
            stage = 'finish_cancel'
            status = await self.storage.finish_cancel(operation.trace, operation.attempt,
                                                     error_code=operation.error_code)
            if status not in TERMINAL:
                raise RuntimeError('Cancellation did not reach terminal state')
            trace = operation.trace
            trace.status, trace.error_code = status, operation.error_code if status == 'failed' else None
            trace._persisted_terminal = True
            if operation.attempt is not None and operation.attempt.status in {'running', 'canceling'}:
                operation.attempt.status, operation.attempt.error_code = status, trace.error_code
            self.defer(trace, operation.attempt)
            self.cancellations.pop(request_id, None)
        except StorageUnavailable as exc:
            if operation.last_logged_stage != stage:
                log_storage_failure(request_id, stage, operation.attempt)
                operation.last_logged_stage = stage
            if not operation.ready.done():
                operation.ready.set_exception(exc)
        except GatewayError as exc:
            if not operation.ready.done():
                operation.ready.set_exception(exc)
            self.cancellations.pop(request_id, None)
        except Exception:
            if not operation.ready.done():
                operation.ready.set_exception(GatewayError('internal_error', '服务内部错误', 500))
            logging.getLogger(__name__).error('取消操作待恢复 request_id=%s', request_id)

    def _restore(self, row, trace):
        from services.models import RecoveryPolicy
        from services.routing import Router, endpoint_snapshot
        from trace_contracts import TraceRouting
        snapshot = loads(row['input_snapshot'])
        if snapshot.get('snapshot_version') == 2:
            from services.auto_routing import auto_configuration_snapshot
            router = Router(self.registry, self.prompt_registry, self.token_budget)
            body = StreamRequest.model_validate(snapshot['auto_request'])
            current = loads(dumps(auto_configuration_snapshot(router, body.model, body)))
            if snapshot['auto_configuration'] != current:
                raise ValueError('changed automatic routing configuration')
            plan = router.route(body, trace, streaming=True)
            if loads(dumps(plan.snapshot())) != snapshot:
                raise ValueError('changed automatic routing plan')
            return plan
        router = Router(self.registry, None, self.token_budget)
        # Legacy pending snapshots cannot prove an equivalent three-layer plan.
        # Never silently re-plan a persisted request after an upgrade or edit.
        if snapshot.get('snapshot_version') != 1:
            raise ValueError('legacy routing snapshot')
        # Persisted JSON keeps decimals exact; compare the live configuration
        # through the same codec rather than Decimal('0.1') to binary float .1.
        configuration = loads(dumps(router.configuration_snapshot(row['requested_model'])))
        metadata = TraceRouting.model_validate(snapshot['routing']).model_dump()
        if (snapshot['routing_configuration'] != configuration
                or metadata['config_fingerprint'] != router.configuration_fingerprint(row['requested_model'])):
            raise ValueError('changed routing configuration')
        names = [item['endpoint_id'] for item in metadata['candidates'] if item['status'] == 'eligible']
        if (not names or names != [entry['name'] for entry in snapshot['candidates']]
                or metadata['selected_endpoint'] != names[0]):
            raise ValueError('invalid routing candidates')
        candidates = []
        initial = snapshot['initial_output_tokens']
        for entry in snapshot['candidates']:
            endpoint = self.registry.get_endpoint(entry['name'])
            provider = self.registry.providers[endpoint.provider]
            if (endpoint_snapshot(endpoint) != entry['model'] or provider.model_dump() != entry['provider']
                    or self.token_budget.counter_for(endpoint).version != entry['counter_version']):
                raise ValueError('changed candidate')
            candidates.append((PreparedStream(entry['name'], endpoint, provider,
                [Message.model_validate(value) for value in entry['messages']], initial, snapshot['timeout_seconds']),
                self.providers[endpoint.provider]))
        trace.routing = metadata
        prepared, provider = candidates[0]
        policy = RecoveryPolicy.model_validate(snapshot['recovery'])
        execution = StreamingExecution(prepared, provider, trace, self.traces,
                                       storage=self.storage, defer=self.defer, snapshot=snapshot,
                                       recovery_policy=policy, candidates=candidates,
                                       authorized_output=snapshot['max_output_tokens'])
        execution.provider_admission = lambda start: self._admit_provider(trace.request_id, execution, start)
        return execution

    async def _admit_provider(self, request_id, execution, start):
        # Same boundary as cancel acceptance, including its uncertain ACK.
        # A plain status check outside this lock leaves check/start racing.
        async with self._claim_lock(request_id):
            row = await self.storage.get_stream(request_id)
            if row is not None and row['status'] == 'canceling':
                execution.cancel_requested = True
                execution.trace._cancel_requested = True
                return
            if row is None or row['status'] != 'running':
                raise StreamFailure('execution_interrupted')
            if not execution.cancel_requested:
                await start()

    async def _admit_classification(self, trace, start):
        async with self._claim_lock(trace.request_id):
            row = await self.storage.get_stream(trace.request_id)
            if row is not None and row['status'] == 'canceling':
                trace._cancel_requested = True
                raise asyncio.CancelledError
            if row is None or row['status'] != 'running':
                raise GatewayError('execution_interrupted', '请求执行已中断', 503)
            # Register the call before releasing the same lock used to accept
            # cancellation; await the task outside the lock so cancel can run.
            return asyncio.create_task(start())

    async def _generate(self, row, trace, deadline):
        request_id = trace.request_id
        try:
            try:
                execution = self._restore(row, trace)
            except (ValueError, KeyError, TypeError, GatewayError):
                trace.error_code = 'execution_configuration_changed'
                trace.status = await self.storage.finish(trace, error_code=trace.error_code,
                    event=('response.failed', {'request_id': request_id, 'code': trace.error_code,
                                              'message': '执行配置已变更'}), deadline=deadline)
                trace._persisted_terminal = True
                return
            if self.registry.get_auto_route(row['requested_model']) is not None:
                from services.gateway import GatewayService
                gateway = GatewayService(self.providers, self.traces, self.prompt_registry,
                    self.registry, self.token_budget, self.storage, self.defer, limiter=self.limiter)
                async with asyncio.timeout_at(deadline):
                    plan = await gateway.resolve_auto_plan(execution, trace, deadline,
                        provider_admission=lambda start: self._admit_classification(trace, start))
                execution = gateway.prepare_stream_plan(plan, trace)
                execution.provider_admission = lambda start: self._admit_provider(request_id, execution, start)
            self.executions[request_id] = execution
            execution.deadline = deadline
            execution.prepared_storage = True
            trace._admitted = True
            await execution.run_background()
        except TimeoutError:
            trace.error_code = ('request_timeout' if asyncio.get_running_loop().time() >= deadline
                                else 'internal_error')
        except asyncio.CancelledError:
            if not getattr(trace, '_persisted_terminal', False) and not getattr(trace, '_cancel_requested', False):
                trace.error_code = 'execution_interrupted'
        except StorageUnavailable:
            if not getattr(trace, '_persisted_terminal', False):
                trace.error_code = 'storage_unavailable'
        except (GatewayError, ProviderCallError) as exc:
            trace.error_code = exc.code
        except Exception:
            if not getattr(trace, '_persisted_terminal', False):
                trace.error_code = 'internal_error'
        finally:
            execution = self.executions.pop(request_id, None)
            if isinstance(execution, StreamingExecution):
                for task in execution.cleanup_tasks:
                    if not task.done():
                        self.cleanup_tasks.add(task)
                        task.add_done_callback(self.cleanup_tasks.discard)
            self._release_permit(request_id, execution if isinstance(execution, StreamingExecution) else None,
                                 asyncio.current_task())
            self.tasks.pop(request_id, None)
            self.active.discard(request_id)
            if not getattr(trace, '_cancel_requested', False):
                self.defer(trace, next((a for a in reversed(self.traces.attempts)
                                       if a.request_id == request_id), None))

    def _task_done(self, request_id, trace, task):
        self._release_permit(request_id)
        if not task.cancelled():
            task.exception()
        if self.tasks.get(request_id) is task:
            self.tasks.pop(request_id)
            self.active.discard(request_id)
            if not getattr(trace, '_cancel_requested', False):
                trace.error_code = trace.error_code or 'internal_error'
                self.defer(trace)

    def _release_permit(self, request_id, execution=None, task=None):
        permit = self.permits.pop(request_id, None)
        if permit is None:
            return
        tasks = set(execution.cleanup_tasks) if execution is not None else set()
        if task is not None:
            tasks.add(task)
        permit.release_when_done(tasks)

    async def reconcile(self):
        for request_id, operation in list(self.cancellations.items()):
            if not self.closing and (operation.task is None or operation.task.done()):
                self._start_cancel(request_id, operation)
        for request_id in list(self.orphans):
            # A pending retry may have committed while its task registration
            # is still in flight. Recovery shares the same ownership lock.
            async with self._claim_lock(request_id):
                if (request_id not in self.active and request_id not in self.cancellations
                        and not self.awaiting_persistence(request_id)):
                    await self.storage.reconcile(request_id)
                    self.orphans.discard(request_id)

    async def close(self):
        self.closing = True
        subscribers = list(self.subscriptions)
        for task in subscribers:
            task.cancel()
        if subscribers:
            await asyncio.wait(subscribers, timeout=STREAM_CLEANUP_TIMEOUT_SECONDS)
        tasks = list(self.tasks.values())
        for execution in self.executions.values():
            execution.stop_error = 'execution_interrupted'
        for task in tasks:
            task.cancel()
        tasks += [op.task for op in self.cancellations.values() if op.task is not None]
        tasks += list(self.cleanup_tasks)
        if tasks:
            # Each executor owns its one-second upstream cleanup. This outer
            # allowance also covers a task canceled before its first bytecode.
            done, pending = await asyncio.wait(tasks, timeout=STREAM_CLEANUP_TIMEOUT_SECONDS + DATABASE_SHUTDOWN_GRACE_SECONDS)
            for task in done:
                if not task.cancelled():
                    task.exception()
            if pending:
                for task in pending:
                    task.cancel()
        # A task canceled before _generate began cannot run its finally.
        for request_id in list(self.tasks):
            if self.tasks[request_id].done():
                self.tasks.pop(request_id)
                self.active.discard(request_id)
                self.orphans.add(request_id)
