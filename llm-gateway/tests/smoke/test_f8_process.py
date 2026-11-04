"""A diagnosed request survives a Gateway process replacement and a database outage."""
from test_f5o2_process import process_pg, process_case, create, gateway, observations, wait_for


def test_f8_query_outage_and_process_replacement(process_case):
    pg, _, directory = process_case
    with gateway(directory) as (_, client):
        rid, stream_url = create(client, 'f8-restart')
        trace_url = f'/v1/traces/{rid}'
        assert client.get(trace_url).json()['data']['status'] == 'pending'
        (directory / 'release-f8-restart').touch()
        assert 'response.completed' in client.get(stream_url).text
        def with_delivery_observations():
            data = client.get(trace_url).json()['data']
            if all(data['timing'][key] is not None for key in ('ttft_ms', 'stream_duration_ms')):
                return data
        saved = wait_for(with_delivery_observations)
        assert saved['status'] == 'completed' and len(saved['attempts']) == 1
        assert pg.compose('stop', 'postgres').returncode == 0
        try:
            failure = client.get(trace_url)
            assert failure.status_code == 503 and failure.json()['code'] == 'storage_unavailable'
        finally:
            pg.start()
    with gateway(directory) as (_, client):
        trace = client.get(trace_url)
        assert trace.status_code == 200
        assert trace.json()['data'] == saved
    assert [item for item in observations(directory) if item[0] == 'stream'] == [['stream', 'f8-restart']]
