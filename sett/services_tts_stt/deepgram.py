"""Deepgram pre-recorded speech-to-text adapter."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from sett.core_ruler.adapter import (
    AdapterCapabilities,
    AdapterErrorKind,
    AdapterKind,
    CapabilitySupport,
    ConnectivityRequirement,
    adapter_call_scope,
    normalized_adapter_error,
)
from sett.core_ruler.lifecycle import pop_lifecycle_kwargs, validate_duration_seconds
from sett.exceptions import SETTServiceAdapterError
from sett.services_tts_stt._http import (
    checked_request,
    credential,
    require_requests,
    response_json,
)
from sett.services_tts_stt.base import STTBase


class DeepgramSTTAdapter(STTBase):
    """Transcribe complete audio payloads through Deepgram's REST API."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "nova-3",
        language: str | None = None,
        endpoint: str = "https://api.deepgram.com/v1/listen",
        timeout_seconds: float = 120,
    ) -> None:
        """Configure a Deepgram pre-recorded request without contacting it."""
        require_requests(adapter_name=type(self).__name__, extra="deepgram")
        self._api_key = credential(
            api_key,
            "DEEPGRAM_API_KEY",
            provider="deepgram",
            adapter_name=type(self).__name__,
        )
        self._model = model.strip() or "nova-3"
        self._language = language.strip() if language else None
        self._endpoint = endpoint.rstrip("?")
        self._timeout_seconds = validate_duration_seconds(
            timeout_seconds, name="timeout_seconds"
        )

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Declare the synchronous pre-recorded transcription path."""
        return AdapterCapabilities(
            AdapterKind.STT,
            streaming=CapabilitySupport.NO,
            voice_cloning=CapabilitySupport.NO,
            partial_results=CapabilitySupport.NO,
            languages=None,
            connectivity=ConnectivityRequirement.REMOTE_SERVICE,
            input_formats=("audio/wav", "audio/mpeg", "audio/ogg", "audio/flac", "audio/webm"),
            output_formats=("text/plain",),
            metadata={"api_mode": "pre_recorded"},
        )

    def transcribe(self, audio: bytes, **kwargs: Any) -> str:
        """Transcribe one complete binary audio payload."""
        with adapter_call_scope(self, "transcribe"):
            lifecycle = pop_lifecycle_kwargs(kwargs)
            lifecycle.throw_if_cancelled()
            query: dict[str, str] = {
                "model": str(kwargs.get("model", self._model)),
                "smart_format": str(bool(kwargs.get("smart_format", True))).lower(),
            }
            language = kwargs.get("language", self._language)
            if language:
                query["language"] = str(language)
            content_type = str(kwargs.get("content_type", "audio/wav"))
            response = checked_request(
                "POST",
                f"{self._endpoint}?{urlencode(query)}",
                provider="deepgram",
                adapter_name=type(self).__name__,
                operation="transcribe",
                timeout=lifecycle.effective_timeout(self._timeout_seconds),
                headers={
                    "Authorization": f"Token {self._api_key}",
                    "Content-Type": content_type,
                },
                data=audio,
            )
            payload = response_json(
                response,
                provider="deepgram",
                adapter_name=type(self).__name__,
                operation="transcribe",
            )
            try:
                transcript = payload["results"]["channels"][0]["alternatives"][0][
                    "transcript"
                ]
            except (KeyError, IndexError, TypeError) as error:
                raise normalized_adapter_error(
                    error,
                    error_type=SETTServiceAdapterError,
                    provider="deepgram",
                    adapter_name=type(self).__name__,
                    operation="transcribe",
                    kind=AdapterErrorKind.INVALID_RESPONSE,
                ) from error
            lifecycle.throw_if_cancelled()
            return str(transcript)

    def __repr__(self) -> str:
        return f"DeepgramSTTAdapter(model={self._model!r})"
