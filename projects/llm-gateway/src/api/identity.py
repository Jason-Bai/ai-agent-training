"""Persisted Gateway credentials and request ownership for internal Agents."""
import hashlib
import hmac
import re
import secrets
from uuid import UUID, uuid4

from fastapi import Request
from sqlalchemy import text
from starlette._utils import get_route_path
from starlette.datastructures import Headers

from api.errors import error_response
from errors import GatewayError


_BEARER = re.compile(r'Bearer ([0-9a-f]{32})\.([A-Za-z0-9_-]{43})', re.IGNORECASE | re.ASCII)


async def create_agent(session, agent_id, name):
    if not isinstance(agent_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', agent_id):
        raise ValueError('invalid agent id')
    if not isinstance(name, str) or not name.strip() or len(name) > 256:
        raise ValueError('invalid agent name')
    created = await session.scalar(text('INSERT INTO tb_agent(agent_id,name) VALUES (:id,:name) '
        'ON CONFLICT DO NOTHING RETURNING agent_id'), {'id': agent_id, 'name': name})
    if created is None:
        raise ValueError('agent already exists')


async def issue_credential(session, agent_id):
    public_id, secret = uuid4().hex, secrets.token_urlsafe(32)
    issued = await session.scalar(text('INSERT INTO tb_agent_credential(public_id,agent_id,digest,created_at) '
        'SELECT :public,agent_id,:digest,now() FROM tb_agent WHERE agent_id=:agent RETURNING public_id'),
        {'public': public_id, 'agent': agent_id, 'digest': hashlib.sha256(secret.encode()).hexdigest()})
    if issued is None:
        raise ValueError('agent not found')
    return f'{public_id}.{secret}'


async def revoke_credential(session, agent_id, public_id):
    revoked = await session.scalar(text('UPDATE tb_agent_credential SET revoked_at=now() '
        'WHERE agent_id=:agent AND public_id=:public AND revoked_at IS NULL RETURNING public_id'),
        {'agent': agent_id, 'public': public_id})
    if revoked is None:
        raise ValueError('active credential not found')


async def rotate_credential(session, agent_id, public_id):
    await revoke_credential(session, agent_id, public_id)
    return await issue_credential(session, agent_id)


async def owned_stream_id(request_id: UUID, request: Request) -> str:
    """Check the resource selected by FastAPI before subscribing or canceling."""
    async with request.app.state.storage.database.transaction() as session:
        allowed = await session.scalar(text('SELECT 1 FROM tb_llm_request WHERE request_id=:rid '
            "AND agent_id=:agent AND operation='streaming'"), {'rid': request_id, 'agent': request.state.agent_id})
    if allowed is None:
        raise GatewayError('not_found', '未找到请求记录', 404)
    return str(request_id)


class AgentIdentityMiddleware:
    def __init__(self, app, database):
        self.app, self.database = app, database

    async def __call__(self, scope, receive, send):
        path = get_route_path(scope) if scope['type'] == 'http' else ''
        if not path.startswith('/v1/'):
            return await self.app(scope, receive, send)
        state = scope.setdefault('state', {})
        try:
            values = Headers(scope=scope).getlist('authorization')
            match = _BEARER.fullmatch(values[0]) if len(values) == 1 else None
            if match is None:
                raise GatewayError('unauthorized', '凭据无效', 401)
            public_id, secret = match.groups()
            async with self.database.transaction() as session:
                result = (await session.execute(text('SELECT agent_id,digest FROM tb_agent_credential '
                    'WHERE public_id=:public AND revoked_at IS NULL'), {'public': public_id})).first()
                if result is None or not hmac.compare_digest(result.digest, hashlib.sha256(secret.encode()).hexdigest()):
                    raise GatewayError('unauthorized', '凭据无效', 401)
                state['agent_id'] = result.agent_id
        except GatewayError as exc:
            headers = {'X-Request-ID': str(uuid4())}
            if exc.status_code == 401:
                headers['WWW-Authenticate'] = 'Bearer'
            response = error_response(state, exc.status_code, exc.code, exc.message, headers)
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)
