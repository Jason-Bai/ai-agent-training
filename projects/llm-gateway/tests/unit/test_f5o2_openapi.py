"""The published contract must describe the actual unified error envelope."""
import pytest

from f4_cases import registry
from main import create_app
from persistence.database import Database, DatabaseConfig
from providers.mock import MockProvider
from settings import Settings


@pytest.mark.parametrize('method,path,codes',[
    ('post','/v1/llm/stream',{400,422,500,503}),
    ('get','/v1/llm/stream/{request_id}/events',{400,404,422,500,503}),
])
async def test_openapi_stream_errors_use_the_runtime_envelope(tmp_path,method,path,codes):
    (tmp_path/'password').write_text('synthetic-only')
    database=Database(DatabaseConfig(host='127.0.0.1',port=1,name='test',username='test',
        password_file='password'),tmp_path)
    try:
        app=create_app(settings=Settings(env='test',_env_file=None),provider=MockProvider(),
                       model_registry=registry(),database=database,config_dir=tmp_path)
        response=app.openapi()['paths'][path][method]['responses']
        assert response['422']['content']['application/json']['schema']=={'$ref':'#/components/schemas/ErrorResponse'}
        assert codes<={int(code) for code in response}
        for code in codes:
            assert response[str(code)]['content']['application/json']['schema']=={'$ref':'#/components/schemas/ErrorResponse'}
        if method=='post':
            assert response['202']['content']['application/json']['schema']=={'$ref':'#/components/schemas/StreamCreatedResponse'}
        else:
            assert set(response['200']['content'])=={'text/event-stream'}
            assert 'content' not in response['204']
    finally:
        await database.close()
