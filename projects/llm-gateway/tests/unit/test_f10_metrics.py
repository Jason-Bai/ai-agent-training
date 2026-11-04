import pytest


def test_percentiles_use_nearest_rank_and_keep_null_out_of_samples():
    import f10_metrics
    assert callable(getattr(f10_metrics, "distribution", None)), "missing capacity distribution contract"
    distribution = f10_metrics.distribution
    assert distribution([]) == {'samples': 0, 'p50': None, 'p95': None, 'p99': None, 'maximum': None}
    values = list(range(100, 0, -1))
    assert distribution(values) == {'samples': 100, 'p50': 50, 'p95': 95, 'p99': 99, 'maximum': 100}
    assert distribution([None, 5])['samples'] == 1
    for invalid in ([True], [-1], [float('nan')]):
        with pytest.raises(ValueError):
            distribution(invalid)


def test_summary_has_separate_window_and_drained_rates_and_groups():
    import f10_metrics
    assert callable(getattr(f10_metrics, "summarize", None)), "missing capacity summarize contract"
    summarize = f10_metrics.summarize
    records = [
        {'request_id': 'a', 'model': 'general-primary', 'mode': 'streaming', 'status': 'completed',
         'error_code': None, 'elapsed_ms': 100, 'first_text_ms': 10, 'create_ms': 2},
        {'request_id': 'b', 'model': 'general-backup', 'mode': 'text', 'status': 'failed',
         'error_code': 'provider_timeout', 'elapsed_ms': 200, 'first_text_ms': None, 'create_ms': None},
    ]
    report = summarize(records, dispatch_seconds=10, elapsed_seconds=20)
    assert report['started'] == 2 and report['successes'] == 1 and report['failures'] == {'provider_timeout': 1}
    assert report['dispatch_rate_per_second'] == .2
    assert report['drained_rate_per_second'] == .1
    assert report['groups']['general-primary/streaming']['first_text_ms']['p95'] == 10
    assert report['groups']['general-backup/text']['first_text_ms']['samples'] == 0
    assert report['success_rate'] == .5


def test_record_checker_rejects_duplicate_request_and_response_db_mismatch():
    import f10_metrics
    assert callable(getattr(f10_metrics, "validate_records", None)), "missing capacity validate_records contract"
    validate_records = f10_metrics.validate_records
    good = {'request_id': 'a', 'status': 'completed', 'error_code': None}
    validate_records([good], {'a': {'status': 'completed'}})
    with pytest.raises(AssertionError, match='duplicate'):
        validate_records([good, good], {'a': {'status': 'completed'}})
    with pytest.raises(AssertionError, match='terminal'):
        validate_records([good], {'a': {'status': 'failed'}})
    with pytest.raises(AssertionError, match='unexpected'):
        validate_records([dict(good, status='failed', error_code='internal_error')], {'a': {'status': 'failed'}})


def test_model_quality_is_counted_separately_from_successful_gateway_delivery():
    from f10_metrics import summarize
    rows=[dict(request_id='a',model='general-primary',mode='text',status='completed',error_code=None,
               elapsed_ms=100,first_text_ms=None,create_ms=None,quality_ok=False),
          dict(request_id='b',model='general-primary',mode='text',status='failed',error_code='provider_timeout',
               elapsed_ms=100,first_text_ms=None,create_ms=None,quality_ok=None)]
    report=summarize(rows,dispatch_seconds=1,elapsed_seconds=2)
    assert report['quality_evaluated']==1 and report['quality_anomalies']==['a']
    assert report['successes']==1 and report['failures']=={'provider_timeout':1}


def test_sustained_accepts_proven_upstream_failure_but_rejects_local_or_inconsistent_failures():
    from f10_metrics import validate_sustained
    good={'request_id':'ok','status':'completed','error_code':None}
    failed={'request_id':'u','status':'failed','error_code':'provider_unavailable'}
    observed={'u':{'status':'failed','error_code':'provider_unavailable',
                   'server_timing':{'error_stage':'provider'},
                   'external_failure_evidence':{'request_id':'u','kind':'upstream_http_error'}}}
    validate_sustained({'records':[good,failed],'verified':observed})
    with pytest.raises(AssertionError):
        validate_sustained({'records':[good,dict(failed,error_code='storage_unavailable')],'verified':observed})
    with pytest.raises(AssertionError):
        validate_sustained({'records':[good,failed],'verified':{'u':dict(observed['u'],error_code='internal_error')}})
    with pytest.raises(AssertionError):
        validate_sustained({'records':[good,failed],'verified':{'u':dict(observed['u'],server_timing={'error_stage':'finish'})}})


def test_sustained_requires_failure_scene_proof_and_accepts_proven_stream_deadline():
    from f10_metrics import validate_sustained
    good={'request_id':'ok','status':'completed','error_code':None}
    failed={'request_id':'u','status':'failed','error_code':'request_timeout'}
    observed={'status':'failed','error_code':'request_timeout','server_timing':{'error_stage':'provider'},
              'external_failure_evidence':{'request_id':'u','kind':'upstream_stream_stall'}}
    report={'records':[good,failed],'verified':{'u':observed}}
    validate_sustained(report)
    for proof in (None,{}, {'request_id':'different','kind':'upstream_stream_stall'},
                  {'request_id':'u','kind':'unattributed'}):
        with pytest.raises(AssertionError):
            validate_sustained(dict(report,verified={'u':dict(observed,external_failure_evidence=proof)}))
    with pytest.raises(AssertionError):
        validate_sustained({'records':[failed],'verified':{'u':observed}})
