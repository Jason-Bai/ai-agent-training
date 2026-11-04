"""The acceptance observer must reject deliberately inconsistent persisted results."""
from datetime import datetime,timezone
from uuid import uuid4

import pytest

from integration.test_f5o2_http import server,case
from f10_support import Environment


@pytest.mark.parametrize('damage',['none','wrong_fee','missing_bill','no_stream_terminal','foreign_owner'])
async def test_observer_distinguishes_bad_records(server,case,damage):
    name,config=case
    rid=uuid4()
    with server.connect(name) as sql:
        sql.execute("INSERT INTO tb_agent VALUES ('a','A'),('b','B')")
        pk=sql.execute("""INSERT INTO tb_llm_request
            (agent_id,request_id,operation,status,created_at,last_event_seq)
            VALUES ('a',%s,'streaming','completed',now(),1) RETURNING id""",(rid,)).fetchone()[0]
        sql.execute("""INSERT INTO tb_llm_attempt(request_pk,attempt_no,model,provider,adapter,api_protocol,
            configured_model,status,started_at,input_tokens,output_tokens,total_tokens)
            VALUES (%s,1,'general-primary','wire','deepseek','chat_completions','deepseek-flash','completed',now(),10,1,11)""",(pk,))
        if damage!='no_stream_terminal':
            sql.execute("""INSERT INTO tb_llm_stream_event VALUES (%s,1,'response.completed',%s,now())""",
                        (pk,'{"request_id":"'+str(rid)+'","usage":null}'))
        if damage!='missing_bill':
            sql.execute("""INSERT INTO tb_billing_attempt(request_pk,attempt_no,agent_id,state,reserved_amount,
                agent_charge,supplier_cost,quote,cached_input_tokens,billing_observed_at)
                VALUES (%s,1,%s,'settled',0.001,%s,%s,%s,0,now())""",
                (pk,'b' if damage=='foreign_owner' else 'a','.1' if damage=='wrong_fee' else '.000028',
                 '.1' if damage=='wrong_fee' else '.000028',
                 '{"hit":"0.4","miss":"2","output":"8"}'))
    env=Environment(server,name,config.parent)
    if damage=='none':
        assert (await env.verify(str(rid)))['status']=='completed'
    else:
        with pytest.raises(AssertionError):
            await env.verify(str(rid))
