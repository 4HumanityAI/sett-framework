"""Kokoro open-weight local text-to-speech adapter."""
from __future__ import annotations

import wave
from collections.abc import Callable
from io import BytesIO
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
from sett.exceptions import SETTServiceAdapterError
from sett.services_tts_stt.base import TTSBase


class KokoroTTSAdapter(TTSBase):
    """Generate 24 kHz mono WAV bytes with a lazily loaded Kokoro pipeline."""

    def __init__(
        self,
        *,
        voice: str = "af_heart",
        language_code: str = "a",
        device: str | None = None,
        pipeline_factory: Callable[..., Any] | None = None,
    ) -> None:
        """Configure a local voice while deferring all model loading."""
        self._voice = voice
        self._language_code = language_code
        self._device = device
        self._pipeline_factory = pipeline_factory
        self._pipeline: Any = None

    @property
    def audio_format(self) -> str:
        """Return the 24 kHz mono WAV container produced by this adapter."""
        return "wav"

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Declare the local, fixed-voice synthesis implementation."""
        return AdapterCapabilities(
            AdapterKind.TTS,
            streaming=CapabilitySupport.NO,
            voice_cloning=CapabilitySupport.NO,
            partial_results=CapabilitySupport.NO,
            connectivity=ConnectivityRequirement.NONE,
            input_formats=("text/plain",),
            output_formats=("audio/wav",),
            metadata={"engine": "kokoro", "sample_rate_hz": 24000},
        )

    def _get_pipeline(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        factory = self._pipeline_factory
        if factory is None:
            try:
                from kokoro import KPipeline
            except ImportError as error:
                raise SETTServiceAdapterError(
                    "The 'kokoro' package is required to use KokoroTTSAdapter. "
                    "SETT installs from source; from your checkout run: "
                    "pip install -e \".[kokoro]\"",
                    kind=AdapterErrorKind.CONFIGURATION,
                    adapter_name=type(self).__name__,
                    reason_code="adapter.dependency_missing",
                ) from error
            factory = KPipeline
        options: dict[str, Any] = {"lang_code": self._language_code}
        if self._device is not None:
            options["device"] = self._device
        self._pipeline = factory(**options)
        return self._pipeline

    def synthesize(self, text: str, **kwargs: Any) -> bytes:
        """Generate all Kokoro chunks and combine them into one WAV payload."""
        with adapter_call_scope(self, "synthesize"):
            lifecycle = pop_lifecycle_kwargs(kwargs)
            lifecycle.throw_if_cancelled()
            voice = str(kwargs.get("voice", self._voice))
            speed = float(kwargs.get("speed", 1.0))
            try:
                output = BytesIO()
                with wave.open(output, "wb") as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(24000)
                    for result in self._get_pipeline()(text, voice=voice, speed=speed):
                        lifecycle.throw_if_cancelled()
                        audio = getattr(result, "audio", result[2])
                        if audio is None:
                            continue
                        import torch

                        samples = audio.detach().cpu().clamp(-1, 1)
                        frames = (samples * 32767).to(dtype=torch.int16)
                        wav_file.writeframes(frames.numpy().tobytes())
                return output.getvalue()
            except SETTServiceAdapterError:
                raise
            except Exception as error:
                raise normalized_adapter_error(
                    error,
                    error_type=SETTServiceAdapterError,
                    provider="kokoro",
                    adapter_name=type(self).__name__,
                    operation="synthesize",
                ) from error

    def __repr__(self) -> str:
        return f"KokoroTTSAdapter(voice={self._voice!r})"
