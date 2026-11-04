from pathlib import Path
from decimal import Decimal,localcontext
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo
import gzip,json,yaml,hashlib
root=Path.cwd(); d=root/'docs/features/F10'; wire=Path('/var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/pytest-of-baiyu/pytest-401/test_real_capacity_and_ten_min0')
report=json.loads(gzip.decompress((d/'evidence-final-capacity.json.gz').read_bytes()))
prices=yaml.safe_load((root/'config/prices.yaml').read_text());rule=prices['versions'][0]
# This run has exactly one version and one off-peak window (12:00-14:00 CST).
assert len(prices['versions'])==1 and rule['model']=='deepseek-flash'
windows=[]
for phase in report['phases']+[report['sustained']]:
 start=datetime.fromisoformat(phase['started_at']).astimezone(ZoneInfo('Asia/Shanghai'))
 end=datetime.fromisoformat(phase['completed_at']).astimezone(ZoneInfo('Asia/Shanghai'))
 assert start.date()==end.date() and (start.hour>=12 and end.hour<14)
 assert start>=datetime.fromisoformat(rule['effective_from'])
 windows.append({'start':start.isoformat(),'end':end.isoformat()})
rates={k:Decimal(v) for k,v in rule['off_peak'].items()}
rows=[]; totals={a:dict(input_tokens=0,output_tokens=0,total_tokens=0,cached_input_tokens=0,fee=Decimal(0),known=0,unknown=0) for a in ['a','b']}
stream_checked=0
with localcontext() as context:
 context.prec=80
 for phase in report['phases']+[report['sustained']]:
  for record in phase['records']:
   rid=record['request_id']; owner=record['agent']; usage=None; file=wire/('response-final-'+rid+'.json')
   if file.exists(): usage=json.loads(file.read_text())['raw'].get('usage')
   else:
    file=wire/('wire-'+rid+'.json')
    if file.exists():
     candidates=[e['data'].get('usage') for e in json.loads(file.read_text()) if isinstance(e['data'],dict) and e['data'].get('usage')]
     if candidates:usage=candidates[-1]
   row=dict(request_id=rid,agent=owner,model=record['model'],mode=record['mode'],status=record['status'])
   if usage is None:
    assert record['status']=='failed';totals[owner]['unknown']+=1;row.update(usage=None,fee=None)
   else:
    inp=usage.get('input_tokens',usage.get('prompt_tokens'));output=usage.get('output_tokens',usage.get('completion_tokens'))
    assert type(inp) is int and type(output) is int and usage['total_tokens']==inp+output
    cache_evidence=[]
    detail=usage.get('input_tokens_details',usage.get('prompt_tokens_details',{}))
    if 'cached_tokens' in detail: cache_evidence.append(detail['cached_tokens'])
    if 'prompt_cache_hit_tokens' in usage:cache_evidence.append(usage['prompt_cache_hit_tokens'])
    if 'prompt_cache_miss_tokens' in usage:cache_evidence.append(inp-usage['prompt_cache_miss_tokens'])
    assert cache_evidence and all(type(v) is int and v==cache_evidence[0] and 0<=v<=inp for v in cache_evidence)
    cache=cache_evidence[0]
    fee=(cache*rates['hit']+(inp-cache)*rates['miss']+output*rates['output'])/Decimal(1000000)
    normalized=dict(input_tokens=inp,output_tokens=output,total_tokens=inp+output)
    if record['mode']=='streaming':
     assert record['events'][-1][2]['usage']==normalized
     stream_checked+=1
    row.update(usage=normalized,cached_input_tokens=cache,cache_evidence=cache_evidence,fee=str(fee),source_sha256=hashlib.sha256(file.read_bytes()).hexdigest())
    for key,value in normalized.items():totals[owner][key]+=value
    totals[owner]['cached_input_tokens']+=cache;totals[owner]['fee']+=fee;totals[owner]['known']+=1
   rows.append(row)
 for owner,expected in totals.items():
  actual=report['sustained']['accounts'][owner]; real=actual['usage']['real']
  assert Decimal(actual['charged'])==expected['fee']
  assert real['known_subtotal']=={k:expected[k] for k in ('input_tokens','output_tokens','total_tokens')}
  assert real['unknown_attempts']==expected['unknown']
  expected['fee']=str(expected['fee'])
result=dict(source='raw upstream per request; no production quote/price/cache functions or SQL quote reused',price_config_sha256=hashlib.sha256((root/'config/prices.yaml').read_bytes()).hexdigest(),version=rule['version'],period='off_peak',rates={k:str(v) for k,v in rates.items()},independent_time_windows=windows,accounts=totals,records=len(rows),streaming_client_usage_matches=stream_checked,limitations=['SQL database was cleaned after fixture; per-request SQL Usage/cache/price fields not archived; per-request original wire vs SQL cannot be retrospectively verified','Original supplier invoice unavailable'],per_request_file='evidence-raw-per-request.json.gz')
(d/'evidence-raw-per-request.json.gz').write_bytes(gzip.compress(json.dumps(rows,ensure_ascii=False).encode(),mtime=0))
(d/'evidence-raw-account-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(result,ensure_ascii=False,indent=2))
