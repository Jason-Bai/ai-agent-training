"""C03: shared upstream and reordered phases do not mix Agent state."""
import asyncio
from dataclasses import replace
import json

import pytest

from errors import GatewayError
from f12_cases import request
from f13_cases import registry, service
from providers.base import ProviderResult
from services.classification import CLASSIFIER_SYSTEM
from services.models import ConcurrencyPolicy


async def test_two_agents_keep_one_permit_each_across_reordered_phases():
    definitions=replace(registry(),concurrency=ConcurrencyPolicy(max_requests=2,max_requests_per_agent=1))
    gateway,first,provider,traces=service(definitions);first.agent_id='agent-A'
    second=traces.start_request('request-B');second.agent_id='agent-B'
    entered={name:asyncio.Event() for name in ('req-A','req-B')}
    release={name:asyncio.Event() for name in entered}
    generating={name:asyncio.Event() for name in entered}
    finish={name:asyncio.Event() for name in entered}
    calls=[]

    async def complete(model,messages,**options):
        classifier=messages[0].content==CLASSIFIER_SYSTEM
        text=json.loads(messages[-1].content)['task_material']['user_messages'][0]['content'] if classifier else messages[-1].content
        name='req-A' if 'req-A' in text else 'req-B'
        calls.append((name,'classification' if classifier else 'generation'))
        if classifier:
            entered[name].set();await release[name].wait()
            return ProviderResult(text='{"group":"'+('light' if name=='req-A' else 'analysis')+'"}')
        generating[name].set();await finish[name].wait()
        return ProviderResult(text=name)
    provider.complete=complete
    tasks=[asyncio.create_task(gateway.complete(request(model='auto-general',
        messages=[{'role':'user','content':name}]),trace)) for name,trace in [('req-A',first),('req-B',second)]]
    try:
        async with asyncio.timeout(3):
            await asyncio.gather(*(event.wait() for event in entered.values()))
            assert gateway.limiter.snapshot()=={'active_requests':2,'agents':{'agent-A':1,'agent-B':1}}
            rejected=traces.start_request('request-rejected');rejected.agent_id='agent-A'
            with pytest.raises(GatewayError) as caught:await gateway.complete(request(model='auto-general'),rejected)
            assert caught.value.code=='concurrency_limited' and len(calls)==2
            release['req-B'].set();await generating['req-B'].wait()
            assert gateway.limiter.snapshot()['active_requests']==2
            release['req-A'].set();await generating['req-A'].wait()
            assert gateway.limiter.snapshot()['active_requests']==2
            finish['req-B'].set();finish['req-A'].set()
            assert [result.text for result in await asyncio.gather(*tasks)]==['req-A','req-B']
    finally:
        for event in [*release.values(),*finish.values()]:event.set()
        for task in tasks:
            if not task.done():task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
    assert first.auto_routing['resolved_group']=='light' and second.auto_routing['resolved_group']=='analysis'
    for trace in (first,second):
        own=[attempt for attempt in traces.attempts if attempt.request_id==trace.request_id]
        assert [(a.number,a.purpose) for a in own]==[(1,'classification'),(2,'generation')]
    assert gateway.limiter.snapshot()=={'active_requests':0,'agents':{}}
