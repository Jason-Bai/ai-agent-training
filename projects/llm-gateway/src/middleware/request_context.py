from uuid import uuid4
import asyncio
import logging
import re

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette import status as http_status

from api.errors import error_response
from services.trace import TraceStore
from persistence.database import StorageUnavailable


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, traces: TraceStore, storage=None, active=None, defer=None):
        self.app = app
        self.traces = traces
        self.storage, self.active, self.defer = storage, active, defer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        request_id = str(uuid4())
        state["request_id"] = request_id
        trace = None
        is_probe = re.fullmatch(r"/v1/models/[^/]+/health", scope["path"]) is not None
        is_stream = scope['path'] == '/v1/llm/stream'
        if scope["method"] == "POST" and (scope["path"] == "/v1/llm" or is_probe or is_stream):
            operation = 'streaming' if is_stream else 'provider_probe' if is_probe else 'completion'
            trace = self.traces.start_request(request_id, operation=operation)
            trace.agent_id = state.get("agent_id")
            state["trace"] = trace
        persisted = trace is not None and self.storage is not None
        if persisted:
            self.active.add(request_id)
        response_started = False
        response_completed = False

        async def send_with_id(message):
            nonlocal response_started, response_completed
            if message["type"] == "http.response.start":
                if persisted and state.get('error_code') and not getattr(trace, '_persisted_terminal', False):
                    attempt = next((a for a in reversed(self.traces.attempts) if a.request_id == request_id), None)
                    if trace.error_stage is None and not getattr(trace, '_admitted', False):
                        trace.error_stage = 'storage' if state['error_code'] == 'storage_unavailable' else 'admission'
                    if state['error_code'] in {'storage_unavailable', 'request_timeout', 'provider_timeout'} or getattr(trace, '_stream_creation', False):
                        trace.error_code = state['error_code']
                        self.defer(trace, attempt)
                    else:
                        try:
                            trace.status = await self.storage.finish(trace, attempt, error_code=state['error_code'])
                            trace.error_code = state['error_code'] if trace.status == 'failed' else None
                            trace._persisted_terminal = True
                        except StorageUnavailable:
                            trace.error_code = state['error_code']
                            self.defer(trace, attempt)
                            logging.getLogger(__name__).warning(
                                '请求错误记录待恢复 request_id=%s stage=finish code=%s', request_id, state['error_code'])
                MutableHeaders(scope=message)["X-Request-ID"] = state.get("generation_request_id", request_id)
                response_started = True
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_completed = True

        try:
            await self.app(scope, receive, send_with_id)
        except asyncio.CancelledError:
            if persisted:
                attempt = next((a for a in reversed(self.traces.attempts) if a.request_id == request_id), None)
                if getattr(trace, '_persisted_terminal', False):
                    trace.delivery_error_code = 'client_disconnected'
                else:
                    trace.error_code = trace.error_code or 'internal_error'
                self.defer(trace, attempt)
            raise
        except Exception as exc:
            state["error_code"] = "internal_error"
            if persisted and getattr(trace, '_persisted_terminal', False):
                trace.delivery_error_code = 'client_disconnected' if isinstance(exc, OSError) else 'internal_error'
                attempt = next((a for a in reversed(self.traces.attempts) if a.request_id == request_id), None)
                self.defer(trace, attempt)
            if response_started:
                raise
            response = error_response(state, http_status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error", "服务内部错误")
            await response(scope, receive, send_with_id)
        finally:
            if persisted:
                self.active.discard(request_id)
            # The stream executor owns its lifecycle once the response starts.
            if trace is not None and response_completed and not state.get('stream_execution_owned') and not getattr(trace, '_stream_creation', False):
                if not getattr(trace, '_persisted_terminal', False):
                    self.traces.finish_request(trace, error_code=state.get("error_code"))
