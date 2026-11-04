import asyncio
import pytest
from contracts import StreamRequest
from errors import ProviderCallError
from providers.base import ProviderStreamEvent
from test_f7_budget_and_repair import service,body


class ScriptStream:
    def __init__(self,*scripts):
        self.scripts=list(scripts); self.calls=[];self.closed=0
    async def stream(self,model,messages,**options):
        self.calls.append((model,messages,options))
        try:
            for event in self.scripts.pop(0):
                if isinstance(event,BaseException): raise event
                yield event
        finally:self.closed+=1


async def test_f7_stream_retries_before_content_not_after():
    p=ScriptStream([ProviderStreamEvent(status='failed',error_code='provider_connection_error')],
                   [ProviderStreamEvent(delta='A123'),ProviderStreamEvent(status='completed',upstream_terminal_received=True)])
    svc,traces,trace=service(p)
    execution=svc.prepare_stream(StreamRequest.model_validate(body()),trace)
    await execution.run_background()
    assert len(p.calls)==2 and p.closed==2
    assert [a.status for a in traces.attempts]==['failed','completed']
    assert trace.status=='completed'


@pytest.mark.parametrize('text',['text',' ','\n'])
async def test_f7_stream_any_nonempty_delta_permanently_blocks_recovery(text):
    p=ScriptStream([ProviderStreamEvent(delta=text),ProviderStreamEvent(status='failed',error_code='provider_connection_error')])
    svc,traces,trace=service(p)
    execution=svc.prepare_stream(StreamRequest.model_validate(body()),trace)
    await execution.run_background()
    assert len(p.calls)==1 and trace.status=='failed'


async def test_f7_stream_growth_before_content():
    p=ScriptStream([ProviderStreamEvent(status='failed',error_code='output_truncated')],
                   [ProviderStreamEvent(delta='complete'),ProviderStreamEvent(status='completed',upstream_terminal_received=True)])
    svc,traces,trace=service(p)
    execution=svc.prepare_stream(StreamRequest.model_validate(body(initial_output_tokens=32,max_output_tokens=128)),trace)
    await execution.run_background()
    assert [x[2]['max_output_tokens'] for x in p.calls]==[32,128]
    assert trace.status=='completed'
