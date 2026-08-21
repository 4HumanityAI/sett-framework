"""Official voice adapter roster and contract behavior."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sett import AdapterErrorKind, CapabilitySupport
from sett.core_ruler.execution_context import ExecutionContext
from sett.core_ruler.lifecycle import ExecutionControl, lifecycle_scope
from sett.exceptions import (
    SETTCancelledError,
    SETTServiceAdapterError,
    SETTTimeoutError,
)
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
    assert adapter.transcribe(b"wav", language_code="es_AR") == "hello"
    method, url, kwargs = fake_requests.calls[0]
    assert method == "POST"
    assert "/v1/listen?" in url
    assert kwargs["headers"]["Authorization"] == "Token secret"
    assert kwargs["data"] == b"wav"
    assert "language=es-AR" in url


def test_azure_tts_escapes_ssml_and_returns_audio(fake_requests):
    fake_requests.responses.append(Response(content=b"mp3"))
    adapter = AzureTTSAdapter("key", "eastus")
    assert adapter.synthesize("A < B") == b"mp3"
    body = fake_requests.calls[0][2]["data"].decode()
    assert "A &lt; B" in body


def test_azure_tts_uses_language_code_at_construction_and_per_call(fake_requests):
    fake_requests.responses.extend([Response(content=b"mp3"), Response(content=b"mp3")])
    adapter = AzureTTSAdapter("key", "eastus", language_code="es_MX")

    adapter.synthesize("hola")
    adapter.synthesize("bonjour", language_code="fr-FR")

    first_body = fake_requests.calls[0][2]["data"].decode()
    second_body = fake_requests.calls[1][2]["data"].decode()
    assert 'xml:lang="es-MX"' in first_body
    assert 'xml:lang="fr-FR"' in second_body


def test_azure_stt_language_code_is_not_silently_discarded(fake_requests):
    fake_requests.responses.extend(
        [Response({"DisplayText": "hola"}), Response({"DisplayText": "bonjour"})]
    )
    adapter = AzureSTTAdapter("key", "eastus", language_code="es_MX")

    assert adapter.transcribe(b"wav") == "hola"
    assert adapter.transcribe(b"wav", language_code="fr-FR") == "bonjour"

    assert "language=es-MX" in fake_requests.calls[0][1]
    assert "language=fr-FR" in fake_requests.calls[1][1]


def test_azure_language_remains_a_compatibility_alias(fake_requests):
    fake_requests.responses.extend(
        [Response(content=b"mp3"), Response({"DisplayText": "hola"})]
    )
    tts = AzureTTSAdapter("key", "eastus", language="es_MX")
    stt = AzureSTTAdapter("key", "eastus", language="es_MX")

    tts.synthesize("hola")
    stt.transcribe(b"wav")

    assert 'xml:lang="es-MX"' in fake_requests.calls[0][2]["data"].decode()
    assert "language=es-MX" in fake_requests.calls[1][1]


@pytest.mark.parametrize("adapter_type", [AzureTTSAdapter, AzureSTTAdapter])
def test_azure_rejects_conflicting_constructor_language_names(
    fake_requests, adapter_type
):
    with pytest.raises(SETTServiceAdapterError) as excinfo:
        adapter_type(
            "key",
            "eastus",
            language_code="es-AR",
            language="en-US",
        )

    error = excinfo.value
    assert error.kind is AdapterErrorKind.CONFIGURATION
    assert error.provider == "azure_speech"
    assert error.reason_code == "adapter.language_arguments_conflict"
    assert fake_requests.calls == []


@pytest.mark.parametrize(
    "adapter, invoke",
    [
        (
            lambda: AzureTTSAdapter("key", "eastus"),
            lambda value: value.synthesize(
                "hola", language_code="es-AR", language="en-US"
            ),
        ),
        (
            lambda: AzureSTTAdapter("key", "eastus"),
            lambda value: value.transcribe(
                b"wav", language_code="es-AR", language="en-US"
            ),
        ),
    ],
)
def test_azure_rejects_conflicting_call_language_names(
    fake_requests, adapter, invoke
):
    with pytest.raises(SETTServiceAdapterError) as excinfo:
        invoke(adapter())

    assert excinfo.value.reason_code == "adapter.language_arguments_conflict"
    assert fake_requests.calls == []


def test_whisper_and_cartesia_requests_are_explicit(fake_requests):
    fake_requests.responses.extend([Response({"text": "hosted"}), Response(content=b"mp3")])
    whisper = WhisperSTTAdapter("key")
    cartesia = CartesiaTTSAdapter("voice", "key")
    assert whisper.transcribe(b"wav", language_code="es-AR") == "hosted"
    assert cartesia.synthesize("hello", language_code="es_AR") == b"mp3"
    assert fake_requests.calls[0][2]["data"]["model"] == "whisper-1"
    assert fake_requests.calls[0][2]["data"]["language"] == "es"
    assert fake_requests.calls[1][2]["json"]["voice"] == {"id": "voice"}
    assert fake_requests.calls[1][2]["json"]["language"] == "es-AR"


def test_cartesia_refuses_blank_voice_id_as_configuration_error(fake_requests):
    for blank in ("", "   ", "\t\n"):
        with pytest.raises(SETTServiceAdapterError) as excinfo:
            CartesiaTTSAdapter(blank, "key")
        error = excinfo.value
        assert error.kind is AdapterErrorKind.CONFIGURATION
        assert error.provider == "cartesia"
        assert error.adapter_name == "CartesiaTTSAdapter"
        assert error.reason_code == "adapter.voice_id_missing"
    assert fake_requests.calls == []


def test_assemblyai_upload_submit_and_poll(fake_requests):
    fake_requests.responses.extend(
        [
            Response({"upload_url": "https://upload"}),
            Response({"id": "job"}),
            Response({"status": "completed", "text": "done"}),
        ]
    )
    adapter = AssemblyAISTTAdapter("key", poll_interval_seconds=0.001)
    assert adapter.transcribe(b"wav", language_code="es-AR") == "done"
    assert [call[0] for call in fake_requests.calls] == ["POST", "POST", "GET"]
    assert fake_requests.calls[1][2]["json"]["language_code"] == "es-AR"


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
    assert adapter.transcribe(b"wav", language_code="es_AR") == "done"
    assert fake_requests.calls[1][2]["json"]["language_config"] == {
        "languages": ["es-AR"]
    }


@pytest.mark.parametrize(
    "adapter, invoke",
    [
        (
            lambda: DeepgramSTTAdapter("key"),
            lambda value: value.transcribe(
                b"wav", language_code="es-AR", language="en-US"
            ),
        ),
        (
            lambda: GladiaSTTAdapter("key"),
            lambda value: value.transcribe(
                b"wav", language_code="es-AR", language="en-US"
            ),
        ),
        (
            lambda: WhisperSTTAdapter("key"),
            lambda value: value.transcribe(
                b"wav", language_code="es-AR", language="en-US"
            ),
        ),
        (
            lambda: CartesiaTTSAdapter("voice", "key"),
            lambda value: value.synthesize(
                "hola", language_code="es-AR", language="en-US"
            ),
        ),
    ],
)
def test_official_voice_adapters_reject_conflicting_language_names_before_io(
    fake_requests, adapter, invoke
):
    with pytest.raises(SETTServiceAdapterError) as excinfo:
        invoke(adapter())

    assert excinfo.value.reason_code == "adapter.language_arguments_conflict"
    assert fake_requests.calls == []


class FakeVoice:
    def __init__(self, id_, languages=()):
        self.id = id_
        self.languages = languages


class FakeEngine:
    def __init__(self, voices=()):
        self.path = None
        self.voices = list(voices)
        self.properties = {}

    def setProperty(self, name, value):
        self.properties[name] = value

    def getProperty(self, name):
        if name == "voices":
            return self.voices
        return self.properties.get(name)

    def save_to_file(self, text, path):
        self.path = path

    def runAndWait(self):
        Path(self.path).write_bytes(b"wav")


def test_local_tts_returns_bytes_without_playback():
    adapter = LocalTTSAdapter(engine_factory=FakeEngine)
    assert adapter.synthesize("hello") == b"wav"


def test_local_tts_matches_a_language_code_to_an_installed_voice():
    engine = FakeEngine(voices=[
        FakeVoice("voice-en", languages=["en-US"]),
        FakeVoice("voice-es", languages=["es-MX"]),
    ])
    adapter = LocalTTSAdapter(engine_factory=lambda: engine)

    adapter.synthesize("hola", language_code="es-AR")

    assert engine.properties["voice"] == "voice-es"


def test_local_tts_accepts_legacy_espeak_language_metadata():
    engine = FakeEngine(voices=[FakeVoice("voice-en", languages=[b"\x05en-US"])])
    adapter = LocalTTSAdapter(engine_factory=lambda: engine)

    adapter.synthesize("hello", language_code="en-GB")

    assert engine.properties["voice"] == "voice-en"


def test_local_tts_accepts_macos_nsss_underscore_language_metadata():
    engine = FakeEngine(voices=[FakeVoice("voice-es", languages=["es_MX"])])
    adapter = LocalTTSAdapter(engine_factory=lambda: engine)

    adapter.synthesize("hola", language_code="es-AR")

    assert engine.properties["voice"] == "voice-es"


def test_local_tts_normalizes_voice_discovery_failures():
    class BrokenEngine(FakeEngine):
        def getProperty(self, name):
            raise RuntimeError("voice metadata unavailable")

    adapter = LocalTTSAdapter(engine_factory=BrokenEngine)

    with pytest.raises(SETTServiceAdapterError) as excinfo:
        adapter.synthesize("hello", language_code="en")

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert excinfo.value.provider == "pyttsx3"
    assert excinfo.value.operation == "synthesize"


def test_local_tts_preserves_cancellation_detected_after_provider_work():
    control = ExecutionControl.create_root(ExecutionContext.create())

    class CancellingEngine(FakeEngine):
        def save_to_file(self, text, path):
            super().save_to_file(text, path)
            control.cancel(reason="execution.provider_cancelled")

    adapter = LocalTTSAdapter(engine_factory=CancellingEngine)

    with control, lifecycle_scope(control), pytest.raises(SETTCancelledError) as excinfo:
        adapter.synthesize("hello")

    assert excinfo.value.reason == "execution.provider_cancelled"


def test_local_tts_preserves_provider_timeout_errors():
    class TimingOutEngine(FakeEngine):
        def runAndWait(self):
            raise SETTTimeoutError("provider deadline elapsed")

    adapter = LocalTTSAdapter(engine_factory=TimingOutEngine)

    with pytest.raises(SETTTimeoutError, match="provider deadline elapsed"):
        adapter.synthesize("hello")


def test_local_tts_does_not_guess_from_voice_id_text():
    """The regression this exists for: a voice id with no language
    metadata but a generic word containing the requested code as a
    literal substring (a real Windows SAPI5 id contains "Voices", which
    contains "es") must not be selected for that language."""
    engine = FakeEngine(voices=[
        FakeVoice(
            r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\EN_US",
            languages=[],
        ),
    ])
    adapter = LocalTTSAdapter(engine_factory=lambda: engine)

    adapter.synthesize("hola", language_code="es-AR")

    assert "voice" not in engine.properties


def test_local_tts_explicit_voice_id_overrides_language_code():
    engine = FakeEngine(voices=[FakeVoice("voice-es", languages=["es-MX"])])
    adapter = LocalTTSAdapter(engine_factory=lambda: engine)

    adapter.synthesize("hola", language_code="es-AR", voice_id="voice-manual")

    assert engine.properties["voice"] == "voice-manual"


def test_local_stt_lazily_loads_and_consumes_segment_generator():
    model = SimpleNamespace(
        transcribe=lambda path, **kwargs: (
            iter([SimpleNamespace(text="hello"), SimpleNamespace(text="world")]),
            None,
        )
    )
    adapter = LocalSTTAdapter(model_factory=lambda *args, **kwargs: model)
    assert adapter.transcribe(b"wav") == "hello world"


def test_local_stt_normalizes_a_bcp47_language_code_for_whisper():
    received_kwargs = {}

    def fake_transcribe(path, **kwargs):
        received_kwargs.update(kwargs)
        return iter([SimpleNamespace(text="hola")]), None

    model = SimpleNamespace(transcribe=fake_transcribe)
    adapter = LocalSTTAdapter(model_factory=lambda *args, **kwargs: model)

    assert adapter.transcribe(b"wav", language_code="ES-AR") == "hola"
    assert received_kwargs["language"] == "es"


def test_local_stt_accepts_a_bare_language_code_unchanged():
    received_kwargs = {}

    def fake_transcribe(path, **kwargs):
        received_kwargs.update(kwargs)
        return iter([]), None

    model = SimpleNamespace(transcribe=fake_transcribe)
    adapter = LocalSTTAdapter(model_factory=lambda *args, **kwargs: model)

    adapter.transcribe(b"wav", language_code="ja")
    assert received_kwargs["language"] == "ja"


def test_local_stt_keeps_language_as_a_compatibility_alias():
    received_kwargs = {}

    def fake_transcribe(path, **kwargs):
        received_kwargs.update(kwargs)
        return iter([]), None

    model = SimpleNamespace(transcribe=fake_transcribe)
    adapter = LocalSTTAdapter(model_factory=lambda *args, **kwargs: model)

    adapter.transcribe(b"wav", language="pt-BR")
    assert received_kwargs["language"] == "pt"


def test_local_stt_accepts_equivalent_language_argument_names():
    received_kwargs = {}

    def fake_transcribe(path, **kwargs):
        received_kwargs.update(kwargs)
        return iter([]), None

    model = SimpleNamespace(transcribe=fake_transcribe)
    adapter = LocalSTTAdapter(model_factory=lambda *args, **kwargs: model)

    adapter.transcribe(b"wav", language_code="es-AR", language="es_MX")
    assert received_kwargs["language"] == "es"


def test_local_stt_rejects_conflicting_language_argument_names():
    factory_calls = []
    model = SimpleNamespace(transcribe=lambda path, **kwargs: (iter([]), None))
    adapter = LocalSTTAdapter(
        model_factory=lambda *args, **kwargs: factory_calls.append((args, kwargs)) or model
    )

    with pytest.raises(SETTServiceAdapterError) as excinfo:
        adapter.transcribe(b"wav", language_code="es-AR", language="en")

    error = excinfo.value
    assert error.kind is AdapterErrorKind.CONFIGURATION
    assert error.provider == "faster-whisper"
    assert error.reason_code == "adapter.language_arguments_conflict"
    assert factory_calls == []


def test_local_stt_without_a_language_code_omits_the_argument():
    received_kwargs = {}

    def fake_transcribe(path, **kwargs):
        received_kwargs.update(kwargs)
        return iter([]), None

    model = SimpleNamespace(transcribe=fake_transcribe)
    adapter = LocalSTTAdapter(model_factory=lambda *args, **kwargs: model)

    adapter.transcribe(b"wav")
    assert "language" not in received_kwargs


def test_local_stt_preserves_cancellation_detected_after_provider_work():
    control = ExecutionControl.create_root(ExecutionContext.create())

    def cancelling_transcribe(path, **kwargs):
        control.cancel(reason="execution.provider_cancelled")
        return iter([SimpleNamespace(text="too late")]), None

    model = SimpleNamespace(transcribe=cancelling_transcribe)
    adapter = LocalSTTAdapter(model_factory=lambda *args, **kwargs: model)

    with control, lifecycle_scope(control), pytest.raises(SETTCancelledError) as excinfo:
        adapter.transcribe(b"wav")

    assert excinfo.value.reason == "execution.provider_cancelled"


def test_local_stt_preserves_provider_timeout_errors():
    def timing_out_transcribe(path, **kwargs):
        raise SETTTimeoutError("provider deadline elapsed")

    model = SimpleNamespace(transcribe=timing_out_transcribe)
    adapter = LocalSTTAdapter(model_factory=lambda *args, **kwargs: model)

    with pytest.raises(SETTTimeoutError, match="provider deadline elapsed"):
        adapter.transcribe(b"wav")


def test_official_adapter_honors_precancel_before_http(fake_requests):
    adapter = DeepgramSTTAdapter("key")
    assert_cancellation_preflight(lambda **kwargs: adapter.transcribe(b"wav", **kwargs))
    assert fake_requests.calls == []
