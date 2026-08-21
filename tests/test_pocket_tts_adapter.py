"""Contract and integration-boundary tests for PocketTTSAdapter."""
from __future__ import annotations

import io
import threading
import time
import wave

import pytest

from sett import AdapterErrorKind, AdapterKind, CapabilitySupport
from sett.exceptions import SETTServiceAdapterError
from sett.services_tts_stt import PocketTTSAdapter
from sett.testing import (
    assert_adapter_contract,
    assert_cancellation_preflight,
    assert_normalized_adapter_error,
)


class FakeArray:
    def __init__(self, frames: bytes = b"\x00\x00\xff\x7f"):
        self.frames = frames

    def reshape(self, size):
        assert size == -1
        return self

    def clip(self, minimum, maximum):
        assert (minimum, maximum) == (-1.0, 1.0)
        return self

    def __mul__(self, factor):
        assert factor == 32767.0
        return self

    def astype(self, dtype, copy=False):
        assert dtype == "<i2"
        assert copy is False
        return self

    def tobytes(self):
        return self.frames


class FakeTensor:
    def __init__(self, frames: bytes = b"\x00\x00\xff\x7f"):
        self.array = FakeArray(frames)

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.array


class FakePocketModel:
    sample_rate = 24000

    def __init__(self):
        self.voice_calls = []
        self.generation_calls = []

    def get_state_for_audio_prompt(self, voice):
        self.voice_calls.append(voice)
        return {"voice": voice}

    def generate_audio(self, **kwargs):
        self.generation_calls.append(kwargs)
        return FakeTensor()


def test_pocket_tts_declares_the_exposed_contract_without_loading_a_model():
    factory_calls = []
    adapter = PocketTTSAdapter(model_factory=lambda **kwargs: factory_calls.append(kwargs))

    assert_adapter_contract(adapter, AdapterKind.TTS)
    assert adapter.capabilities.streaming is CapabilitySupport.NO
    assert adapter.capabilities.voice_cloning is CapabilitySupport.YES
    assert adapter.capabilities.languages == ("en", "fr", "de", "es", "it", "pt")
    assert adapter.capabilities.metadata["initial_model_download_may_require_network"]
    assert factory_calls == []


def test_pocket_tts_lazily_loads_and_returns_pcm16_wav_bytes():
    model = FakePocketModel()
    factory_calls = []

    def factory(**kwargs):
        factory_calls.append(kwargs)
        return model

    adapter = PocketTTSAdapter(language="spanish_24l", model_factory=factory)

    first = adapter.synthesize("hola")
    second = adapter.synthesize("otra vez")

    assert factory_calls == [{"language": "spanish_24l", "quantize": False}]
    assert model.voice_calls == ["lola"]
    assert [call["text_to_generate"] for call in model.generation_calls] == [
        "hola",
        "otra vez",
    ]
    assert first == second
    with wave.open(io.BytesIO(first), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 24000
        assert wav_file.readframes(wav_file.getnframes()) == b"\x00\x00\xff\x7f"


def test_pocket_tts_voice_override_refreshes_then_reuses_its_state():
    model = FakePocketModel()
    adapter = PocketTTSAdapter(voice="alba", model_factory=lambda **kwargs: model)

    adapter.synthesize("one")
    adapter.synthesize("dos", voice="lola")
    adapter.synthesize("tres", voice="lola")

    assert model.voice_calls == ["alba", "lola"]


def test_pocket_tts_preserves_normalized_provider_failures():
    class BrokenModel(FakePocketModel):
        def generate_audio(self, **kwargs):
            raise RuntimeError("provider detail")

    adapter = PocketTTSAdapter(model_factory=lambda **kwargs: BrokenModel())
    error = assert_normalized_adapter_error(lambda: adapter.synthesize("hello"))

    assert isinstance(error, SETTServiceAdapterError)
    assert error.provider == "pocket-tts"
    assert error.operation == "synthesize"


def test_pocket_tts_reports_blank_configuration_before_loading_a_model():
    with pytest.raises(SETTServiceAdapterError) as language_error:
        PocketTTSAdapter(language="  ")
    assert language_error.value.kind is AdapterErrorKind.CONFIGURATION
    assert language_error.value.reason_code == "adapter.language_missing"

    with pytest.raises(SETTServiceAdapterError) as voice_error:
        PocketTTSAdapter(voice="\t")
    assert voice_error.value.kind is AdapterErrorKind.CONFIGURATION
    assert voice_error.value.reason_code == "adapter.voice_missing"


def test_pocket_tts_honors_precancellation_before_model_loading():
    factory_calls = []
    adapter = PocketTTSAdapter(model_factory=lambda **kwargs: factory_calls.append(kwargs))

    assert_cancellation_preflight(lambda **kwargs: adapter.synthesize("hello", **kwargs))
    assert factory_calls == []


def test_pocket_tts_serializes_calls_to_the_non_thread_safe_model():
    class ObservedModel(FakePocketModel):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.maximum_active = 0
            self.observation_lock = threading.Lock()

        def generate_audio(self, **kwargs):
            with self.observation_lock:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
            time.sleep(0.005)
            with self.observation_lock:
                self.active -= 1
            return FakeTensor()

    model = ObservedModel()
    adapter = PocketTTSAdapter(model_factory=lambda **kwargs: model)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(adapter.synthesize("hello")))
        for _ in range(8)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 8
    assert model.maximum_active == 1
