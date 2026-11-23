"""OpenAI-hosted Whisper speech-to-text adapter."""
from __future__ import annotations

from typing import Any

from sett.core_ruler.adapter import (
    AdapterCapabilities,
    AdapterErrorKind,
    AdapterKind,
    CapabilitySupport,
    ConnectivityRequirement,
    adapter_call_scope,
    normalized_adapter_error,
    required_model,
)
from sett.core_ruler.lifecycle import pop_lifecycle_kwargs, validate_duration_seconds
from sett.exceptions import SETTServiceAdapterError
from sett.services_tts_stt._http import (
    checked_request,
    credential,
    require_requests,
    response_json,
    validated_endpoint,
)
from sett.services_tts_stt._language import resolve_language_code
from sett.services_tts_stt.base import STTBase


class WhisperSTTAdapter(STTBase):
    """Transcribe audio with the hosted OpenAI audio transcription API.

    ``language_code`` accepts a BCP-47 tag and forwards its primary subtag,
    which is the form the provider expects. ``language`` remains an alias.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str | None = None,
        endpoint: str = "https://api.openai.com/v1/audio/transcriptions",
        timeout_seconds: float = 120,
    ) -> None:
        """Configure hosted transcription without contacting OpenAI."""
        require_requests(adapter_name=type(self).__name__, extra="openai-audio")
        self._api_key = credential(
            api_key,
            "OPENAI_API_KEY",
            provider="openai",
            adapter_name=type(self).__name__,
        )
        self._model = required_model(
            model,
            error_type=SETTServiceAdapterError,
            provider="openai",
            adapter_name=type(self).__name__,
        )
        self._endpoint = validated_endpoint(
            endpoint, provider="openai", adapter_name=type(self).__name__
        )
        self._timeout_seconds = validate_duration_seconds(
            timeout_seconds, name="timeout_seconds"
        )

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Declare hosted complete-file transcription capabilities."""
        return AdapterCapabilities(
            AdapterKind.STT,
            streaming=CapabilitySupport.NO,
            voice_cloning=CapabilitySupport.NO,
            partial_results=CapabilitySupport.NO,
            connectivity=ConnectivityRequirement.REMOTE_SERVICE,
            input_formats=(
                "audio/flac",
                "audio/mpeg",
                "audio/mp4",
                "audio/ogg",
                "audio/wav",
                "audio/webm",
            ),
            output_formats=("text/plain",),
            metadata={"hosted": True},
        )

    def transcribe(self, audio: bytes, **kwargs: Any) -> str:
        """Upload one audio file and return its hosted transcription."""
        with adapter_call_scope(self, "transcribe"):
            lifecycle = pop_lifecycle_kwargs(kwargs)
            lifecycle.throw_if_cancelled()
            filename = str(kwargs.get("filename", "audio.wav"))
            content_type = str(kwargs.get("content_type", "audio/wav"))
            data = {"model": str(kwargs.get("model", self._model))}
            language_code = resolve_language_code(
                kwargs.pop("language_code", None),
                kwargs.pop("language", None),
                default=None,
                provider="openai",
                adapter_name=type(self).__name__,
                operation="transcribe",
                primary_only=True,
            )
            if language_code:
                data["language"] = language_code
            response = checked_request(
                "POST",
                self._endpoint,
                provider="openai",
                adapter_name=type(self).__name__,
                operation="transcribe",
                timeout=lifecycle.effective_timeout(self._timeout_seconds),
                headers={"Authorization": f"Bearer {self._api_key}"},
                data=data,
                files={"file": (filename, audio, content_type)},
            )
            payload = response_json(
                response,
                provider="openai",
                adapter_name=type(self).__name__,
                operation="transcribe",
            )
            if not isinstance(payload, dict) or "text" not in payload:
                error = KeyError("text")
                raise normalized_adapter_error(
                    error,
                    error_type=SETTServiceAdapterError,
                    provider="openai",
                    adapter_name=type(self).__name__,
                    operation="transcribe",
                    kind=AdapterErrorKind.INVALID_RESPONSE,
                ) from error
            lifecycle.throw_if_cancelled()
            return str(payload["text"])

    def __repr__(self) -> str:
        return f"WhisperSTTAdapter(model={self._model!r})"
