from fastapi.testclient import TestClient

from f4_cases import make_app


def test_empty_response_schema_is_preflight_failure(provider, payload, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('F4 contract tests must inject their runtime registry')
    monkeypatch.setattr('main.load_registry', forbidden)
    payload['response_schema'] = {}
    with TestClient(make_app(provider)) as client:
        response = client.post('/v1/llm', json=payload)
    assert response.status_code == 400
    assert response.json()['code'] == 'invalid_response_schema'
    assert provider.calls == []
