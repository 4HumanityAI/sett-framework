"""No adapter chooses a provider model on the application's behalf.

Which model an application uses is its own decision. An adapter built
without one fails at construction with a configuration error, and no
adapter module carries a default model identifier that would go stale as
providers retire models.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from test_adapter_credentials import (
    _anthropic,
    _cartesia,
    _deepgram,
    _elevenlabs,
    _gemini,
    _openai,
    _whisper,
)

from sett.exceptions import SETTAdapterError
from sett.services_llm.ollama import OllamaAdapter

ROOT = Path(__file__).resolve().parents[1]

# (label, constructor, name of the model argument)
ADAPTERS = [
    ("AnthropicAdapter", _anthropic, "model"),
    ("OpenAIAdapter", _openai, "model"),
    ("GeminiAdapter", _gemini, "model"),
    ("OllamaAdapter", lambda **kwargs: OllamaAdapter(**kwargs), "model"),
    ("DeepgramSTTAdapter", _deepgram, "model"),
    ("WhisperSTTAdapter", _whisper, "model"),
    ("CartesiaTTSAdapter", _cartesia, "model"),
    ("ElevenLabsTTSAdapter", _elevenlabs, "model_id"),
]


def _credentials(label: str) -> dict[str, str]:
    return {} if label == "OllamaAdapter" else {"api_key": "test-key"}


@pytest.mark.parametrize("missing", [None, "", "   "])
@pytest.mark.parametrize(
    ("label", "build", "argument"), ADAPTERS, ids=[row[0] for row in ADAPTERS]
)
def test_an_adapter_without_a_model_is_refused(label, build, argument, missing):
    with pytest.raises(SETTAdapterError) as caught:
        build(**_credentials(label), **{argument: missing})
    assert caught.value.reason_code == "adapter.model_missing"
    assert caught.value.adapter_name == label


@pytest.mark.parametrize(
    ("label", "build", "argument"), ADAPTERS, ids=[row[0] for row in ADAPTERS]
)
def test_the_model_the_application_chose_is_kept(label, build, argument):
    adapter = build(**_credentials(label), **{argument: "  chosen-model  "})
    stored = [
        getattr(adapter, name, None)
        for name in ("_model", "_model_name", "_model_id")
    ]
    assert "chosen-model" in stored


def test_no_provider_adapter_defines_a_default_model():
    offenders = []
    for folder in ("services_llm", "services_tts_stt"):
        for path in sorted((ROOT / "sett" / folder).glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    name = getattr(target, "id", "")
                    if name in {"DEFAULT_MODEL", "DEFAULT_MODEL_ID"}:
                        offenders.append(f"{path.name}: {name}")
    assert not offenders, offenders
