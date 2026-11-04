"""Authenticated read-only views of an Agent's billing account."""
from datetime import datetime, timezone
import re
from uuid import UUID

from fastapi import APIRouter, Request

from errors import GatewayError
from persistence.billing_query import read_detail, read_requests, read_summary
from utils.json_codec import dumps
from starlette.responses import Response


router = APIRouter(prefix='/v1/billing')


def success(data):
    # Financial amounts are strings on the wire. FastAPI's generic encoder can
    # convert Decimal to float before our JSON codec sees the original value.
    return Response(dumps({'success': True, 'code': 'ok', 'message': '成功', 'data': data}),
                    media_type='application/json')


def reject():
    return GatewayError('invalid_request', '账务查询参数不合法', 422)


def instant(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass
    raise reject()


@router.get('/summary')
async def summary(request: Request):
    if request.query_params:
        raise reject()
    data = await read_summary(request.app.state.storage.database, request.state.agent_id)
    return success(data)


@router.get('/requests')
async def list_requests(request: Request):
    query = request.query_params
    if set(query) - {'from', 'to', 'cursor', 'limit'} or any(len(query.getlist(k)) != 1 for k in query):
        raise reject()
    limit = query.get('limit', '50')
    if not re.fullmatch(r'[1-9][0-9]{0,2}', limit) or int(limit) > 100:
        raise reject()
    start = instant(query['from']) if 'from' in query else None
    end = instant(query['to']) if 'to' in query else None
    if start is not None and end is not None and start >= end:
        raise reject()
    cursor = None
    if 'cursor' in query:
        stamp, sep, seq = query['cursor'].rpartition('|')
        if not sep or not re.fullmatch(r'[1-9][0-9]{0,18}', seq) or int(seq) > 2**63 - 1:
            raise reject()
        cursor = (instant(stamp), int(seq))
    data = await read_requests(request.app.state.storage.database, request.state.agent_id,
                               int(limit), start, end, cursor)
    return success(data)


@router.get('/requests/{request_id}')
async def request_details(request_id: UUID, request: Request):
    if request.query_params:
        raise reject()
    data = await read_detail(request.app.state.storage.database, request.state.agent_id, request_id)
    if data is None:
        raise GatewayError('not_found', '未找到请求记录', 404)
    return success(data)
