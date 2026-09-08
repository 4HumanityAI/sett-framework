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
from sett.exceptions import (
    SETTCancelledError,
    SETTServiceAdapterError,
    SETTTimeoutError,
)
from sett.services_tts_stt._language import resolve_language_code
from sett.services_tts_stt.base import STTBase, TTSBase


def _dependency_error(adapter_name: str, dependency: str, extra: str) -> SETTServiceAdapterError:
    return SETTServiceAdapterError(
        f"The '{dependency}' package is required to use {adapter_name}. "
        "SETT installs from source; from your checkout run: "
        f"pip install -e \".[{extra}]\"",
        kind=AdapterErrorKind.CONFIGURATION,
        adapter_name=adapter_name,
        reason_code="adapter.dependency_missing",
    )


def _voice_id_for_language(engine: Any, language_code: str) -> str | None:
    """Best-effort match of an installed host voice to a BCP-47 tag.

    Reads only each voice's own declared ``languages`` metadata, compared
    by primary subtag (``es-AR`` against a voice reporting ``es-MX``
    matches; ``es`` against ``en-US`` does not). Deliberately does not
    fall back to searching voice id or name text: those are free-form
    strings (a Windows SAPI5 id is a full registry path such as
    ``...\\Speech\\Voices\\Tokens\\...``), and a substring search over them
    can match generic words that happen to contain a language code
    (``es`` inside ``Voices``) regardless of the voice's actual language.
    That would pick a wrong voice with no error and no signal that
    anything went wrong, which is worse than picking none. Returns
    ``None``, never a guess, when no voice's own metadata names the
    requested language; the caller keeps whichever voice was already
    selected.
    """
    primary_subtag = str(language_code).strip().replace("_", "-").split("-")[0].lower()
    for voice in engine.getProperty("voices") or ():
        for tag in getattr(voice, "languages", None) or ():
            tag_text = tag.decode("utf-8", "ignore") if isinstance(tag, bytes) else str(tag)
            normalized_tag = tag_text.lstrip("".join(chr(code) for code in range(32)))
            normalized_tag = normalized_tag.strip().replace("_", "-").lower()
            if normalized_tag.split("-")[0] == primary_subtag:
                return voice.id
    return None


class LocalTTSAdapter(TTSBase):
    """Generate WAV bytes through the host's pyttsx3 speech engine.

    synthesize() accepts an optional ``language_code`` keyword argument,
    the same name GoogleTTSAdapter already uses. An explicit ``voice_id``
    always takes precedence; without one, the adapter looks for an
    installed host voice whose own metadata names that language (see
    ``_voice_id_for_language()``) and falls back to whatever voice is
    already selected when none does, rather than guessing.
    """

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
            path: Path | None = None
            try:
                engine = self._new_engine()
                voice_id = kwargs.get("voice_id", self._voice_id)
                language_code = kwargs.get("language_code")
                if voice_id is None and language_code:
                    voice_id = _voice_id_for_language(engine, language_code)
                rate = kwargs.get("rate", self._rate)
                volume = kwargs.get("volume", self._volume)
                if voice_id is not None:
                    engine.setProperty("voice", voice_id)
                if rate is not None:
                    engine.setProperty("rate", rate)
                if volume is not None:
                    engine.setProperty("volume", volume)
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as output:
                    path = Path(output.name)
                engine.save_to_file(text, str(path))
                engine.runAndWait()
                lifecycle.throw_if_cancelled()
                return path.read_bytes()
            except (SETTCancelledError, SETTServiceAdapterError, SETTTimeoutError):
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
    """Transcribe a complete local audio file through faster-whisper.

    transcribe() accepts an optional ``language_code`` keyword argument,
    matching GoogleSTTAdapter and AssemblyAISTTAdapter. The historical
    ``language`` name remains a compatibility alias. faster-whisper only
    recognizes bare language identifiers (``es``, not ``es-AR``), so either
    spelling is reduced to its primary subtag before the provider call.
    """

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
                normalized_language = resolve_language_code(
                    kwargs.pop("language_code", None),
                    kwargs.pop("language", None),
                    default=None,
                    provider="faster-whisper",
                    adapter_name=type(self).__name__,
                    operation="transcribe",
                    primary_only=True,
                    lowercase=True,
                )
                transcribe_kwargs: dict[str, Any] = {
                    "beam_size": int(kwargs.pop("beam_size", 5)),
                }
                if normalized_language is not None:
                    transcribe_kwargs["language"] = normalized_language
                transcribe_kwargs.update(kwargs)
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as source:
                    source.write(audio)
                    path = Path(source.name)
                model = self._get_model()
                segments, _ = model.transcribe(str(path), **transcribe_kwargs)
                parts: list[str] = []
                for segment in segments:
                    lifecycle.throw_if_cancelled()
                    parts.append(str(segment.text).strip())
                return " ".join(part for part in parts if part)
            except (SETTCancelledError, SETTServiceAdapterError, SETTTimeoutError):
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
