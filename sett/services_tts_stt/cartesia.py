"""Cartesia text-to-speech adapter."""
from __future__ import annotations

from typing import Any

from sett.core_ruler.adapter import (
    AdapterCapabilities,
    AdapterKind,
    CapabilitySupport,
    ConnectivityRequirement,
    adapter_call_scope,
)
from sett.core_ruler.lifecycle import pop_lifecycle_kwargs, validate_duration_seconds
from sett.services_tts_stt._http import checked_request, credential, require_requests
from sett.services_tts_stt.base import TTSBase


class CartesiaTTSAdapter(TTSBase):
    """Synthesize complete transcripts through Cartesia's bytes endpoint."""

    def __init__(
        self,
        voice_id: str,
        api_key: str | None = None,
        *,
        model: str = "sonic-3",
        api_version: str = "2026-03-01",
        endpoint: str = "https://api.cartesia.ai/tts/bytes",
        timeout_seconds: float = 60,
    ) -> None:
        """Configure a voice ID and Cartesia API version without network I/O."""
        require_requests(adapter_name=type(self).__name__, extra="cartesia")
        self._api_key = credential(
            api_key,
            "CARTESIA_API_KEY",
            provider="cartesia",
            adapter_name=type(self).__name__,
        )
        self._voice_id = voice_id.strip()
        if not self._voice_id:
            raise ValueError("voice_id must be a non-empty string")
        self._model = model.strip() or "sonic-3"
        self._api_version = api_version
        self._endpoint = endpoint
        self._timeout_seconds = validate_duration_seconds(
            timeout_seconds, name="timeout_seconds"
        )

    @property
    def audio_format(self) -> str:
        """Return the MP3 container selected by this implementation."""
        return "mp3"

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Declare batch synthesis and custom voice-ID support."""
        return AdapterCapabilities(
            AdapterKind.TTS,
            streaming=CapabilitySupport.NO,
            voice_cloning=CapabilitySupport.YES,
            partial_results=CapabilitySupport.NO,
            connectivity=ConnectivityRequirement.REMOTE_SERVICE,
            input_formats=("text/plain",),
            output_formats=("audio/mpeg",),
            metadata={"custom_voice_id": True},
        )

    def synthesize(self, text: str, **kwargs: Any) -> bytes:
        """Synthesize one complete transcript and return MP3 bytes."""
        with adapter_call_scope(self, "synthesize"):
            lifecycle = pop_lifecycle_kwargs(kwargs)
            lifecycle.throw_if_cancelled()
            payload: dict[str, Any] = {
                "model_id": str(kwargs.get("model", self._model)),
                "transcript": text,
                "voice": {"id": str(kwargs.get("voice_id", self._voice_id))},
                "output_format": {
                    "container": "mp3",
                    "bit_rate": int(kwargs.get("bit_rate", 128000)),
                    "sample_rate": int(kwargs.get("sample_rate", 44100)),
                },
            }
            if kwargs.get("language"):
                payload["language"] = str(kwargs["language"])
            response = checked_request(
                "POST",
                self._endpoint,
                provider="cartesia",
                adapter_name=type(self).__name__,
                operation="synthesize",
                timeout=lifecycle.effective_timeout(self._timeout_seconds),
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Cartesia-Version": self._api_version,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            lifecycle.throw_if_cancelled()
            return response.content

    def __repr__(self) -> str:
        return f"CartesiaTTSAdapter(model={self._model!r}, voice_id={self._voice_id!r})"
