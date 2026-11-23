"""
SETT Framework: TTS/STT Base Adapters
==============================
Abstract interfaces that all text-to-speech and speech-to-text
adapters must implement.

Same intercambiability philosophy as LLMBase (sett/services_llm/base.py):
swap Google Cloud for ElevenLabs, or any future provider, by changing
the adapter: no other code needs to change. An Expert or Agent that
needs voice depends on TTSBase/STTBase, never on a specific provider's
SDK.

TTSBase and STTBase are deliberately two separate interfaces, not one
merged "voice" interface: a provider is free to implement only one of
them (ElevenLabs, historically TTS-only, implements TTSBase and has no
STTBase adapter; nothing about the interface forces it to pretend
otherwise).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from sett.core_ruler.adapter import (
    AdapterCapabilities,
    AdapterHealth,
    AdapterKind,
    adapter_health,
    record_adapter_event,
)


class TTSBase(ABC):
    """
    Abstract base class for all text-to-speech adapters in SETT.

    Implement one method:
    - synthesize(): text in, audio bytes out.

    Deliberately does NOT play audio, write files, or touch any UI:
    that is an application-layer concern, and mixing playback or UI
    updates into the adapter is exactly the coupling this interface
    avoids. Returning raw
    bytes keeps the adapter a pure function, testable without a
    speaker, a UI, or a filesystem.

    Example (minimal implementation):
        class MyTTSAdapter(TTSBase):
            @property
            def audio_format(self):
                return "mp3"

            def synthesize(self, text, **kwargs):
                return my_api.speak(text)
    
    Two reserved keyword arguments (v0.12.0)
    ----------------------------------------
    ``timeout`` (seconds) and ``cancellation_token`` are reserved by the
    framework inside ``**kwargs``. Adapters sit outside the Agent/Expert
    tree and never receive an ambient control, so these two names are how
    a run's remaining time and its cancellation reach a provider call.
    Unpack them with ``pop_lifecycle_kwargs(kwargs)`` from
    ``sett.core_ruler.lifecycle``; see ``LLMBase`` for a worked example.
    """

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Declared behavior; legacy adapters honestly default to UNKNOWN."""
        return AdapterCapabilities(AdapterKind.TTS)

    @property
    def health(self) -> AdapterHealth:
        """Most recent passive observation, initially UNKNOWN."""
        return adapter_health(self)

    def check_health(self) -> AdapterHealth:
        """Return current health without hidden or billable provider I/O."""
        health = self.health
        record_adapter_event(
            self,
            kind="adapter.health_checked",
            operation="check_health",
            status=health.status.value,
            reason_codes=health.reason_codes,
        )
        return health

    @abstractmethod
    def synthesize(self, text: str, **kwargs: Any) -> bytes:
        """
        Convert text to speech audio.

        Args:
            text: The text to synthesize.
            **kwargs: Adapter parameters. Official adapters that expose
                per-call BCP-47 selection use ``language_code``; other
                options such as voice and stability remain provider-specific.

        Returns:
            Raw audio bytes, encoded as `audio_format`.
        """

    @property
    @abstractmethod
    def audio_format(self) -> str:
        """Return the audio encoding this adapter produces (e.g. 'mp3', 'wav')."""


class STTBase(ABC):
    """
    Abstract base class for all speech-to-text adapters in SETT.

    Implement one method:
    - transcribe(): audio bytes in, text out.

    Deliberately does not own a microphone, a listening loop, or any
    concurrency guard. A lock against overlapping listens is worth
    having, but it is orchestration, not transcription, so it belongs in
    the application or Expert layer that calls this adapter, not inside
    it.

    Example (minimal implementation):
        class MySTTAdapter(STTBase):
            def transcribe(self, audio, **kwargs):
                return my_api.transcribe(audio)
    
    Two reserved keyword arguments (v0.12.0)
    ----------------------------------------
    ``timeout`` (seconds) and ``cancellation_token`` are reserved by the
    framework inside ``**kwargs``. Adapters sit outside the Agent/Expert
    tree and never receive an ambient control, so these two names are how
    a run's remaining time and its cancellation reach a provider call.
    Unpack them with ``pop_lifecycle_kwargs(kwargs)`` from
    ``sett.core_ruler.lifecycle``; see ``LLMBase`` for a worked example.
    """

    @abstractmethod
    def transcribe(self, audio: bytes, **kwargs: Any) -> str:
        """
        Convert speech audio to text.

        Args:
            audio: Raw audio bytes (encoding expected by the specific
                adapter: check its docstring, e.g. LINEAR16 WAV for
                GoogleSTTAdapter).
            **kwargs: Adapter parameters. Official adapters that expose
                language selection use the provider-neutral ``language_code``
                spelling; other options remain provider-specific.

        Returns:
            The transcribed text. Empty string if nothing was
            recognized: adapters raise SETTServiceAdapterError only
            for actual failures (network, auth, malformed audio), not
            for silence or unrecognized speech.
        """

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Declared behavior; legacy adapters honestly default to UNKNOWN."""
        return AdapterCapabilities(AdapterKind.STT)

    @property
    def health(self) -> AdapterHealth:
        """Most recent passive observation, initially UNKNOWN."""
        return adapter_health(self)

    def check_health(self) -> AdapterHealth:
        """Return current health without hidden or billable provider I/O."""
        health = self.health
        record_adapter_event(
            self,
            kind="adapter.health_checked",
            operation="check_health",
            status=health.status.value,
            reason_codes=health.reason_codes,
        )
        return health
