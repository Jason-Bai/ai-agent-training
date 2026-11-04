"""R1 counterexamples: public errors, persisted diagnostics and zero side effects."""
from fastapi.testclient import TestClient
import pytest

from f12_cases import registry, request
from legacy_identity import HEADERS
from providers.mock import MockProvider
from test_f12_stream_plan import f12_pg, f12_case, app, rows, trace


@pytest.mark.parametrize('streaming',[False,True])
@pytest.mark.parametrize('disabled',[False,True])
def test_render_overflow_obeys_route_stage_and_persists_rejection(f12_case,f12_pg,streaming,disabled):
    name,config,_=f12_case
    definition=registry({n:{'enabled':not disabled} for n in ('chat-a','responses-b')})
    a,b=MockProvider(),MockProvider()
    application=app(config,definition,a,b)
    with TestClient(application,headers=HEADERS) as client:
        body=request(streaming=streaming,
            prompt={'name':'context_answer','version':'1.1.0','variables':{'context':'C'*10000}},
            messages=[{'role':'user','content':'M'*10000}]).model_dump(exclude_none=True)
        response=client.post('/v1/llm/stream' if streaming else '/v1/llm',json=body)
        code='no_available_endpoint' if disabled else 'prompt_too_long'
        assert response.status_code==(503 if disabled else 400)
        assert set(response.json())=={'success','code','data','message'}
        assert response.json()['success'] is False and response.json()['code']==code
        assert response.json()['data'] is None
        rid=response.headers['x-request-id'];diagnostic=trace(client,rid)
        assert diagnostic['status']=='failed' and diagnostic['error_stage']=='routing'
        route=diagnostic['routing']
        assert route['selected_endpoint'] is None and route['reason']=='no_eligible_endpoint'
        assert route['candidates']==[
            {'endpoint_id':n,'status':'skipped','code':'endpoint_disabled' if disabled else 'prompt_too_long'}
            for n in ('chat-a','responses-b')]
        row,attempts,events,bills=rows(f12_pg,name,rid)
        assert row[3] is None and not attempts and not events and not bills
        assert not a.calls and not b.calls and not a.stream_calls and not b.stream_calls
        assert application.state.concurrency_limiter.snapshot()['active_requests']==0


def test_probe_timeout_rejection_has_no_attempt_or_fee_reservation(f12_case,f12_pg):
    name,config,_=f12_case
    a,b=MockProvider(),MockProvider()
    application=app(config,registry({'chat-a':{'max_timeout_seconds':9}}),a,b)
    with TestClient(application,headers=HEADERS) as client:
        response=client.post('/v1/models/general-primary/health')
        assert response.status_code==400
        assert response.json()['code']=='invalid_timeout' and response.json()['data'] is None
        rid=response.headers['x-request-id'];diagnostic=trace(client,rid)
        assert diagnostic['status']=='failed' and diagnostic['error_code']=='invalid_timeout'
        assert diagnostic['attempts']==[]
        row,attempts,events,bills=rows(f12_pg,name,rid)
        assert row[3] is None and not attempts and not events and not bills
        assert not a.calls and not b.calls
        assert application.state.concurrency_limiter.snapshot()['active_requests']==0
