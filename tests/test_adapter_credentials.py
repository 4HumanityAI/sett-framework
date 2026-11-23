"""
Credential validation, checked once for every adapter that takes a key.

Written as one parametrized table rather than a case per adapter for a
specific reason: the whitespace-only key defect was originally fixed in
three of the four adapters and missed in the fourth, because each one
carries its own copy of the same two lines and the fourth had a
parenthesis in a different place. A per-adapter test would have been
written for the three that were already fixed.

Adding a new adapter that takes an API key means adding one row to
``KEYED_ADAPTERS`` below, and it inherits every case here.
"""
from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from sett.exceptions import SETTError


def _stub_modules(*names: str) -> dict[str, ModuleType]:
    """Minimal importable stand-ins for provider SDKs.

    The adapters import their SDK before validating the key, so without
    these the constructor would fail for the wrong reason and the test
    would pass without ever reaching the check it exists for.
    """
    modules: dict[str, ModuleType] = {}
    for name in names:
        parts = name.split(".")
        for index in range(1, len(parts) + 1):
            partial = ".".join(parts[:index])
            module = ModuleType(partial)
            module.__dict__.update(
                {
                    "Anthropic": MagicMock(),
                    "OpenAI": MagicMock(),
                    "Client": MagicMock(),
                }
            )
            modules[partial] = module
        if len(parts) > 1:
            setattr(modules[parts[0]], parts[1], modules[name])
    return modules


def _elevenlabs(**kwargs):
    from sett.services_tts_stt.elevenlabs import ElevenLabsTTSAdapter

    return ElevenLabsTTSAdapter(**{"model_id": "test-model", **kwargs})


def _openai(**kwargs):
    with patch.dict(sys.modules, _stub_modules("openai")):
        from sett.services_llm.openai import OpenAIAdapter

        return OpenAIAdapter(**{"model": "test-model", **kwargs})


def _anthropic(**kwargs):
    with patch.dict(sys.modules, _stub_modules("anthropic")):
        from sett.services_llm.anthropic import AnthropicAdapter

        return AnthropicAdapter(**{"model": "test-model", **kwargs})


def _gemini(**kwargs):
    with patch.dict(sys.modules, _stub_modules("google.genai")):
        from sett.services_llm.gemini import GeminiAdapter

        return GeminiAdapter(**{"model": "test-model", **kwargs})


def _deepgram(**kwargs):
    from sett.services_tts_stt.deepgram import DeepgramSTTAdapter

    return DeepgramSTTAdapter(**{"model": "test-model", **kwargs})


def _azure_stt(**kwargs):
    from sett.services_tts_stt.azure import AzureSTTAdapter

    return AzureSTTAdapter(region="eastus", **kwargs)


def _azure_tts(**kwargs):
    from sett.services_tts_stt.azure import AzureTTSAdapter

    return AzureTTSAdapter(region="eastus", **kwargs)


def _assemblyai(**kwargs):
    from sett.services_tts_stt.assemblyai import AssemblyAISTTAdapter

    return AssemblyAISTTAdapter(**kwargs)


def _gladia(**kwargs):
    from sett.services_tts_stt.gladia import GladiaSTTAdapter

    return GladiaSTTAdapter(**kwargs)


def _whisper(**kwargs):
    from sett.services_tts_stt.openai_audio import WhisperSTTAdapter

    return WhisperSTTAdapter(**{"model": "test-model", **kwargs})


def _cartesia(**kwargs):
    from sett.services_tts_stt.cartesia import CartesiaTTSAdapter

    return CartesiaTTSAdapter(voice_id="test-voice", **{"model": "test-model", **kwargs})


# (label, constructor, environment variable it falls back to)
KEYED_ADAPTERS = [
    ("ElevenLabsTTSAdapter", _elevenlabs, "ELEVENLABS_API_KEY"),
    ("OpenAIAdapter", _openai, "OPENAI_API_KEY"),
    ("AnthropicAdapter", _anthropic, "ANTHROPIC_API_KEY"),
    ("GeminiAdapter", _gemini, "GOOGLE_API_KEY"),
    ("DeepgramSTTAdapter", _deepgram, "DEEPGRAM_API_KEY"),
    ("AzureSTTAdapter", _azure_stt, "AZURE_SPEECH_KEY"),
    ("AzureTTSAdapter", _azure_tts, "AZURE_SPEECH_KEY"),
    ("AssemblyAISTTAdapter", _assemblyai, "ASSEMBLYAI_API_KEY"),
    ("GladiaSTTAdapter", _gladia, "GLADIA_API_KEY"),
    ("WhisperSTTAdapter", _whisper, "OPENAI_API_KEY"),
    ("CartesiaTTSAdapter", _cartesia, "CARTESIA_API_KEY"),
]

_IDS = [entry[0] for entry in KEYED_ADAPTERS]


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch):
    """No adapter may quietly pass because the machine has a real key set."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    for _, _, env_var in KEYED_ADAPTERS:
        monkeypatch.delenv(env_var, raising=False)


@pytest.mark.parametrize("label, build, env_var", KEYED_ADAPTERS, ids=_IDS)
@pytest.mark.parametrize("blank", ["   ", "\t", "\n", " \t\n "])
def test_a_whitespace_only_explicit_key_is_refused(label, build, env_var, blank):
    """The defect: a truthy but empty key builds fine and fails as a 401.

    Refusing here turns a provider authentication error at the first real
    call into a configuration error at construction, which is where the
    mistake actually is.
    """
    with pytest.raises(SETTError) as excinfo:
        build(api_key=blank)
    assert "key" in str(excinfo.value).lower()


@pytest.mark.parametrize("label, build, env_var", KEYED_ADAPTERS, ids=_IDS)
def test_a_whitespace_only_environment_key_is_refused(
    label, build, env_var, monkeypatch
):
    monkeypatch.setenv(env_var, "   ")
    with pytest.raises(SETTError):
        build()


@pytest.mark.parametrize("label, build, env_var", KEYED_ADAPTERS, ids=_IDS)
def test_a_missing_key_is_refused(label, build, env_var):
    with pytest.raises(SETTError):
        build()


@pytest.mark.parametrize("label, build, env_var", KEYED_ADAPTERS, ids=_IDS)
def test_a_real_key_is_accepted_and_stored_stripped(label, build, env_var):
    adapter = build(api_key="  real-key  ")
    assert adapter._api_key == "real-key"


@pytest.mark.parametrize("label, build, env_var", KEYED_ADAPTERS, ids=_IDS)
def test_an_environment_key_is_accepted_and_stripped(
    label, build, env_var, monkeypatch
):
    monkeypatch.setenv(env_var, "  env-key  ")
    adapter = build()
    assert adapter._api_key == "env-key"


def test_every_keyed_adapter_in_the_package_is_covered_here():
    """A new adapter that takes an api_key must join the table above.

    Without this, the next adapter repeats the omission this file was
    written for: the fix lands in the files that already had the pattern
    and the new one is never checked.
    """
    import pathlib
    import re

    package_root = pathlib.Path(__file__).resolve().parents[1] / "sett"
    keyed: set[str] = set()
    for path in package_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "api_key" not in text:
            continue
        for match in re.finditer(r"^class (\w+)\(", text, flags=re.MULTILINE):
            name = match.group(1)
            if name.endswith("Adapter"):
                keyed.add(name)

    covered = {label for label, _, _ in KEYED_ADAPTERS}
    uncovered = sorted(keyed - covered)
    assert not uncovered, (
        f"These adapters take an api_key but are not in KEYED_ADAPTERS: "
        f"{uncovered}. Add a row so they inherit the credential checks."
    )
