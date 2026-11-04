"""The same public acceptance steps on a test-owned scripted Mock process."""
import pytest

from f10_support import mock_acceptance_environment
from bug002_scenarios import (
    agent_success_and_cost, disconnect_resume_keeps_one_generation,
    cancel_pending_and_in_progress,
)

MODELS = ['general-primary', 'general-backup']


@pytest.mark.parametrize('model', MODELS)
@pytest.mark.parametrize('mode', ['text','structured','streaming'])
async def test_mock_agent_success_and_cost(mock_acceptance_environment, model, mode, record_property):
    await agent_success_and_cost(mock_acceptance_environment, model, mode, record_property)


@pytest.mark.parametrize('model', MODELS)
async def test_mock_disconnect_resume_keeps_one_generation(mock_acceptance_environment, model, record_property):
    await disconnect_resume_keeps_one_generation(mock_acceptance_environment, model, record_property)


@pytest.mark.parametrize('model', MODELS)
async def test_mock_cancel_pending_and_in_progress(mock_acceptance_environment, model, record_property):
    await cancel_pending_and_in_progress(mock_acceptance_environment, model, record_property)
