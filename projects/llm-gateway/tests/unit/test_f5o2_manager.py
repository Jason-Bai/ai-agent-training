"""Background ownership invariants, independently of ASGI subscribers."""
import asyncio
import importlib.util
from uuid import uuid4


def test_background_manager_component_exists():
    assert importlib.util.find_spec('services.streaming_manager') is not None, 'F5-O2 needs application-owned generation'


async def test_background_execution_does_not_claim_fake_delivery():
    from contracts import Message
    from f4_cases import registry
    from providers.mock import MockProvider
    from services.streaming import PreparedStream, StreamingExecution
    from services.trace import TraceStore
    models=registry();model=models.endpoints['general-primary'];traces=TraceStore();trace=traces.start_request(str(uuid4()),operation='streaming')
    execution=StreamingExecution(PreparedStream('general-primary',model,models.providers[model.provider],
        [Message(role='user',content='A123')],16,30),MockProvider(),trace,traces)
    assert hasattr(execution,'run_background'), 'Producer needs a persistence-only entry with no fake ASGI send'
    recorded=[]
    class Storage:
        async def begin_attempt(self,*args,**kwargs):
            recorded.append('attempt')
        async def append_event(self,trace,name,payload,**kwargs):
            recorded.append((name,payload))
        async def finish(self,trace,attempt=None,**kwargs):
            recorded.append(kwargs['event'])
            return 'completed'
    execution.storage=Storage()
    execution.defer=lambda *args: None
    execution.prepared_storage=True
    execution.deadline=asyncio.get_running_loop().time()+30
    trace.stream_started_at=execution.deadline-30
    await execution.run_background()
    assert recorded[0]=='attempt' and recorded[-1][0]=='response.completed'
    assert trace.status=='completed' and trace.first_text_sent_at is None
    assert trace.ttft_ms is None and trace.generation_duration_ms is not None
    assert execution.provider.stream_close_calls==1

