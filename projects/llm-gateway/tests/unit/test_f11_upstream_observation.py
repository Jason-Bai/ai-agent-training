"""The observer must retain exact HTTP bytes and preserve Provider behavior."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import httpx2
import pytest

from contracts import Message
from f4_cases import registry
from providers.deepseek import DeepSeekProvider
from upstream_observation import install


@pytest.mark.parametrize('stalled', [False, True])
async def test_raw_observation_preserves_text_and_observes_actual_read_exit(tmp_path, monkeypatch, stalled):
    delta = {'object':'chat.completion.chunk','model':'local',
             'choices':[{'index':0,'delta':{'content':'订单'},'finish_reason':None}]}
    terminal = {'object':'chat.completion.chunk','model':'local',
                'choices':[{'index':0,'delta':{},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}
    prefix = ('data: '+json.dumps(delta,ensure_ascii=False)+'\n\n').encode()
    suffix = ('data: '+json.dumps(terminal)+'\n\ndata: [DONE]\n\n').encode()

    class Bytes(httpx2.AsyncByteStream):
        async def __aiter__(self):
            # Split a UTF-8 character across transport chunks.
            pos = prefix.index('订单'.encode())+1
            yield prefix[:pos]
            yield prefix[pos:]
            if stalled:
                await asyncio.Event().wait()
            else:
                yield suffix

    provider = DeepSeekProvider('http://localhost','fake-test-key')
    await provider.client.aclose()
    provider.client = httpx2.AsyncClient(transport=httpx2.MockTransport(
        lambda request:httpx2.Response(200,stream=Bytes(),headers={'content-type':'text/event-stream'})))

    async def begin(*args, **kwargs): pass
    @asynccontextmanager
    async def lifespan(app): yield
    app = SimpleNamespace(state=SimpleNamespace(storage=SimpleNamespace(begin_attempt=begin),
                                                providers={'p':provider}),
                          router=SimpleNamespace(lifespan_context=lifespan))
    install(app,tmp_path,monkeypatch)
    items=[]
    try:
        async with app.router.lifespan_context(app):
            await app.state.storage.begin_attempt(SimpleNamespace(request_id='case'),
                                                  SimpleNamespace(number=1,api_protocol='chat_completions'))
            try:
                async with asyncio.timeout(.05 if stalled else 2):
                    async for item in provider.stream(registry().get_endpoint('general-primary'),
                            [Message(role='user',content='订单')],max_output_tokens=16,timeout_seconds=2):
                        items.append(item)
            except TimeoutError:
                assert stalled
        path=tmp_path/'availability-case.json'
        assert path.exists(), 'real HTTP read evidence was not recorded'
        saved=json.loads(path.read_text())
        observed,=saved['attempts']
        assert observed['http_status']==200 and observed['read_closed'] is True
        assert observed['read_cancelled'] is stalled
        assert observed['raw_text']==(prefix if stalled else prefix+suffix).decode()
        assert observed['typed_text']==''.join(x.delta for x in items)=='订单'
        assert observed['transport_chunks']==(2 if stalled else 3)
        assert (items[-1].status=='completed') is not stalled
        assert not any(word in path.read_text() for word in ('fake-test-key','Authorization','Bearer'))
    finally:
        await provider.aclose()
