"""Official voice adapter roster and contract behavior for v0.13.0."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sett import CapabilitySupport
from sett.services_tts_stt import (
    AssemblyAISTTAdapter,
    AzureSTTAdapter,
    AzureTTSAdapter,
    CartesiaTTSAdapter,
    DeepgramSTTAdapter,
    EdgeTTSAdapter,
    GladiaSTTAdapter,
    KokoroTTSAdapter,
    LocalSTTAdapter,
    LocalTTSAdapter,
    PiperTTSAdapter,
    WhisperSTTAdapter,
    _http,
)
from sett.testing import assert_adapter_contract, assert_cancellation_preflight


class Response:
    def __init__(self, payload=None, *, content=b"audio", status=200):
        self._payload = payload
        self.content = content
        self.status_code = status
        self.text = "provider detail"
        self.headers = {}

    def json(self):
        return self._payload


class FakeRequests:
    class exceptions:
        class RequestException(Exception):
            pass

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


@pytest.fixture
def fake_requests(monkeypatch):
    client = FakeRequests([])
    monkeypatch.setattr(_http, "requests", client)
    return client


def test_remote_roster_is_instantiable_and_declares_honest_contracts(fake_requests):
    adapters = [
        DeepgramSTTAdapter("key"),
        AzureSTTAdapter("key", "eastus"),
        AzureTTSAdapter("key", "eastus"),
        AssemblyAISTTAdapter("key"),
        GladiaSTTAdapter("key"),
        WhisperSTTAdapter("key"),
        CartesiaTTSAdapter("voice", "key"),
    ]
    for adapter in adapters:
        assert_adapter_contract(adapter, adapter.capabilities.kind)
        assert adapter.capabilities.streaming is CapabilitySupport.NO
    assert adapters[-1].capabilities.voice_cloning is CapabilitySupport.YES


def test_local_and_community_roster_is_instantiable_without_loading_models():
    adapters = [
        LocalTTSAdapter(),
        LocalSTTAdapter(),
        KokoroTTSAdapter(),
        PiperTTSAdapter("voice.onnx"),
        EdgeTTSAdapter(),
    ]
    for adapter in adapters:
        assert_adapter_contract(adapter, adapter.capabilities.kind)
        assert adapter.capabilities.streaming is CapabilitySupport.NO
    assert EdgeTTSAdapter().capabilities.metadata["community_unofficial"] is True


def test_deepgram_transcription_uses_documented_binary_endpoint(fake_requests):
    fake_requests.responses.append(
        Response({"results": {"channels": [{"alternatives": [{"transcript": "hello"}]}]}})
    )
    adapter = DeepgramSTTAdapter("secret", language="en-US")
    assert adapter.transcribe(b"wav") == "hello"
    method, url, kwargs = fake_requests.calls[0]
    assert method == "POST"
    assert "/v1/listen?" in url
    assert kwargs["headers"]["Authorization"] == "Token secret"
    assert kwargs["data"] == b"wav"


def test_azure_tts_escapes_ssml_and_returns_audio(fake_requests):
    fake_requests.responses.append(Response(content=b"mp3"))
    adapter = AzureTTSAdapter("key", "eastus")
    assert adapter.synthesize("A < B") == b"mp3"
    body = fake_requests.calls[0][2]["data"].decode()
    assert "A &lt; B" in body


def test_whisper_and_cartesia_requests_are_explicit(fake_requests):
    fake_requests.responses.extend([Response({"text": "hosted"}), Response(content=b"mp3")])
    whisper = WhisperSTTAdapter("key")
    cartesia = CartesiaTTSAdapter("voice", "key")
    assert whisper.transcribe(b"wav") == "hosted"
    assert cartesia.synthesize("hello") == b"mp3"
    assert fake_requests.calls[0][2]["data"]["model"] == "whisper-1"
    assert fake_requests.calls[1][2]["json"]["voice"] == {"id": "voice"}


def test_assemblyai_upload_submit_and_poll(fake_requests):
    fake_requests.responses.extend(
        [
            Response({"upload_url": "https://upload"}),
            Response({"id": "job"}),
            Response({"status": "completed", "text": "done"}),
        ]
    )
    adapter = AssemblyAISTTAdapter("key", poll_interval_seconds=0.001)
    assert adapter.transcribe(b"wav") == "done"
    assert [call[0] for call in fake_requests.calls] == ["POST", "POST", "GET"]


def test_gladia_upload_submit_and_poll(fake_requests):
    fake_requests.responses.extend(
        [
            Response({"audio_url": "https://upload"}),
            Response({"id": "job", "result_url": "https://result"}),
            Response(
                {
                    "status": "done",
                    "result": {"transcription": {"full_transcript": "done"}},
                }
            ),
        ]
    )
    adapter = GladiaSTTAdapter("key", poll_interval_seconds=0.001)
    assert adapter.transcribe(b"wav") == "done"


class FakeEngine:
    def __init__(self):
        self.path = None

    def setProperty(self, name, value):
        pass

    def save_to_file(self, text, path):
        self.path = path

    def runAndWait(self):
        Path(self.path).write_bytes(b"wav")


def test_local_tts_returns_bytes_without_playback():
    adapter = LocalTTSAdapter(engine_factory=FakeEngine)
    assert adapter.synthesize("hello") == b"wav"


def test_local_stt_lazily_loads_and_consumes_segment_generator():
    model = SimpleNamespace(
        transcribe=lambda path, **kwargs: (
            iter([SimpleNamespace(text="hello"), SimpleNamespace(text="world")]),
            None,
        )
    )
    adapter = LocalSTTAdapter(model_factory=lambda *args, **kwargs: model)
    assert adapter.transcribe(b"wav") == "hello world"


def test_official_adapter_honors_precancel_before_http(fake_requests):
    adapter = DeepgramSTTAdapter("key")
    assert_cancellation_preflight(lambda **kwargs: adapter.transcribe(b"wav", **kwargs))
    assert fake_requests.calls == []
