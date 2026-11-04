import signal

from test_f5o2_process import create, events, gateway, observations, process_case, process_pg


def test_f8_entry_evidence_during_dispatch_and_after_process_loss(process_case):
    server, name, directory = process_case
    def inspect(client, rid, status):
        result = client.get(f'/v1/traces/{rid}')
        assert result.status_code == 200
        data = result.json()['data']
        assert data['status'] == status
        assert [o for o in observations(directory) if o[0] == 'stream'] == [['stream', 'entry-proof']]
        assert len(data['attempts']) == 1
        assert data['attempts'][0]['provider_entered'] is None

    with gateway(directory) as (process, client):
        rid, url = create(client, 'entry-proof')
        with client.stream('GET', url) as subscription:
            assert next(events(subscription))[1] == 'response.delta'
            inspect(client, rid, 'running')
            process.kill()
            assert process.wait(timeout=5) == -signal.SIGKILL
    with gateway(directory) as (_, client):
        inspect(client, rid, 'failed')
