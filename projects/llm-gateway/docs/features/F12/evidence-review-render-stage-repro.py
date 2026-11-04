import asyncio,json
from f12_cases import registry,request,service
async def main():
 for disabled in (True,False):
  changes={name:{'enabled':not disabled} for name in ('chat-a','responses-b')}
  svc,trace,a,b,traces=service(registry(changes))
  req=request(prompt={'name':'context_answer','version':'1.1.0','variables':{'context':'C'*10000}},messages=[{'role':'user','content':'M'*10000}])
  try:
   await svc.complete(req,trace);result={'returned':True}
  except Exception as e:result={'exception':type(e).__name__,'code':getattr(e,'code',None)}
  result.update(all_disabled=disabled,primary_calls=len(a.calls),backup_calls=len(b.calls),attempts=len(traces.attempts),routing=trace.routing,error_stage=trace.error_stage)
  print(json.dumps(result,ensure_ascii=False))
asyncio.run(main())
