from typing import Annotated
from uuid import UUID
import re
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from fastapi.routing import APIRoute
from starlette import status as http_status

from api.dependencies import get_gateway
from api.identity import owned_stream_id
from contracts import ErrorResponse, LLMRequest, ProbeResponse, SuccessResponse, StructuredLLMData, StreamRequest, StreamCreatedData, StreamCreatedResponse, StreamCancelData, StreamCancelResponse
from api.sse import EventStreamResponse
from persistence.database import StorageUnavailable
from persistence.trace_query import read_trace
from trace_contracts import TraceResponse
from errors import GatewayError
from utils.json_codec import dumps, loads
from services.gateway import GatewayService


class DecimalRequest(Request):
    async def json(self):
        return loads(await self.body())


class DecimalJSONRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def exact_json(request: Request):
            return await handler(DecimalRequest(request.scope, request.receive))

        return exact_json


router = APIRouter(route_class=DecimalJSONRoute)


def structured_response(response: SuccessResponse) -> Response:
    """Keep validated Decimal values as JSON numbers, including in parsed."""
    return Response(content=dumps(response.model_dump()), media_type='application/json')


@router.post("/v1/llm", response_model=SuccessResponse,
             description='普通文本或经过严格 JSON/Schema 验收的结构化输出。response_schema 使用平台子集；'
                         '完整关键字、引用和资源规则见项目 docs/response-schema.md；不提供规则查询接口。', responses={
    status: {"model": ErrorResponse} for status in (
        http_status.HTTP_400_BAD_REQUEST, http_status.HTTP_422_UNPROCESSABLE_CONTENT,
        http_status.HTTP_429_TOO_MANY_REQUESTS,
        http_status.HTTP_500_INTERNAL_SERVER_ERROR, http_status.HTTP_502_BAD_GATEWAY,
        http_status.HTTP_503_SERVICE_UNAVAILABLE, http_status.HTTP_504_GATEWAY_TIMEOUT,
    )
})
async def complete(
    body: LLMRequest, request: Request,
    gateway: Annotated[GatewayService, Depends(get_gateway)],
) -> SuccessResponse | Response:
    response = SuccessResponse(data=await gateway.complete(body, request.state.trace))
    return structured_response(response) if isinstance(response.data, StructuredLLMData) else response


@router.post('/v1/llm/stream', response_model=StreamCreatedResponse, status_code=202,
             description='保存pending请求并返回ID；首次GET事件接口才启动生成。',
             responses={code: {'model': ErrorResponse} for code in (
                 http_status.HTTP_400_BAD_REQUEST, http_status.HTTP_422_UNPROCESSABLE_CONTENT,
                 http_status.HTTP_500_INTERNAL_SERVER_ERROR, http_status.HTTP_503_SERVICE_UNAVAILABLE)})
async def stream(body: StreamRequest, request: Request,
                 gateway: Annotated[GatewayService, Depends(get_gateway)]) -> StreamCreatedResponse:
    trace = request.state.trace
    execution = gateway.prepare_stream(body, trace)
    trace._stream_creation = True
    try:
        await request.app.state.storage.create_pending(trace, execution.snapshot)
    except StorageUnavailable:
        trace.record_storage_failure('create_pending')
        raise
    trace.status = 'pending'
    return StreamCreatedResponse(data=StreamCreatedData(request_id=trace.request_id))


@router.get('/v1/llm/stream/{request_id}/events', response_class=Response,
            responses={200: {'content': {'text/event-stream': {}}},
                       204: {'description': '终态且无未读事件'},
                       **{code: {'model': ErrorResponse} for code in (
                           http_status.HTTP_400_BAD_REQUEST, http_status.HTTP_404_NOT_FOUND,
                           http_status.HTTP_422_UNPROCESSABLE_CONTENT,
                           http_status.HTTP_429_TOO_MANY_REQUESTS,
                           http_status.HTTP_500_INTERNAL_SERVER_ERROR, http_status.HTTP_503_SERVICE_UNAVAILABLE)}},
            description='从after_seq之后读取已提交事件；省略为0。断开订阅不取消生成。')
async def stream_events(request: Request, request_id: Annotated[str, Depends(owned_stream_id)]) -> Response:
    query = request.query_params
    values = query.getlist('after_seq')
    if set(query) - {'after_seq'} or len(values) > 1 or (values and re.fullmatch(r'[0-9]+', values[0]) is None):
        raise GatewayError('invalid_request', '事件游标不合法', 422)
    # Avoid Python's integer-string resource guard turning a bad cursor into500.
    significant = values[0].lstrip('0') if values else ''
    after_seq = int(significant) if len(significant) <= 19 and significant else (0 if not significant else 10**19)
    manager = request.app.state.streaming_manager
    row, on_first_text = await manager.subscribe(request_id, after_seq)
    request.state.generation_request_id = request_id
    if row['status'] in {'completed', 'failed', 'canceled'} and after_seq == row['last_event_seq']:
        return Response(status_code=204)
    from services.subscriptions import EventSubscription
    subscription = EventSubscription(request.app.state.storage, request_id, after_seq,
                                     on_first_text=on_first_text)
    return EventStreamResponse(subscription, owner=manager)


@router.post('/v1/llm/stream/{request_id}/cancel', response_model=StreamCancelResponse,
             responses={http_status.HTTP_202_ACCEPTED: {'model': StreamCancelResponse},
                        **{code: {'model': ErrorResponse} for code in (
                            http_status.HTTP_404_NOT_FOUND, http_status.HTTP_422_UNPROCESSABLE_CONTENT,
                            http_status.HTTP_500_INTERNAL_SERVER_ERROR, http_status.HTTP_503_SERVICE_UNAVAILABLE)}},
             description='发起或轮询取消；202表示canceling，200返回已有终态。断连不撤销已接受的取消。')
async def cancel_stream(request: Request, response: Response,
                        request_id: Annotated[str, Depends(owned_stream_id)]) -> StreamCancelResponse:
    if request.query_params or await request.body():
        raise GatewayError('invalid_request', '取消接口不接受请求体或查询参数', http_status.HTTP_422_UNPROCESSABLE_CONTENT)
    status = await request.app.state.streaming_manager.cancel(request_id)
    request.state.generation_request_id = request_id
    response.status_code = http_status.HTTP_202_ACCEPTED if status == 'canceling' else http_status.HTTP_200_OK
    return StreamCancelResponse(data=StreamCancelData(request_id=request_id, status=status),
                              message='请求正在取消' if status == 'canceling' else '请求已结束')


@router.post("/v1/models/{model}/health", response_model=ProbeResponse, responses={
    status: {"model": ErrorResponse} for status in (
        http_status.HTTP_400_BAD_REQUEST, http_status.HTTP_422_UNPROCESSABLE_CONTENT,
        http_status.HTTP_429_TOO_MANY_REQUESTS,
        http_status.HTTP_500_INTERNAL_SERVER_ERROR, http_status.HTTP_502_BAD_GATEWAY,
        http_status.HTTP_503_SERVICE_UNAVAILABLE, http_status.HTTP_504_GATEWAY_TIMEOUT,
    )
})
async def probe(model: str, request: Request, gateway: Annotated[GatewayService, Depends(get_gateway)]) -> ProbeResponse:
    if await request.body():
        raise GatewayError("invalid_request", "探测接口不接受请求体", http_status.HTTP_422_UNPROCESSABLE_CONTENT)
    return ProbeResponse(data=await gateway.probe(model, request.state.trace))


@router.get("/healthz")
async def liveness():
    return {"success": True, "code": "ok", "data": None, "message": "服务正常"}


@router.get('/v1/traces/{request_id}', response_model=TraceResponse,
            responses={code: {'model': ErrorResponse} for code in (404, 422, 503)},
            description='读取已提交的请求诊断元数据，按发生顺序列出各次模型尝试。')
async def trace_by_id(request_id: UUID, request: Request) -> TraceResponse:
    request.state.generation_request_id = str(request_id)
    try:
        data = await read_trace(request.app.state.storage.database, request_id, request.state.agent_id)
    except StorageUnavailable:
        logging.getLogger(__name__).error('request_id=%s stage=trace_query code=storage_unavailable', request_id)
        raise
    if data is None:
        raise GatewayError('not_found', '未找到请求记录', http_status.HTTP_404_NOT_FOUND)
    return TraceResponse(data=data)
