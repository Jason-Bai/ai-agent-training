"""Two-layer acceptance on a real Gateway process and explicit real Provider."""
import pytest

from f11_support import f11_real_environment
from f11_scenarios import (global_admission_and_pending, agent_credential_and_model_isolation,
                           stream_disconnect_retains_capacity)

pytestmark = pytest.mark.live

MODELS = ['general-primary', 'general-backup']


@pytest.mark.parametrize('f11_real_environment', [{'max_requests':1,'max_requests_per_agent':1}], indirect=True)
@pytest.mark.parametrize('model', MODELS)
async def test_global_admission_and_pending(f11_real_environment, model, record_property):
    await global_admission_and_pending(f11_real_environment, model, record_property)


@pytest.mark.parametrize('f11_real_environment', [{'max_requests':2,'max_requests_per_agent':1}], indirect=True)
@pytest.mark.parametrize('model', MODELS)
async def test_agent_credential_and_model_isolation(f11_real_environment, model, record_property):
    await agent_credential_and_model_isolation(f11_real_environment, model, record_property)


@pytest.mark.parametrize('f11_real_environment', [{'max_requests':1,'max_requests_per_agent':1}], indirect=True)
@pytest.mark.parametrize('model', MODELS)
async def test_stream_disconnect_retains_capacity(f11_real_environment, model, record_property):
    await stream_disconnect_retains_capacity(f11_real_environment, model, record_property)
