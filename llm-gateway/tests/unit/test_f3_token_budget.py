import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from contracts import Message
from errors import GatewayError
from services.token_budget import TokenBudget
from services.token_counters.deepseek_v41 import DEFAULT_ASSET_DIR, DeepSeekV41Counter


GOLDENS = json.loads((Path(__file__).parents[1] / "fixtures/f3_token_goldens.json").read_text())


@pytest.mark.parametrize("case", GOLDENS["cases"], ids=lambda case: case["name"])
def test_counter_matches_independent_official_encoding(case):
    counter = DeepSeekV41Counter()
    messages = [Message.model_validate(message) for message in case["messages"]]
    assert counter.count(messages) == case["count"]
    assert counter.encode(messages) == case["encoded"]
    assert counter.resource_revision == GOLDENS["revision"]
    assert counter.version == GOLDENS["revision"] + ':response-schema-v1'


@pytest.mark.parametrize("damage", ["missing", "tokenizer.json", "reference_encoding.py", "manifest.json", "LICENSE"])
def test_counter_rejects_missing_or_tampered_assets(tmp_path, damage):
    directory = tmp_path / "assets"
    if damage != "missing":
        shutil.copytree(DEFAULT_ASSET_DIR, directory)
        (directory / damage).write_text("PRIVATE_INPUT_MARKER")
    with pytest.raises(ValueError, match="计数资源") as exc:
        DeepSeekV41Counter(directory)
    assert "PRIVATE_INPUT_MARKER" not in str(exc.value)


def test_token_window_exact_boundary_and_one_over():
    model = SimpleNamespace(context_window_tokens=48, safety_margin_tokens=8)
    TokenBudget.check(24, 16, model)
    with pytest.raises(GatewayError) as exc:
        TokenBudget.check(25, 16, model)
    assert exc.value.code == "context_length_exceeded"
