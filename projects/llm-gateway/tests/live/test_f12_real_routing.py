"""F12 L02/L05: controlled routing decisions followed by genuine Provider calls.

Never claims that the local injected 503 originated from the supplier.
"""
import json

import pytest
import yaml

from f10_support import f10_environment, payload
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

pytestmark=pytest.mark.live


@contextmanager
def upstream(status,headers):
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def do_POST(self):
            calls.append((self.path,json.loads(self.rfile.read(int(self.headers['content-length'])))))
            body=b'{"error":{"type":"server_error","message":"controlled local failure"}}'
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    thread=Thread(target=lambda:server.serve_forever(poll_interval=.01),daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}',calls
    finally:server.shutdown();server.server_close();thread.join(3)


def configure(env,selected,*,controlled_url=None):
    path=env.config/'gateway.yaml';data=yaml.safe_load(path.read_text())
    by_protocol={data['providers'][v['provider']]['api_protocol']:name for name,v in data['endpoints'].items()}
    old_chat=by_protocol['chat_completions'];old_response=by_protocol['responses']
    rename={old_chat:'chat-a',old_response:'responses-b'}
    data['endpoints']={rename[name]:value for name,value in data['endpoints'].items()}
    for group in data['model_groups'].values():
        group['candidates']=[rename[name] for name in group['candidates']]
    other='responses-b' if selected=='chat-a' else 'chat-a'
    data['model_groups']['general-primary']['candidates']=[other,selected]
    data['endpoints'][other]['enabled']=controlled_url is not None
    if controlled_url is not None:
        controlled=data['endpoints'][other];provider=controlled['provider']
        # Only a fake key is sent to the local failure server; the actual
        # supplier key stays in an owned private credential file for the target.
        credentials=yaml.safe_load((env.config/'credentials.yaml').read_text())
        credentials['credentials']['f12-controlled']={'api_key':'fake-local-f12-key'}
        key_path=env.config/'credentials.yaml';key_path.unlink()
        key_path.write_text(yaml.safe_dump(credentials));key_path.chmod(0o600)
        data['providers'][provider]['base_url']=controlled_url
        data['providers'][provider]['credential_ref']='f12-controlled'
        prices=data['pricing']
        prices['bindings'][provider]='f12-controlled-supplier'
        rate={'hit':'1','miss':'1','output':'1'}
        prices['versions'].append(dict(supplier='f12-controlled-supplier',model=controlled['model_id'],
            version='test-local',effective_from='2020-01-01T00:00:00Z',peak=rate,off_peak=rate))
        data['recovery'].update(max_attempts=2,max_attempts_per_endpoint=1,
                                base_delay_seconds=0,max_delay_seconds=0)
    path.write_text(yaml.safe_dump(data))


async def assert_actual_result(env,selected,mode,*,controlled=False,record_property):
    response=await env.client.post('/v1/llm/stream' if mode=='streaming' else '/v1/llm',
                                  json=payload('general-primary',mode))
    rid=response.headers['x-request-id']
    events=[]
    if mode=='streaming':
        assert response.status_code==202,response.text
        response=await env.client.get(f'/v1/llm/stream/{rid}/events')
        for frame in response.text.split('\n\n'):
            fields=dict(line.split(': ',1) for line in frame.splitlines() if ': ' in line)
            if fields.get('event') and fields['event']!='response.heartbeat':
                events.append((fields['event'],json.loads(fields['data'])))
    diagnostic=(await env.client.get('/v1/traces/'+rid)).json()['data']
    record_property('f12_request_id',rid)
    record_property('f12_evidence_kind','controlled_http_failure_then_real' if controlled else 'disabled_filter_then_real')
    record_property('f12_trace',json.dumps(diagnostic,ensure_ascii=False))
    assert response.status_code==200,response.text
    if mode=='streaming':
        assert events and events[-1][0]=='response.completed',events
        text=''.join(data['delta'] for event,data in events if event=='response.delta')
    else:
        text=response.json()['data']['text']
    assert 'A123' in text and '充电器' in text
    expected_model='general-primary' if selected=='chat-a' else 'general-backup'
    upstream_text,_=await env.upstream_answer(rid,expected_model,mode)
    assert text==upstream_text,'Gateway changed the real Provider response'
    assert diagnostic['requested_model']=='general-primary'
    assert diagnostic['attempts'][-1]['model']==selected
    assert diagnostic['attempts'][-1]['api_protocol']==('chat_completions' if selected=='chat-a' else 'responses')
    if controlled:
        assert len(diagnostic['attempts'])==2
        assert diagnostic['attempts'][0]['error_code']=='provider_unavailable'
        assert diagnostic['attempts'][1]['reason']=='fallback'
        assert diagnostic['routing']['selected_endpoint']!=selected
    else:
        assert 1<=len(diagnostic['attempts'])<=2
        assert all(a['model']==selected for a in diagnostic['attempts'])
        assert diagnostic['routing']['selected_endpoint']==selected
        assert diagnostic['routing']['candidates'][0]['code']=='endpoint_disabled'
    verified=await env.verify(rid)
    assert verified['status']=='completed'
    await env.assert_balances()
    record_property('f12_sql_billing',json.dumps(verified,ensure_ascii=False))


@pytest.mark.parametrize('selected',['chat-a','responses-b'])
@pytest.mark.parametrize('mode',['text','streaming'])
async def test_filtered_choice_reaches_actual_provider(f10_environment,selected,mode,record_property):
    env=f10_environment
    await env.stop();configure(env,selected);await env.start()
    await assert_actual_result(env,selected,mode,record_property=record_property)


@pytest.mark.parametrize('selected',['chat-a','responses-b'])
@pytest.mark.parametrize('mode',['text','streaming'])
async def test_controlled_failure_then_actual_backup(f10_environment,selected,mode,record_property):
    env=f10_environment
    with upstream(503,[]) as (url,calls):
        await env.stop();configure(env,selected,controlled_url=url);await env.start()
        await assert_actual_result(env,selected,mode,controlled=True,record_property=record_property)
        assert len(calls)==1
        record_property('f12_controlled_http_call_count',len(calls))
