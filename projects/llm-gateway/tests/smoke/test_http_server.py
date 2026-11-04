import os
import socket
import subprocess
import sys
import tempfile
import time
import json
from uuid import UUID

import httpx
from legacy_identity import HEADERS
import pytest


def test_single_worker_real_http_server(runtime_dir):
    root = runtime_dir
    with socket.socket() as listener, tempfile.TemporaryFile(mode="w+") as log:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "--app-dir", str(root / "src"), "main:create_app", "--factory",
             "--workers", "1", "--fd", str(listener.fileno()), "--log-level", "warning"],
            cwd=root / "unrelated-cwd", env={"PATH": os.defpath},
            pass_fds=(listener.fileno(),), stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(headers=HEADERS, base_url=f"http://127.0.0.1:{port}", timeout=0.4, trust_env=False) as client:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        missing = client.get("/missing")
                        break
                    except httpx.TransportError:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            log.seek(0)
                            raise AssertionError(f"Server did not start: {log.read()}")
                        time.sleep(0.05)
                valid = client.post("/v1/llm", json={
                    "model": "general-primary",
                    "prompt": {"name": "basic_text", "version": "1.0.0"},
                    "messages": [{"role": "user", "content": "question"}],
                })
                invalid = client.post("/v1/llm", content="{", headers={"Content-Type": "application/json"})
                for response, status, code in [(valid, 200, "ok"), (invalid, 422, "invalid_request"), (missing, 404, "not_found")]:
                    assert response.status_code == status
                    assert set(response.json()) == {"success", "code", "data", "message"}
                    assert response.json()["code"] == code
                    UUID(response.headers["X-Request-ID"])
                assert valid.json()["data"]["text"] == "MOCK_REPLY"
                for version in ("1.0.0", "1.1.0"):
                    body = {
                        "model": "general-primary",
                        "prompt": {"name": "context_answer", "version": version, "variables": {"context": "订单 A123 是充电器。"}},
                        "messages": [{"role": "user", "content": "订单的商品是什么？"}],
                    }
                    response = client.post("/v1/llm", json=body)
                    assert response.status_code == 200
                    assert set(response.json()) == {"success", "code", "data", "message"}
                    assert response.json()["data"]["text"] == "MOCK_REPLY"
                    UUID(response.headers["X-Request-ID"])
                body["prompt"]["variables"] = {}
                missing_variable = client.post("/v1/llm", json=body)
                assert missing_variable.status_code == 400
                assert missing_variable.json()["code"] == "missing_prompt_variable"
                assert missing_variable.json()["data"] is None
                assert set(missing_variable.json()) == {"success", "code", "data", "message"}
                UUID(missing_variable.headers["X-Request-ID"])
                body["prompt"]["variables"] = {"context": 123}
                invalid_variable = client.post("/v1/llm", json=body)
                assert invalid_variable.status_code == 422
                assert invalid_variable.json()["code"] == "invalid_request"
                assert invalid_variable.json()["data"] is None
                assert set(invalid_variable.json()) == {"success", "code", "data", "message"}
                UUID(invalid_variable.headers["X-Request-ID"])
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.parametrize("name", ["ENV", "LLM_GATEWAY_PROVIDER"])
def test_invalid_configuration_exits_before_serving(runtime_dir, name):
    (runtime_dir / ".env").write_text(f"{name}=PRIVATE_CONFIG_MARKER\n")
    result = subprocess.run(
        [sys.executable, "-m", "uvicorn", "--app-dir", str(runtime_dir / "src"),
         "main:create_app", "--factory", "--host", "127.0.0.1", "--port", "0", "--workers", "1"],
        cwd=runtime_dir / "unrelated-cwd", env={"PATH": os.defpath},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert name in output
    assert "PRIVATE_CONFIG_MARKER" not in output
    assert "Application startup complete" not in output


def test_structured_single_worker_http_and_trace(runtime_dir):
    # Test-only bootstrap in the copied runtime; no product routes or switches.
    bootstrap = runtime_dir / 'src/f4_acceptance.py'
    bootstrap.write_text('''
from contextlib import asynccontextmanager
from fastapi.encoders import jsonable_encoder
from pathlib import Path
import json
from main import create_app as build_app
from providers.mock import MockProvider
from settings import Settings

def create_app():
    providers={'mock-main':MockProvider(text='{"order_id":"A123"}'),
               'mock-backup':MockProvider(text='broken JSON')}
    app=build_app(settings=Settings(env='test',_env_file=None),providers=providers)
    original=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(app):
        async with original(app):
            yield
            data={'requests':app.state.traces.requests,
                  'attempts':app.state.traces.attempts,
                  'calls':{k:len(v.calls) for k,v in providers.items()}}
            (Path(__file__).parents[1]/'trace.json').write_text(json.dumps(jsonable_encoder(data)))
    app.router.lifespan_context=lifespan
    return app
''')
    with socket.socket() as listener, tempfile.TemporaryFile(mode='w+') as log:
        listener.bind(('127.0.0.1',0));listener.listen();port=listener.getsockname()[1]
        process=subprocess.Popen([
            sys.executable,'-m','uvicorn','--app-dir',str(runtime_dir/'src'),
            'f4_acceptance:create_app','--factory','--workers','1','--fd',str(listener.fileno()),'--log-level','warning',
        ],cwd=runtime_dir/'unrelated-cwd',env={'PATH':os.defpath},pass_fds=(listener.fileno(),),stdout=log,stderr=subprocess.STDOUT)
        ids={}
        try:
            with httpx.Client(headers=HEADERS, base_url=f'http://127.0.0.1:{port}',timeout=2,trust_env=False) as client:
                deadline=time.monotonic()+10
                while True:
                    try:
                        assert client.get('/healthz').status_code==200
                        break
                    except httpx.TransportError:
                        if process.poll() is not None or time.monotonic()>deadline:
                            log.seek(0);pytest.fail(log.read())
                        time.sleep(.05)
                base={'model':'general-primary','prompt':{'name':'context_answer','version':'1.2.0','variables':{'context':'订单A123'}},
                      'messages':[{'role':'user','content':'提取订单号'}]}
                schema={'type':'object','properties':{'order_id':{'type':'string'}},'required':['order_id']}
                for label,extra,status,code in [
                    ('text',{},200,'ok'),('structured',{'response_schema':schema},200,'ok'),
                    ('rejected',{'response_schema':{}},400,'invalid_response_schema'),
                    ('failed',{'response_schema':schema,'model':'general-backup'},502,'invalid_json'),
                ]:
                    response=client.post('/v1/llm',json={**base,**extra})
                    assert (response.status_code,response.json()['code'])==(status,code)
                    assert set(response.json())=={'success','code','data','message'}
                    ids[label]=response.headers['X-Request-ID'];UUID(ids[label])
                    if label=='structured':assert response.json()['data']['parsed']=={'order_id':'A123'}
                    elif label=='text':assert set(response.json()['data'])=={'text','usage'}
                    else:assert response.json()['data'] is None
        finally:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        trace=json.loads((runtime_dir/'trace.json').read_text())
        assert trace['calls']=={'mock-main':2,'mock-backup':1}
        for label,request_id in ids.items():
            request=trace['requests'][request_id]
            attempts=[a for a in trace['attempts'] if a['request_id']==request_id]
            count=0 if label=='rejected' else 1
            assert request['attempts']==len(attempts)==count
            assert request['status']==('completed' if label in {'text','structured'} else 'failed')
            if attempts:assert attempts[0]['status']==request['status']
