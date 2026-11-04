"""Exercise each mixed capacity path once before measuring sustained load."""
import json

import pytest

from f13_capacity_support import f13_capacity_environment
from live.test_f10_capacity import KINDS, drained_sample

pytestmark=pytest.mark.live


async def test_mixed_real_paths_forwarding_accounting_and_cleanup(f13_capacity_environment,record_property):
    env=f13_capacity_environment;records=[]
    try:
        for model,mode in KINDS:
            record=await env.call(model,mode)
            record.update(model=model,mode=mode)
            records.append(record)
            assert record['status']=='completed',record
            record['verified']=await env.verify(record['request_id'],expected_agent='a')
        await env.assert_balances()
        await drained_sample(env)
    finally:
        rendered=json.dumps(records,ensure_ascii=False)
        (env.directory/'f13-mixed-path-probe.json').write_text(rendered)
        record_property('f13_mixed_path_probe',rendered)
