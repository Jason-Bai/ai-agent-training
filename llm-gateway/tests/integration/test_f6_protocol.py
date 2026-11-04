from registry_helpers import ModelRegistry
"""Cancel real adapters through the Gateway against a loopback SSE server."""
from services.models import ConcurrencyPolicy
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import select
import socket
from threading import Event, Thread
from types import SimpleNamespace

import httpx
import pytest

from f4_cases import registry
from main import create_app
from providers.deepseek import DeepSeekProvider
from providers.openai_compatible import OpenAICompatibleProvider
from services.models import ProviderConfig
from settings import Settings
from test_f5o2_http import server, case, body, frames, observe


def frame(data):
    return ('data: '+(data if isinstance(data,str) else json.dumps(data))+'\n\n').encode()


def chat(delta='',finish=None):
    return {'object':'chat.completion.chunk','model':'local-model','choices':[{'index':0,'delta':{'content':delta},'finish_reason':finish}]}


@contextmanager
def upstream(protocol):
    state=SimpleNamespace(calls=[],closed=Event(),release=Event(),started=Event())
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args): pass
        def handle(self):
            try: super().handle()
            except (ConnectionResetError,BrokenPipeError): pass
        def do_POST(self):
            content=json.loads(self.rfile.read(int(self.headers['content-length'])))
            messages=content.get('messages',content.get('input'))
            label=messages[-1]['content']
            state.calls.append((self.path,content,label))
            first=frame(chat(label)) if protocol=='chat_completions' else frame({'type':'response.output_text.delta','delta':label})
            final=(frame(chat(finish='stop'))+frame('[DONE]') if protocol=='chat_completions' else frame({
                'type':'response.completed','response':{'object':'response','model':'local-model',
                'status':'completed','error':None,'incomplete_details':None,
                'output':[{'type':'message','role':'assistant','status':'completed',
                           'content':[{'type':'output_text','text':label}]}], 'usage':None}}))
            self.send_response(200);self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(first)+len(final)));self.end_headers()
            try:
                self.wfile.write(first);self.wfile.flush()
                if label=='cancel-A':
                    state.started.set()
                    while not state.release.wait(.01):
                        ready,_,_=select.select([self.connection],[],[],0)
                        if ready:
                            try: closed=self.connection.recv(1,socket.MSG_PEEK)==b''
                            except (ConnectionResetError,OSError): closed=True
                            if closed: state.closed.set();return
                self.wfile.write(final);self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError):
                if label=='cancel-A': state.closed.set()
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler);http.daemon_threads=True
    thread=Thread(target=lambda:http.serve_forever(poll_interval=.01),daemon=True);thread.start()
    state.url=f'http://127.0.0.1:{http.server_port}'
    try: yield state
    finally:
        state.release.set();http.shutdown();http.server_close();thread.join(3)


async def until(predicate):
    async with asyncio.timeout(8):
        while not (value:=predicate()): await asyncio.sleep(.01)
        return value


@pytest.mark.parametrize('kind,adapter,protocol',[
    (DeepSeekProvider,'deepseek','chat_completions'),
    (OpenAICompatibleProvider,'openai_compatible','responses'),
])
async def test_real_protocol_cancel_closes_own_stream_preserves_peer(server,case,kind,adapter,protocol):
    name,config=case
    with upstream(protocol) as wire:
        provider=kind(wire.url,'FAKE_LOCAL_KEY')
        base=registry({name: {'structured_output_mode': 'none'} for name in ('general-primary','general-backup')})
        definitions={key:ProviderConfig(adapter=adapter,api_protocol=protocol,
            base_url=wire.url,credential_ref='fake-local') for key in base.providers}
        models=ModelRegistry(base.endpoints,definitions, model_groups=base.model_groups,concurrency=ConcurrencyPolicy(max_requests=64, max_requests_per_agent=64))
        app=create_app(settings=Settings(env='test',_env_file=None),model_registry=models,
                       config_dir=config,provider=provider)
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                    created=await client.post('/v1/llm/stream',json=body(messages=[{'role':'user','content':'cancel-A'}]))
                    assert created.status_code==202,created.text
                    rid=created.json()['data']['request_id']
                    subscription=asyncio.create_task(client.get(f'/v1/llm/stream/{rid}/events'))
                    try:
                        await until(lambda:len(observe(server,name,rid)[2])==1)
                        assert observe(server,name,rid)[2][0]['event_type']=='response.delta'
                        response=await client.post(f'/v1/llm/stream/{rid}/cancel')
                        assert response.status_code in (200,202),response.text
                        async with asyncio.timeout(8):
                            while response.json()['data']['status']=='canceling':
                                await asyncio.sleep(.02)
                                response=await client.post(f'/v1/llm/stream/{rid}/cancel')
                        assert response.json()['data']['status']=='canceled', observe(server,name,rid)
                        await until(wire.closed.is_set)
                        received=frames((await subscription).text)
                        assert [item[1] for item in received]==['response.delta','response.canceling','response.canceled']
                        row,attempts,saved=observe(server,name,rid)
                        assert row['status']==attempts[0]['status']=='canceled'
                        assert len(attempts)==1 and len(saved)==3
                        peer=await client.post('/v1/llm/stream',json=body(messages=[{'role':'user','content':'peer-B'}]))
                        peer_id=peer.json()['data']['request_id']
                        completed=await client.get(f'/v1/llm/stream/{peer_id}/events')
                        assert frames(completed.text)[-1][1]=='response.completed'
                        assert observe(server,name,peer_id)[0]['status']=='completed'
                        assert len(wire.calls)==2
                        for path,sent,_ in wire.calls:
                            assert path==('/chat/completions' if protocol=='chat_completions' else '/responses')
                            assert sent['stream'] is True
                            if protocol=='responses': assert sent['store'] is False
                    finally:
                        if not subscription.done(): subscription.cancel()
                        await asyncio.gather(subscription,return_exceptions=True)
        finally: await provider.aclose()


def test_openai_compatible_chat_configuration_rejected_before_client_construction(monkeypatch):
    from pydantic import ValidationError
    from providers import factory
    calls=[]
    monkeypatch.setattr(factory, 'create_provider', lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValidationError, match='适配器与 API 协议不兼容'):
        ProviderConfig(adapter='openai_compatible', api_protocol='chat_completions',
                       base_url='http://127.0.0.1:1', credential_ref='fake-local')
    assert calls == []
