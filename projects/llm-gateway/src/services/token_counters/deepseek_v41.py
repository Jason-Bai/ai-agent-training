from pathlib import Path
import hashlib
import json

from tokenizers import Tokenizer

from contracts import Message
from utils.json_codec import dumps
from services.models import ModelConfig


DEFAULT_ASSET_DIR = Path(__file__).resolve().parents[3] / "assets/tokenizers/deepseek-v4.1-flash"
REVISION = "314113f8a7dadb388215396ba5fbee2c610a04d6"
RESOURCE_HASHES = {
    "tokenizer.json": "c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b",
    "reference_encoding.py": "13231c592e958fb39e2af6001dae69d90e7a36b265702b9b390236fb387599e2",
    "LICENSE": "f2c6c602815669d292889e5be8c802f2ed950653b77999b1584e8e6aed25d040",
}


class DeepSeekV41Counter:
    supports_generation_constraints = True

    def __init__(self, asset_dir: Path = DEFAULT_ASSET_DIR):
        try:
            manifest = json.loads((asset_dir / "manifest.json").read_text())
            if manifest["revision"] != REVISION or manifest["model"] != "deepseek-flash" or manifest["mode"] != "chat":
                raise ValueError
            for name, digest in RESOURCE_HASHES.items():
                if manifest["files"][name]["sha256"] != digest or hashlib.sha256((asset_dir / name).read_bytes()).hexdigest() != digest:
                    raise ValueError
            self._tokenizer = Tokenizer.from_file(str(asset_dir / "tokenizer.json"))
        except Exception:
            raise ValueError("计数资源缺失、损坏或版本不匹配") from None
        self.resource_revision = REVISION
        self.version = f"{REVISION}:response-schema-v1"
        self._model_id = manifest["model"]

    @staticmethod
    def encode(messages: list[Message], *, generation_constraint: dict | None = None) -> str:
        """Pinned official encoding's text-only, non-thinking role transitions.

        Reference encoding.py: render_message/_encode_messages_text. No tools,
        image processing or model code is loaded. Independent official goldens
        cover complete encoded bytes, not just a content token sum.
        """
        parts = ["<｜begin▁of▁sentence｜>"]
        if generation_constraint is not None and (not messages or messages[0].role != "system"):
            raise ValueError("生成约束计数需要受控 System 消息")
        for index, message in enumerate(messages):
            if message.role == "system":
                parts.extend(["<｜System｜>", message.content])
                if index == 0 and generation_constraint is not None:
                    # Count-only view of the pinned reference's response_format.
                    # The outgoing messages and fixed System are never mutated.
                    parts.extend(["\n\n## Response Format:\n\nYou MUST strictly adhere to the following schema to reply:\n",
                                  dumps(generation_constraint, compact=False)])
            elif message.role == "user":
                parts.extend(["<｜User｜>", message.content])
            else:
                parts.extend([message.content, "<｜end▁of▁sentence｜>"])
            next_is_assistant = index + 1 == len(messages) or messages[index + 1].role == "assistant"
            if next_is_assistant and (message.role == "user" or (message.role == "system" and index > 0)):
                parts.append("<｜Assistant｜></think>")
        return "".join(parts)

    def count(self, messages: list[Message], *, generation_constraint: dict | None = None) -> int:
        return len(self._tokenizer.encode(self.encode(messages, generation_constraint=generation_constraint), add_special_tokens=False).ids)

    def supports(self, model: ModelConfig) -> bool:
        return model.model_id == self._model_id and model.thinking_mode == "disabled"
