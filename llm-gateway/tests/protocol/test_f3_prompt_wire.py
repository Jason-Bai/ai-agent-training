import json
from pathlib import Path

import pytest

from contracts import Message
from providers.base import validate_result
from f3_cases import registry
from test_providers import adapter_type, server, sent_messages, assert_sent_budget


GOLDENS = json.loads((Path(__file__).parents[1] / "fixtures/f3_token_goldens.json").read_text())["cases"][:3]


@pytest.mark.parametrize("case", GOLDENS, ids=lambda case: case["name"])
async def test_all_approved_prompt_messages_reach_real_transports_unchanged(adapter_type, server, case):
    adapter = adapter_type(server.url, "FAKE_CREDENTIAL_MARKER")
    try:
        result = await adapter.complete(registry().get_endpoint("general-primary"),
                                        [Message.model_validate(m) for m in case["messages"]],
                                        max_output_tokens=64, timeout_seconds=2)
        validate_result(result)
    finally:
        await adapter.aclose()
    assert len(server.calls) == 1
    assert sent_messages(server.calls[0]) == case["messages"]
    assert len(sent_messages(server.calls[0])) == (6 if case["name"] == "context_1.1" else 2)
    assert_sent_budget(server.calls[0],64)
