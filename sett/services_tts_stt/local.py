"""Local pyttsx3 TTS and faster-whisper STT adapters."""
from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
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
from sett.services_tts_stt.base import STTBase, TTSBase


def _dependency_error(adapter_name: str, dependency: str, extra: str) -> SETTServiceAdapterError:
    return SETTServiceAdapterError(
        f"The '{dependency}' package is required to use {adapter_name}. "
        f"Install it with: pip install sett-framework[{extra}]",
        kind=AdapterErrorKind.CONFIGURATION,
        adapter_name=adapter_name,
        reason_code="adapter.dependency_missing",
    )


class LocalTTSAdapter(TTSBase):
    """Generate WAV bytes through the host's pyttsx3 speech engine."""

    def __init__(
        self,
        *,
        voice_id: str | None = None,
        rate: int | None = None,
        volume: float | None = None,
        engine_factory: Callable[[], Any] | None = None,
    ) -> None:
        """Configure host voice properties and an optional engine factory."""
        self._voice_id = voice_id
        self._rate = rate
        self._volume = volume
        self._engine_factory = engine_factory

    @property
    def audio_format(self) -> str:
        """Return the WAV container used for host-engine file output."""
        return "wav"

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Declare local synthesis through installed host voices."""
        return AdapterCapabilities(
            AdapterKind.TTS,
            streaming=CapabilitySupport.NO,
            voice_cloning=CapabilitySupport.NO,
            partial_results=CapabilitySupport.NO,
            languages=None,
            connectivity=ConnectivityRequirement.NONE,
            input_formats=("text/plain",),
            output_formats=("audio/wav",),
            metadata={"engine": "pyttsx3", "uses_host_voices": True},
        )

    def _new_engine(self) -> Any:
        if self._engine_factory is not None:
            return self._engine_factory()
        try:
            import pyttsx3
        except ImportError as error:
            raise _dependency_error(type(self).__name__, "pyttsx3", "local-tts") from error
        return pyttsx3.init()

    def synthesize(self, text: str, **kwargs: Any) -> bytes:
        """Generate a temporary WAV and return its bytes without playback."""
        with adapter_call_scope(self, "synthesize"):
            lifecycle = pop_lifecycle_kwargs(kwargs)
            lifecycle.throw_if_cancelled()
            engine = self._new_engine()
            voice_id = kwargs.get("voice_id", self._voice_id)
            rate = kwargs.get("rate", self._rate)
            volume = kwargs.get("volume", self._volume)
            if voice_id is not None:
                engine.setProperty("voice", voice_id)
            if rate is not None:
                engine.setProperty("rate", rate)
            if volume is not None:
                engine.setProperty("volume", volume)
            path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as output:
                    path = Path(output.name)
                engine.save_to_file(text, str(path))
                engine.runAndWait()
                lifecycle.throw_if_cancelled()
                return path.read_bytes()
            except SETTServiceAdapterError:
                raise
            except Exception as error:
                raise normalized_adapter_error(
                    error,
                    error_type=SETTServiceAdapterError,
                    provider="pyttsx3",
                    adapter_name=type(self).__name__,
                    operation="synthesize",
                ) from error
            finally:
                if path is not None:
                    path.unlink(missing_ok=True)

    def __repr__(self) -> str:
        return f"LocalTTSAdapter(voice_id={self._voice_id!r})"


class LocalSTTAdapter(STTBase):
    """Transcribe a complete local audio file through faster-whisper."""

    def __init__(
        self,
        *,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        model_factory: Callable[..., Any] | None = None,
    ) -> None:
        """Configure a faster-whisper model while deferring model loading."""
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model_factory = model_factory
        self._model: Any = None

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Declare local complete-file transcription capabilities."""
        return AdapterCapabilities(
            AdapterKind.STT,
            streaming=CapabilitySupport.NO,
            voice_cloning=CapabilitySupport.NO,
            partial_results=CapabilitySupport.NO,
            languages=None,
            connectivity=ConnectivityRequirement.NONE,
            input_formats=("audio/*", "video/*"),
            output_formats=("text/plain",),
            metadata={"engine": "faster-whisper", "model_loaded_lazily": True},
        )

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        factory = self._model_factory
        if factory is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as error:
                raise _dependency_error(
                    type(self).__name__, "faster-whisper", "local-stt"
                ) from error
            factory = WhisperModel
        self._model = factory(
            self._model_size,
            device=self._device,
            compute_type=self._compute_type,
        )
        return self._model

    def transcribe(self, audio: bytes, **kwargs: Any) -> str:
        """Transcribe a temporary local audio file and join its segments."""
        with adapter_call_scope(self, "transcribe"):
            lifecycle = pop_lifecycle_kwargs(kwargs)
            lifecycle.throw_if_cancelled()
            suffix = str(kwargs.pop("suffix", ".wav"))
            path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as source:
                    source.write(audio)
                    path = Path(source.name)
                model = self._get_model()
                transcribe_kwargs: dict[str, Any] = {
                    "beam_size": int(kwargs.pop("beam_size", 5)),
                }
                language = kwargs.pop("language", None)
                if language:
                    transcribe_kwargs["language"] = language
                transcribe_kwargs.update(kwargs)
                segments, _ = model.transcribe(str(path), **transcribe_kwargs)
                parts: list[str] = []
                for segment in segments:
                    lifecycle.throw_if_cancelled()
                    parts.append(str(segment.text).strip())
                return " ".join(part for part in parts if part)
            except SETTServiceAdapterError:
                raise
            except Exception as error:
                raise normalized_adapter_error(
                    error,
                    error_type=SETTServiceAdapterError,
                    provider="faster-whisper",
                    adapter_name=type(self).__name__,
                    operation="transcribe",
                ) from error
            finally:
                if path is not None:
                    path.unlink(missing_ok=True)

    def __repr__(self) -> str:
        return f"LocalSTTAdapter(model_size={self._model_size!r}, device={self._device!r})"


PyttsxTTSAdapter = LocalTTSAdapter
FasterWhisperSTTAdapter = LocalSTTAdapter
