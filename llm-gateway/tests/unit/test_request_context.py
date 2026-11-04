from uuid import UUID

import pytest

from middleware.request_context import RequestContextMiddleware
from services.trace import TraceStore


async def receive():
    return {"type": "http.disconnect"}


async def test_non_http_scope_is_passed_through():
    scope = {"type": "lifespan"}
    observed = []

    async def send(message):
        observed.append(message)

    async def downstream(actual_scope, actual_receive, actual_send):
        assert actual_scope is scope
        assert actual_receive is receive
        assert actual_send is send
        await actual_send({"type": "lifespan.startup.complete"})

    traces = TraceStore()
    await RequestContextMiddleware(downstream, traces)(scope, receive, send)
    assert observed == [{"type": "lifespan.startup.complete"}]
    assert "state" not in scope
    assert traces.requests == {}
    assert traces.attempts == []


async def test_failure_after_response_start_does_not_send_second_response():
    scope = {"type": "http", "method": "POST", "path": "/v1/llm"}
    sent = []

    async def send(message):
        sent.append(message)

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("PRIVATE_ERROR_MARKER")

    traces = TraceStore()
    with pytest.raises(RuntimeError, match="PRIVATE_ERROR_MARKER"):
        await RequestContextMiddleware(downstream, traces)(scope, receive, send)
    assert len(sent) == 1
    assert sent[0]["type"] == "http.response.start"
    request_id = dict(sent[0]["headers"])[b"x-request-id"].decode()
    UUID(request_id)
    assert traces.requests[request_id].status == "running"
    assert traces.requests[request_id].attempts == 0


async def test_failed_final_body_send_is_not_confirmed_success():
    scope = {"type": "http", "method": "POST", "path": "/v1/llm"}
    sent = []

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body":
            raise ConnectionError("client disconnected")

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    traces = TraceStore()
    with pytest.raises(ConnectionError, match="client disconnected"):
        await RequestContextMiddleware(downstream, traces)(scope, receive, send)
    assert [message["type"] for message in sent] == ["http.response.start", "http.response.body"]
    assert len(traces.requests) == 1
    assert next(iter(traces.requests.values())).status == "running"


async def test_trace_finalizes_only_after_last_body_is_sent():
    scope = {"type": "http", "method": "POST", "path": "/v1/llm"}
    traces = TraceStore()
    statuses = []

    async def send(message):
        statuses.append(next(iter(traces.requests.values())).status)

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"o", "more_body": True})
        await send({"type": "http.response.body", "body": b"k", "more_body": False})

    await RequestContextMiddleware(downstream, traces)(scope, receive, send)
    assert statuses == ["running", "running", "running"]
    assert next(iter(traces.requests.values())).status == "completed"
