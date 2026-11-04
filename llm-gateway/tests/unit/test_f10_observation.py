from decimal import Decimal
import json

from f10_support import BOOTSTRAP


def observer(tmp_path):
    namespace={'__file__':str(tmp_path/'bootstrap.py')}
    exec(compile(BOOTSTRAP,'f10_bootstrap.py','exec'),namespace)
    return namespace


def test_provider_raw_precision_is_serializable_without_changing_input(tmp_path):
    ns=observer(tmp_path)
    raw={'temperature':Decimal('1.0000000000000001'),'usage':{'output_tokens':1},'output':'订'}
    path=tmp_path/'observed.json'
    ns['record_json'](path,raw)
    loaded=json.loads(path.read_text(),parse_float=Decimal)
    assert loaded==raw and type(raw['temperature']) is Decimal
    assert not ns['OBSERVATION_ERRORS']


def test_observation_io_failure_is_reported_without_failing_provider(tmp_path,monkeypatch):
    ns=observer(tmp_path)
    def unavailable(*args,**kwargs):
        raise OSError('controlled observation failure')
    monkeypatch.setattr(type(tmp_path),'write_text',unavailable)
    ns['record_json'](tmp_path/'observed.json',{})
    assert ns['OBSERVATION_ERRORS']==['OSError']


def test_test_credential_headers_are_redacted_in_failure_arguments():
    from f10_support import CredentialHeaders
    headers=CredentialHeaders(Authorization='Bearer transient-test-key')
    assert headers['Authorization']=='Bearer transient-test-key'
    assert 'transient-test-key' not in repr(headers)


def test_capacity_quality_separates_upstream_answer_from_gateway_corruption():
    from f10_support import verified_quality
    assert verified_quality('订单 A123 商品是充电器','订单 A123 商品是充电器',require_facts=True)
    assert verified_quality('订','订',require_facts=False) is False
    import pytest
    with pytest.raises(AssertionError,match='fact'):
        verified_quality('订','订',require_facts=True)
    with pytest.raises(AssertionError,match='upstream'):
        verified_quality('订','订单 A123 商品是充电器',require_facts=False)
