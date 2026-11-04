from services.concurrency import ConcurrencyLimiter
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from f3_cases import MOCK_USAGE, registry as mock_registry
from contracts import LLMRequest


def test_request_rejects_numeric_model_without_coercion(payload):
    payload["model"] = 123
    with pytest.raises(ValidationError):
        LLMRequest.model_validate(payload)


def test_request_preserves_whitespace(payload):
    content = "  print('hello')\n "
    payload["messages"][0]["content"] = content
    assert LLMRequest.model_validate(payload).messages[0].content == content


def test_system_prompt_matches_approved_document():
    from services.prompts import SYSTEM_PROMPT, build_messages

    document = Path(__file__).resolve().parents[2] / "docs/features/F1/design.md"
    approved = re.search(r"```text\n(.*?)\n```", document.read_text(), re.S).group(1)
    assert SYSTEM_PROMPT == approved
    from contracts import Message, PromptSelection
    user = Message(role="user", content="question")
    result = build_messages(PromptSelection(name="basic_text", version="1.0.0"), [user])
    assert [(m.role, m.content) for m in result] == [("system", approved), ("user", "question")]


async def test_gateway_calls_only_provider_and_tracks_attempt(payload):
    from providers.mock import MockProvider
    from services.gateway import GatewayService
    from services.trace import TraceStore

    provider = MockProvider(text="chosen answer")
    store = TraceStore()
    trace = store.start_request("unit-request")
    trace.agent_id = 'unit-agent'
    from services.prompts import PromptRegistry, DEFAULT_TEMPLATES
    from services.token_budget import TokenBudget
    registry = mock_registry()
    result = await GatewayService({name: provider for name in registry.providers}, store,
                                  PromptRegistry(DEFAULT_TEMPLATES), registry, TokenBudget.from_models(registry.endpoints.values()), limiter=ConcurrencyLimiter((registry).concurrency)).complete(LLMRequest.model_validate(payload), trace)
    assert result.text == "chosen answer"
    assert trace.requested_model == "general-primary"
    assert store.attempts[0].actual_model is None
    assert result.usage.model_dump() == MOCK_USAGE
    assert trace.attempts == 1
    assert len(store.attempts) == 1
    assert store.attempts[0].status == "completed"
    assert len(provider.calls) == 1


def test_trace_summary_can_only_finish_once():
    from services.trace import TraceStore

    store = TraceStore()
    trace = store.start_request("one")
    store.finish_request(trace, error_code=None)
    with pytest.raises(RuntimeError):
        store.finish_request(trace, error_code=None)
