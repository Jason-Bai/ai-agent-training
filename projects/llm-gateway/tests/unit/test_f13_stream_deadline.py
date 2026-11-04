"""C01: the global timer owns classification storage waits as well as calls."""
import asyncio

from f12_cases import request
from f13_cases import service
from persistence.database import StorageUnavailable
from services.streaming_manager import StreamingManager
from utils.json_codec import dumps


class DeadlineStorage:
    async def begin_attempt(self, trace, attempt, *, deadline):
        # Match Database.transaction: its own expired timer is normalized to
        # StorageUnavailable. The outer request timer must take precedence.
        try:
            async with asyncio.timeout_at(deadline):
                await asyncio.Event().wait()
        except TimeoutError:
            raise StorageUnavailable() from None


async def test_global_expiration_while_registering_classifier_is_request_timeout():
    gateway,trace,provider,traces=service()
    pending=gateway.prepare_stream(request(model='auto-general',streaming=True),trace)
    row={'input_snapshot':dumps(pending.snapshot),'requested_model':'auto-general'}
    deferred=[];active={trace.request_id}
    manager=StreamingManager(DeadlineStorage(),traces,gateway.model_registry,gateway.token_budget,
        gateway.providers,lambda t,a=None:deferred.append(t),active,
        awaiting_persistence=lambda _:False,limiter=gateway.limiter,
        prompt_registry=gateway.prompt_registry)
    manager.permits[trace.request_id]=gateway.limiter.acquire(trace.agent_id,trace.request_id)
    task=asyncio.create_task(manager._generate(row,trace,asyncio.get_running_loop().time()+.02))
    await asyncio.wait_for(task,.5)
    async with asyncio.timeout(.5):
        while gateway.limiter.snapshot()['active_requests']:
            await asyncio.sleep(0)
    assert trace.error_code=='request_timeout'
    assert deferred==[trace] and not provider.calls and not active
    assert gateway.limiter.snapshot()=={'active_requests':0,'agents':{}}
