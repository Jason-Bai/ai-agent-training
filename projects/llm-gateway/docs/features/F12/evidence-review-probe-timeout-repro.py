import asyncio, json
from f12_cases import registry, service
async def main():
 svc,trace,a,b,traces=service(registry({'chat-a':{'max_timeout_seconds':1}}))
 trace.operation='provider_probe'
 try:
  result=await svc.probe('general-primary',trace)
  outcome={'returned':result.model_dump()}
 except Exception as e:
  outcome={'exception':type(e).__name__,'code':getattr(e,'code',None)}
 outcome.update(primary_calls=len(a.calls),backup_calls=len(b.calls),primary_timeouts=[c[2]['timeout_seconds'] for c in a.calls],attempts=[{'model':t.model,'status':t.status} for t in traces.attempts],routing=trace.routing,limiter=svc.limiter.snapshot())
 print(json.dumps(outcome,ensure_ascii=False,default=str))
asyncio.run(main())
