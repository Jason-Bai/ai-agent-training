"""One streaming call: awaited output, fixed deadline, and owned stream cleanup."""
import asyncio
from dataclasses import dataclass, replace
from time import monotonic

from pydantic import ValidationError

from constants import STREAM_CLEANUP_TIMEOUT_SECONDS, STREAM_HEARTBEAT_SECONDS
from contracts import Message, Usage
from errors import GatewayError, PROVIDER_MESSAGES, ProviderCallError
from providers.base import ProviderStreamEvent
from services.models import ModelConfig, ProviderConfig
from persistence.database import StorageUnavailable


@dataclass(frozen=True)
class PreparedStream:
    model_name: str
    model: ModelConfig
    definition: ProviderConfig
    messages: list[Message]
    max_output_tokens: int
    timeout_seconds: int


class StreamFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class StreamingExecution:
    def __init__(self, prepared, provider, trace, traces, *, storage=None, defer=None, snapshot=None,
                 recovery_policy=None, candidates=None, authorized_output=None):
        self.prepared, self.provider = prepared, provider
        self.trace, self.traces = trace, traces
        from services.models import RecoveryPolicy
        from services.recovery import RecoveryState
        policy = recovery_policy or RecoveryPolicy(max_attempts=1, max_attempts_per_endpoint=1)
        self.recovery = RecoveryState(policy, prepared.max_output_tokens, authorized_output or prepared.max_output_tokens)
        self.candidates = candidates or [(prepared, provider)]
        self.content_committed = False
        self.attempt = None
        self.iterator = self.next_task = None
        self.started_response = self.terminal_attempted = self.completed_sent = False
        self.has_nonblank = False
        self.last_sent_at = None
        self.deadline = None
        self.storage, self.defer, self.snapshot = storage, defer, snapshot
        if snapshot is not None:
            self.trace._skipped_candidates = snapshot.get('skipped_candidates')
        self.prepared_storage = False
        self.background = False
        self.stop_error = None
        self.cancel_requested = False
        self.provider_admission = None
        self.cleanup_tasks = set()
        self.completed_committed = False
        self.storage_uncertain = False
        self.committed_status = self.committed_error = None

    async def prepare(self):
        if self.prepared_storage:
            return
        self.trace.stream_started_at = monotonic()
        self.deadline = asyncio.get_running_loop().time() + self.prepared.timeout_seconds
        if self.storage is not None:
            try:
                await self.storage.prepare(self.trace, self.snapshot, deadline=self.deadline)
                self.trace._admitted = True
            except StorageUnavailable:
                self.trace.record_storage_failure('prepare')
                self.trace.error_code = 'storage_unavailable'
                self.defer(self.trace)
                raise
        self.prepared_storage = True

    async def run_background(self):
        if self.cancel_requested:
            return
        self.background = True

        async def no_transport(*args):
            pass

        await self.run(no_transport, no_transport, asyncio.Event())

    async def run(self, start, send_event, disconnected):
        loop = asyncio.get_running_loop()
        await self.prepare()
        self.send_event, self.disconnected = send_event, disconnected
        worker = asyncio.create_task(self._work(start))
        disconnect = asyncio.create_task(disconnected.wait())
        error = None
        cancelled = False
        try:
            async with asyncio.timeout_at(self.deadline):
                done, _ = await asyncio.wait({worker, disconnect}, return_when=asyncio.FIRST_COMPLETED)
                if self.completed_committed or self.completed_sent:
                    await worker
                elif disconnect in done:
                    raise StreamFailure('client_disconnected')
                else:
                    await worker
        except TimeoutError:
            error = 'request_timeout' if loop.time() >= self.deadline else 'internal_error'
        except StreamFailure as exc:
            error = exc.code
        except asyncio.CancelledError:
            error = self.stop_error or ('client_disconnected' if disconnected.is_set() else 'internal_error')
            cancelled = True
        except Exception:
            error = 'internal_error'
        finally:
            # The cleanup task owns its fixed grace period even if the caller
            # cancels this task again while a network response is closing.
            cleanup = asyncio.create_task(self._cleanup(worker, disconnect))
            try:
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                cleanup.result()
                if self.committed_status is not None and error != self.committed_error:
                    self.trace.delivery_error_code = error
                if self.committed_status is not None:
                    error = self.committed_error
                elif self.completed_sent:
                    error = None
                elif cancelled:
                    error = self.stop_error or ('client_disconnected' if disconnected.is_set() else 'internal_error')
                elif error is None:
                    error = 'provider_invalid_response'
                if error and not self.cancel_requested and not cancelled and not disconnected.is_set() and not self.storage_uncertain:
                    error = await self._notify_failure(error)
            except asyncio.CancelledError:
                cancelled = True
                error = self.stop_error or ('client_disconnected' if disconnected.is_set() else 'internal_error')
            finally:
                # Final storage work is queued after this execution stops.
                if self.committed_status is not None and error != self.committed_error:
                    self.trace.delivery_error_code = error
                if self.completed_committed or self.completed_sent:
                    error = None
                self.trace.stream_ended_at = monotonic()
                if self.cancel_requested:
                    if getattr(self.trace, '_persisted_terminal', False):
                        error = self.trace.error_code
                    else:
                        error = None
                        self.trace.status, self.trace.error_code = 'canceling', None
                if self.attempt is not None and self.attempt.status in {'running', 'canceling'}:
                    self.attempt.status = self.trace.status if self.cancel_requested else 'failed' if error else 'completed'
                    self.attempt.error_code = error
                    self.attempt.latency_ms = (monotonic() - self.attempt_started) * 1000
                if self.storage is not None and not self.cancel_requested:
                    if self.committed_status is not None:
                        self.trace.status, self.trace.error_code = self.committed_status, self.committed_error
                        self.trace._persisted_terminal = True
                    elif not getattr(self.trace, '_persisted_terminal', False):
                        self.trace.status, self.trace.error_code = 'failed', error
                    # Final diagnostics and any interrupted commit are reconciled
                    # outside the response deadline, after this execution stops.
                    self.defer(self.trace, self.attempt)
                elif not self.cancel_requested:
                    self.traces.finish_request(self.trace, error_code=error)
        if cancelled:
            raise asyncio.CancelledError

    async def _emit(self, name, data):
        if self.cancel_requested:
            raise asyncio.CancelledError
        if self.disconnected.is_set():
            raise StreamFailure('client_disconnected')
        if asyncio.get_running_loop().time() >= self.deadline:
            raise StreamFailure('request_timeout')
        if name in {'response.completed', 'response.failed'}:
            self.terminal_attempted = True
        payload = {'request_id': self.trace.request_id, **data}
        if self.storage is not None and name != 'response.heartbeat':
            try:
                if name == 'response.delta':
                    await self.storage.append_event(self.trace, name, payload, deadline=self.deadline)
                    self.content_committed = True
                else:
                    code = data.get('code') if name == 'response.failed' else None
                    if code is not None and self.attempt is not None:
                        self.attempt.error_code = code
                    status = await self.storage.finish(self.trace, self.attempt, error_code=code,
                                                       event=(name, payload), deadline=self.deadline)
                    if status == 'canceling':
                        self.cancel_requested = True
                        self.trace._cancel_requested = True
                        raise asyncio.CancelledError
                    self.committed_status, self.committed_error = status, code if status == 'failed' else None
                    self.trace._persisted_terminal = True
                    if name == 'response.completed':
                        if status != 'completed':
                            raise StreamFailure('execution_interrupted')
                        self.completed_committed = True
            except StorageUnavailable as exc:
                self.trace.record_storage_failure('append_event' if name == 'response.delta' else 'finish', self.attempt)
                self.storage_uncertain |= exc.uncertain
                if asyncio.current_task().cancelling():
                    raise asyncio.CancelledError
                raise StreamFailure('storage_unavailable') from None
        if name == 'response.delta':
            self.content_committed = True
        if self.background:
            if name == 'response.completed' and self.storage is None:
                self.completed_sent = True
            return
        try:
            await self.send_event(name, payload)
        except OSError:
            raise StreamFailure('client_disconnected') from None
        self.last_sent_at = monotonic()
        if name == 'response.delta' and self.trace.first_text_sent_at is None:
            self.trace.first_text_sent_at = self.last_sent_at
        if name == 'response.completed':
            self.completed_sent = True

    def _heartbeat_delay(self):
        if self.background:
            return None
        return max(0, STREAM_HEARTBEAT_SECONDS - (monotonic() - self.last_sent_at))

    async def _work(self, start):
        if self.cancel_requested:
            return
        try:
            await start()
        except OSError:
            raise StreamFailure('client_disconnected') from None
        self.started_response = True
        self.last_sent_at = monotonic()
        if self.disconnected.is_set():
            raise StreamFailure('client_disconnected')
        if asyncio.get_running_loop().time() >= self.deadline:
            raise StreamFailure('request_timeout')
        self.trace.recovery_wait_ms = 0.0
        index, self.attempt_reason = 0, 'initial'
        last_error = None
        while True:
            p, provider = self.candidates[index]
            remaining = self.deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise StreamFailure('request_timeout')
            wait = self.recovery.wait_remaining(p.definition, asyncio.get_running_loop().time())
            if wait >= remaining:
                if index + 1 < len(self.candidates):
                    index += 1
                    self.attempt_reason = 'fallback'
                    continue
                self.trace._recovery_stop_reason = 'retry_wait_exceeds_deadline'
                raise StreamFailure(last_error.code)
            if wait:
                await self.trace.wait_for_recovery(wait)
            if self.cancel_requested:
                return
            self.prepared = replace(p, max_output_tokens=self.recovery.current_output)
            self.provider = provider
            self.trace.error_stage = None
            self.recovery.start(p.model_name)
            self.trace.upstream_first_text_at = self.trace.upstream_terminal_at = None
            self.iterator = self.next_task = None
            try:
                await self._single_attempt()
                return
            except TimeoutError:
                error = StreamFailure('request_timeout' if asyncio.get_running_loop().time() >= self.deadline else 'provider_timeout')
            except (StreamFailure, ProviderCallError) as exc:
                error = exc
            if self.attempt is not None:
                self.attempt.status, self.attempt.error_code = 'failed', error.code
                self.attempt.latency_ms = (monotonic() - self.attempt_started) * 1000
            # End ownership of this iterator before any subsequent call.
            await self._cleanup(None, None)
            self.iterator = self.next_task = None
            if self.trace.cleanup_error_code or self.cancel_requested or self.storage_uncertain:
                raise StreamFailure(error.code)
            if self.storage is not None and self.attempt is not None:
                try:
                    status = await self.storage.finish_attempt(self.trace, self.attempt, deadline=self.deadline)
                except StorageUnavailable as exc:
                    self.trace.record_storage_failure('finish_attempt', self.attempt)
                    self.storage_uncertain |= exc.uncertain
                    raise StreamFailure('storage_unavailable') from None
                if status == 'canceling':
                    self.cancel_requested = True
                    self.trace._cancel_requested = True
                    raise asyncio.CancelledError
                if status != 'running':
                    raise StreamFailure('execution_interrupted')
            decision = self.recovery.decide(error, p.model_name, has_content=self.content_committed)
            if decision == 'stop':
                self.trace._recovery_stop_reason = self.recovery.stop_reason
                raise StreamFailure(error.code)
            last_error = error
            if decision == 'grow':
                self.attempt_reason = 'output_growth'
                continue
            delay = (getattr(error, 'retry_after_seconds', None) or 0) if decision == 'fallback' else self.recovery.delay(error, p.model_name)
            advance = decision == 'fallback' or delay >= self.deadline - asyncio.get_running_loop().time()
            self.recovery.note_wait(p.definition, (getattr(error, 'retry_after_seconds', None) or 0) if advance else delay,
                                    asyncio.get_running_loop().time())
            if advance:
                if index + 1 >= len(self.candidates):
                    self.trace._recovery_stop_reason = 'no_eligible_fallback'
                    raise StreamFailure(error.code)
                index += 1
                self.attempt_reason = 'fallback'
            else:
                self.attempt_reason = 'network_retry'

    def _usage_payload(self):
        attempts = [a for a in self.traces.attempts if a.request_id == self.trace.request_id]
        if not attempts or any(a.usage is None for a in attempts):
            return None
        return {key: sum(getattr(a.usage, key) for a in attempts) for key in Usage.model_fields}

    async def _single_attempt(self):
        p = self.prepared
        self.attempt = self.traces.start_attempt(
            self.trace, p.model_name, provider=p.model.provider, adapter=p.definition.adapter,
            configured_model=p.model.model_id, api_protocol=p.definition.api_protocol)
        self.attempt.reason = self.attempt_reason
        self.attempt.input_snapshot = {'messages': [m.model_dump() for m in p.messages], 'response_schema': None}
        from services.prompts import fingerprint_messages
        self.attempt.prompt_fingerprint = fingerprint_messages(p.messages)
        self.attempt.max_output_tokens = p.max_output_tokens
        if self.snapshot is not None:
            entry = next((c for c in self.snapshot.get('candidates', []) if c['name'] == p.model_name), {})
            self.attempt.input_token_count = entry.get('input_token_count', self.trace.input_token_count)
            self.attempt.counter_version = entry.get('counter_version', self.trace.counter_version)
            self.attempt.input_token_kind = entry.get('input_token_kind', self.trace.input_token_kind)
        self.attempt_started = monotonic()
        if self.storage is not None:
            try:
                await self.storage.begin_attempt(self.trace, self.attempt, deadline=self.deadline)
            except StorageUnavailable as exc:
                self.trace.record_storage_failure('begin_attempt', self.attempt)
                self.storage_uncertain |= exc.uncertain
                if asyncio.current_task().cancelling():
                    raise asyncio.CancelledError
                raise StreamFailure('storage_unavailable') from None
            except GatewayError as exc:
                raise StreamFailure(exc.code) from None
            finally:
                del self.attempt.input_snapshot
        else:
            del self.attempt.input_snapshot
        if self.cancel_requested:
            return
        self.provider_started_at = None
        try:
            if self.provider_admission is None:
                await self._start_provider()
            else:
                await self.provider_admission(self._start_provider)
            if self.cancel_requested:
                return
            async with asyncio.timeout_at(self.provider_deadline):
                usage = await self._read_events()
        except StorageUnavailable as exc:
            self.trace.record_storage_failure('admission', self.attempt)
            self.storage_uncertain |= exc.uncertain
            raise StreamFailure('storage_unavailable') from None
        except BaseException:
            if self.trace.error_stage is None:
                self.trace.error_stage = 'provider' if self.provider_started_at is not None else 'admission'
            raise
        finally:
            if self.provider_started_at is not None and self.attempt.provider_duration_ms is None:
                self.attempt.provider_duration_ms = (monotonic() - self.provider_started_at) * 1000
        await self._emit('response.completed', {'usage': usage})

    async def _read_events(self):
        while True:
            # Ready metadata/empty events are not downstream activity. Check
            # the idle clock even when the upstream never blocks between them.
            if self._heartbeat_delay() == 0:
                await self._emit('response.heartbeat', {})
            if self.next_task is None:
                self.next_task = asyncio.create_task(anext(self.iterator))
            while not self.next_task.done():
                done, _ = await asyncio.wait({self.next_task}, timeout=self._heartbeat_delay())
                if not done:
                    await self._emit('response.heartbeat', {})
            try:
                raw = self.next_task.result()
            except StopAsyncIteration:
                raise StreamFailure('provider_invalid_response') from None
            self.next_task = None
            event = self._validate_event(raw)
            self._observe(event)
            if event.delta:
                self.has_nonblank |= bool(event.delta.strip())
                await self._emit('response.delta', {'delta': event.delta})
            if event.status == 'failed':
                error = StreamFailure(event.error_code)
                for field in ('transient', 'retry_after_seconds', 'upstream_http_status', 'upstream_finish_reason'):
                    setattr(error, field, getattr(event, field))
                raise error
            if event.status == 'completed':
                if not self.has_nonblank:
                    raise StreamFailure('provider_invalid_response')
                return self._usage_payload()

    async def _start_provider(self):
        p = self.prepared
        remaining = min(p.model.max_timeout_seconds, max(0, self.deadline - asyncio.get_running_loop().time()))
        if remaining <= 0:
            raise StreamFailure('request_timeout')
        self.provider_deadline = asyncio.get_running_loop().time() + remaining
        self.provider_started_at = monotonic()
        self.iterator = self.provider.stream(p.model, p.messages,
                                            max_output_tokens=p.max_output_tokens, timeout_seconds=remaining)
        self.next_task = asyncio.create_task(anext(self.iterator))
        # Admission remains locked until this invocation has entered its first
        # read. Release after one scheduling turn, not after first content.
        await asyncio.sleep(0)
        self.attempt.provider_entered = True

    @staticmethod
    def _validate_event(raw):
        if not isinstance(raw, ProviderStreamEvent):
            raise StreamFailure('provider_invalid_response')
        try:
            # Recheck values even for test/third-party model_construct objects.
            values = {name: getattr(raw, name) for name in ProviderStreamEvent.model_fields}
            if isinstance(values['usage'], Usage):
                values['usage'] = {name: getattr(values['usage'], name) for name in Usage.model_fields}
            return ProviderStreamEvent.model_validate(values)
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise StreamFailure('provider_invalid_response') from None

    def _observe(self, event):
        if event.status != 'running':
            self.attempt.provider_duration_ms = (monotonic() - self.provider_started_at) * 1000
        if event.delta and self.trace.upstream_first_text_at is None:
            self.trace.upstream_first_text_at = monotonic()
        if event.upstream_terminal_received:
            self.trace.upstream_terminal_at = monotonic()
        for name in ('actual_model', 'usage', 'cached_input_tokens', 'billing_observed_at', 'upstream_status', 'upstream_finish_reason'):
            value = getattr(event, name)
            if value is not None:
                setattr(self.attempt, name, value)
        if event.status == 'failed':
            self.attempt.provider_error_code = event.error_code

    async def _cleanup(self, worker, disconnect):
        tasks = {task for task in (worker, disconnect, self.next_task) if task is not None}

        def collect(task):
            self.cleanup_tasks.discard(task)
            if not task.cancelled():
                task.exception()

        def track(task):
            self.cleanup_tasks.add(task)
            task.add_done_callback(collect)

        async def close():
            interrupted = {task for task in tasks if not task.done()}
            for task in tasks:
                if not task.done():
                    task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            if any(task in interrupted and isinstance(result, BaseException)
                   and not isinstance(result, asyncio.CancelledError)
                   for task, result in zip(tasks, results)):
                self.trace.cleanup_error_code = 'stream_cleanup_error'
            if self.iterator is not None:
                await self.iterator.aclose()

        for task in tasks:
            track(task)
        closing = asyncio.create_task(close())
        track(closing)
        done, _ = await asyncio.wait({closing}, timeout=STREAM_CLEANUP_TIMEOUT_SECONDS)
        if not done:
            self.trace.cleanup_error_code = 'stream_cleanup_timeout'
            closing.cancel()
        else:
            try:
                closing.result()
            except (Exception, asyncio.CancelledError):
                self.trace.cleanup_error_code = 'stream_cleanup_error'

    async def _notify_failure(self, error):
        if not self.started_response or self.terminal_attempted:
            return error
        messages = {'request_timeout': '请求处理超时', 'internal_error': '服务内部错误',
                    'client_disconnected': '调用方已断开连接'}
        message = PROVIDER_MESSAGES.get(error, messages.get(error, '模型调用失败'))
        # No second writer may overlap a cancelled operation that failed cleanup.
        if self.trace.cleanup_error_code == 'stream_cleanup_timeout':
            return error
        try:
            remaining = self.deadline - asyncio.get_running_loop().time()
            if remaining > 0:
                async with asyncio.timeout_at(self.deadline):
                    await self._emit('response.failed', {'code': error, 'message': message})
            else:
                # One scheduling turn is best effort, never a new send budget.
                if self.storage is not None:
                    return error
                self.terminal_attempted = True
                notice = asyncio.create_task(self.send_event('response.failed', {
                    'request_id': self.trace.request_id, 'code': error, 'message': message}))
                try:
                    await asyncio.sleep(0)
                finally:
                    if not notice.done():
                        notice.cancel()
                    await asyncio.gather(notice, return_exceptions=True)
        except TimeoutError:
            return 'request_timeout'
        except (OSError, StreamFailure) as exc:
            return exc.code if isinstance(exc, StreamFailure) else 'client_disconnected'
        except Exception:
            return 'internal_error'
        return error
