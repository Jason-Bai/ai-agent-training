"""Verify real adapter bytes, Gateway errors and independently committed usage."""
import json

import httpx
import pytest

from persistence.database import DatabaseConfig
from test_f3_gateway_probe import application
from test_providers import adapter_type, server


@pytest.mark.parametrize('model', ['wire-model', None, 'unsafe\x00model'])
@pytest.mark.parametrize('valid_usage', [True, False])
@pytest.mark.parametrize('stream', [False, True])
async def test_f8_unsupported_tool_result_preserves_only_valid_usage(
        server, adapter_type, legacy_postgres_environment, monkeypatch, valid_usage, stream, model):
    pg, template = legacy_postgres_environment
    name = pg.create_database(template=template)
    monkeypatch.setattr('main.load_database_config', lambda directory: DatabaseConfig(**pg.config(name)))
    app, adapter, kind = application(adapter_type, server)
    total = 85 if valid_usage else 86
    if kind == 'deepseek':
        response = {'object': 'chat.completion.chunk' if stream else 'chat.completion', 'model': 'wire-model',
                    'choices': [{'index': 0, 'delta' if stream else 'message': {
                        'role': 'assistant', 'tool_calls': [{'type': 'function'}]}, 'finish_reason': 'tool_calls'}],
                    'usage': {'prompt_tokens': 60, 'completion_tokens': 25, 'total_tokens': total}}
    else:
        response = {'object': 'response', 'status': 'completed', 'model': 'wire-model',
                    'output': [{'type': 'function_call'}],
                    'usage': {'input_tokens': 60, 'output_tokens': 25, 'total_tokens': total}}
    if model is None:
        del response['model']
    else:
        response['model'] = model
    if stream and kind != 'deepseek':
        response = {'type': 'response.completed', 'response': response}
    server.raw = (('data: ' + json.dumps(response) + '\n\n') if stream else json.dumps(response)).encode()
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                if stream:
                    created = await client.post('/v1/llm/stream', json={
                        'model': 'general-primary', 'prompt': {'name':'basic_text','version':'1.0.0'},
                        'messages':[{'role':'user','content':'hello'}]})
                    assert created.status_code == 202
                    rid = created.headers['x-request-id']
                    assert 'response.failed' in (await client.get(f'/v1/llm/stream/{rid}/events')).text
                else:
                    failed = await client.post('/v1/models/general-primary/health')
                    assert failed.status_code == 502 and failed.json()['code'] == 'provider_invalid_response'
                    rid = failed.headers['x-request-id']
                trace = (await client.get(f'/v1/traces/{rid}')).json()['data']
                assert len(server.calls) == 1 and trace['error_code'] == 'provider_invalid_response'
                with pg.connect(name) as conn:
                    saved = conn.execute('SELECT a.input_tokens,a.output_tokens,a.total_tokens,a.status,r.status,a.actual_model FROM tb_llm_attempt a '
                                         'JOIN tb_llm_request r ON a.request_pk=r.id WHERE r.request_id=%s',
                                         (rid,)).fetchall()
                expected_model = model if model == 'wire-model' else None
                counts = (60, 25, 85) if valid_usage else (None, None, None)
                assert saved == [(*counts, 'failed', 'failed', expected_model)]
                assert trace['attempts'][0]['actual_model'] == expected_model
                subtotal = {'input_tokens':60,'output_tokens':25,'total_tokens':85} if valid_usage else None
                assert trace['usage'] == {'known_subtotal': subtotal, 'unknown_attempts': [] if valid_usage else [1]}
    finally:
        await adapter.aclose()
        pg.drop_database(name)
