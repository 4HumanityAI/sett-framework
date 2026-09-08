"""Kyutai Pocket TTS adapter with lazy local model loading."""
from __future__ import annotations

import wave
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any

from sett.core_ruler.adapter import (
    AdapterCapabilities,
    AdapterErrorKind,
    AdapterKind,
    CapabilitySupport,
    ConnectivityRequirement,
    adapter_call_scope,
    normalized_adapter_error,
)
from sett.core_ruler.lifecycle import pop_lifecycle_kwargs
from sett.exceptions import (
    SETTCancelledError,
    SETTServiceAdapterError,
    SETTTimeoutError,
)
from sett.services_tts_stt.base import TTSBase

_SUPPORTED_LANGUAGES = ("en", "fr", "de", "es", "it", "pt")
_DEFAULT_VOICES = {
    "french": "estelle",
    "german": "juergen",
    "italian": "giovanni",
    "portuguese": "rafael",
    "spanish": "lola",
}


def _configuration_error(message: str, reason_code: str) -> SETTServiceAdapterError:
    return SETTServiceAdapterError(
        message,
        kind=AdapterErrorKind.CONFIGURATION,
        provider="pocket-tts",
        adapter_name="PocketTTSAdapter",
        operation="synthesize",
        retryable=False,
        reason_code=reason_code,
    )


def _default_voice(language: str) -> str:
    folded = language.casefold()
    for name, voice in _DEFAULT_VOICES.items():
        if name in folded:
            return voice
    return "alba"


def _same_voice(left: Any, right: Any) -> bool:
    if left is right:
        return True
    if isinstance(left, (str, Path)) and isinstance(right, (str, Path)):
        return str(left) == str(right)
    return False


def _wav_bytes(audio: Any, sample_rate: Any) -> bytes:
    """Convert Kyutai's floating point tensor into mono PCM16 WAV bytes."""
    rate = int(sample_rate)
    if rate <= 0:
        raise ValueError("Pocket TTS returned an invalid sample rate.")
    samples = audio.detach().cpu().numpy()
    if hasattr(samples, "reshape"):
        samples = samples.reshape(-1)
    pcm = (samples.clip(-1.0, 1.0) * 32767.0).astype("<i2", copy=False)
    output = BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        wav_file.writeframes(pcm.tobytes())
    return output.getvalue()


class PocketTTSAdapter(TTSBase):
    """Generate complete WAV payloads through Kyutai Pocket TTS.

    The model and voice state load on first use. Model weights and catalog
    voices may require a network download before they are cached locally.
    The upstream model documents generation as not thread-safe, so one
    adapter instance serializes model loading, voice conditioning, and audio
    generation. Cancellation is cooperative before and after that provider
    call; it cannot interrupt generation already executing inside the model.
    """

    def __init__(
        self,
        *,
        voice: Any = None,
        language: str = "english",
        quantize: bool = False,
        model_factory: Callable[..., Any] | None = None,
    ) -> None:
        """Configure a language and voice while deferring model loading."""
        if not isinstance(language, str) or not language.strip():
            raise _configuration_error(
                "language must be a non-empty Pocket TTS model identifier.",
                "adapter.language_missing",
            )
        if isinstance(voice, str) and not voice.strip():
            raise _configuration_error(
                "voice must be a non-empty name, path, URL, or model state.",
                "adapter.voice_missing",
            )
        self._language = language.strip()
        self._voice = _default_voice(self._language) if voice is None else voice
        self._quantize = bool(quantize)
        self._model_factory = model_factory
        self._model: Any = None
        self._voice_state: Any = None
        self._voice_state_source: Any = None
        self._model_lock = Lock()

    @property
    def audio_format(self) -> str:
        """Return the mono PCM16 WAV container produced by this adapter."""
        return "wav"

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Declare the behavior exposed by SETT's complete-audio path."""
        return AdapterCapabilities(
            AdapterKind.TTS,
            streaming=CapabilitySupport.NO,
            voice_cloning=CapabilitySupport.YES,
            partial_results=CapabilitySupport.NO,
            languages=_SUPPORTED_LANGUAGES,
            connectivity=ConnectivityRequirement.NONE,
            input_formats=("text/plain",),
            output_formats=("audio/wav",),
            metadata={
                "engine": "pocket-tts",
                "model_parameters": "100M",
                "model_loaded_lazily": True,
                "initial_model_download_may_require_network": True,
                "concurrent_calls_serialized": True,
            },
        )

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        factory = self._model_factory
        if factory is None:
            try:
                from pocket_tts import TTSModel
            except ImportError as error:
                raise SETTServiceAdapterError(
                    "The 'pocket-tts' package is required to use PocketTTSAdapter. "
                    "SETT installs from source; from your checkout run: "
                    "pip install -e \".[pocket-tts]\"",
                    kind=AdapterErrorKind.CONFIGURATION,
                    provider="pocket-tts",
                    adapter_name=type(self).__name__,
                    operation="synthesize",
                    retryable=False,
                    reason_code="adapter.dependency_missing",
                ) from error
            factory = TTSModel.load_model
        self._model = factory(language=self._language, quantize=self._quantize)
        return self._model

    def _get_voice_state(self, model: Any, voice: Any) -> Any:
        if self._voice_state is None or not _same_voice(voice, self._voice_state_source):
            self._voice_state = model.get_state_for_audio_prompt(voice)
            self._voice_state_source = voice
        return self._voice_state

    def synthesize(self, text: str, **kwargs: Any) -> bytes:
        """Generate a complete PCM16 WAV payload with an optional voice override."""
        with adapter_call_scope(self, "synthesize"):
            lifecycle = pop_lifecycle_kwargs(kwargs)
            lifecycle.throw_if_cancelled()
            voice = kwargs.pop("voice", self._voice)
            call_language = kwargs.pop("language", self._language)
            if str(call_language).strip() != self._language:
                raise _configuration_error(
                    "A language override requires a separate PocketTTSAdapter instance.",
                    "adapter.language_override_requires_instance",
                )
            if isinstance(voice, str) and not voice.strip():
                raise _configuration_error(
                    "voice must be a non-empty name, path, URL, or model state.",
                    "adapter.voice_missing",
                )
            try:
                with self._model_lock:
                    lifecycle.throw_if_cancelled()
                    model = self._get_model()
                    voice_state = self._get_voice_state(model, voice)
                    audio = model.generate_audio(
                        model_state=voice_state,
                        text_to_generate=text,
                        **kwargs,
                    )
                    lifecycle.throw_if_cancelled()
                    return _wav_bytes(audio, model.sample_rate)
            except (SETTCancelledError, SETTServiceAdapterError, SETTTimeoutError):
                raise
            except Exception as error:
                raise normalized_adapter_error(
                    error,
                    error_type=SETTServiceAdapterError,
                    provider="pocket-tts",
                    adapter_name=type(self).__name__,
                    operation="synthesize",
                ) from error

    def __repr__(self) -> str:
        return f"PocketTTSAdapter(language={self._language!r})"
