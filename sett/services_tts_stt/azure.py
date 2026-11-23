"""Azure Speech text-to-speech and short-audio transcription adapters."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode
from xml.sax.saxutils import escape, quoteattr

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
    host_label,
    require_requests,
    response_json,
    validated_endpoint,
)
from sett.services_tts_stt._language import resolve_language_code
from sett.services_tts_stt.base import STTBase, TTSBase


def _speech_key(value: str | None, adapter_name: str) -> str:
    return credential(
        value,
        "AZURE_SPEECH_KEY",
        provider="azure_speech",
        adapter_name=adapter_name,
    )


def _region(value: str | None, adapter_name: str) -> str:
    """Resolve the region and require it to be one host label.

    The region is interpolated into the host name the subscription key is
    sent to. A value that is not a single label could name another host,
    so it is refused here instead of being sent anywhere. A different
    host is configured deliberately through ``endpoint``.
    """
    return host_label(
        credential(
            value,
            "AZURE_SPEECH_REGION",
            provider="azure_speech",
            adapter_name=adapter_name,
        ),
        provider="azure_speech",
        adapter_name=adapter_name,
        parameter="region",
    )


def _endpoint(value: str | None, default: str, adapter_name: str) -> str:
    if not value:
        return default
    return validated_endpoint(
        value, provider="azure_speech", adapter_name=adapter_name
    )


def _audio_container(output_format: str) -> str:
    """Classify provider format families without claiming codec validation."""
    if isinstance(output_format, str) and not any(
        ord(char) <= 32 or ord(char) == 127 for char in output_format
    ):
        if output_format.startswith("audio-") and output_format.endswith("-mp3"):
            return "mp3"
        for prefix, container in (("riff-", "wav"), ("ogg-", "ogg"), ("webm-", "webm"), ("raw-", "pcm")):
            if output_format.startswith(prefix):
                return container
    raise SETTServiceAdapterError(
        "Azure output_format must name a supported audio container family.",
        kind=AdapterErrorKind.CONFIGURATION,
        provider="azure_speech", reason_code="adapter.output_format_invalid",
    )


class AzureTTSAdapter(TTSBase):
    """Synthesize a complete SSML request through Azure Speech.

    ``language_code`` is the canonical BCP-47 argument. The historical
    ``language`` spelling remains a compatibility alias at construction and
    per call.
    """

    def __init__(
        self,
        api_key: str | None = None,
        region: str | None = None,
        *,
        voice: str = "en-US-AvaMultilingualNeural",
        language_code: str | None = None,
        language: str | None = None,
        output_format: str = "audio-24khz-48kbitrate-mono-mp3",
        endpoint: str | None = None,
        timeout_seconds: float = 60,
    ) -> None:
        """Configure an Azure Speech synthesis resource without contacting it."""
        require_requests(adapter_name=type(self).__name__, extra="azure-speech")
        self._api_key = _speech_key(api_key, type(self).__name__)
        self._region = _region(region, type(self).__name__)
        self._voice = voice
        self._language_code = resolve_language_code(
            language_code,
            language,
            default="en-US",
            provider="azure_speech",
            adapter_name=type(self).__name__,
            operation="configure",
        )
        self._audio_format = _audio_container(output_format)
        self._output_format = output_format
        self._endpoint = _endpoint(
            endpoint,
            f"https://{self._region}.tts.speech.microsoft.com/cognitiveservices/v1",
            type(self).__name__,
        )
        self._timeout_seconds = validate_duration_seconds(
            timeout_seconds, name="timeout_seconds"
        )

    @property
    def audio_format(self) -> str:
        """Return the container implied by the configured Azure output format."""
        return self._audio_format

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Declare the synchronous Azure SSML implementation capabilities."""
        return AdapterCapabilities(
            AdapterKind.TTS,
            streaming=CapabilitySupport.NO,
            voice_cloning=CapabilitySupport.NO,
            partial_results=CapabilitySupport.NO,
            connectivity=ConnectivityRequirement.REMOTE_SERVICE,
            input_formats=("text/plain",),
            output_formats=({
                "mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg",
                "webm": "audio/webm", "pcm": "audio/pcm",
            }[self.audio_format],),
            metadata={"request_format": "ssml"},
        )

    def synthesize(self, text: str, **kwargs: Any) -> bytes:
        """Synthesize escaped text and return the configured audio bytes."""
        with adapter_call_scope(self, "synthesize"):
            lifecycle = pop_lifecycle_kwargs(kwargs)
            lifecycle.throw_if_cancelled()
            voice = str(kwargs.get("voice", self._voice))
            language_code = resolve_language_code(
                kwargs.pop("language_code", None),
                kwargs.pop("language", None),
                default=self._language_code,
                provider="azure_speech",
                adapter_name=type(self).__name__,
                operation="synthesize",
            )
            output_format = kwargs.get("output_format", self._output_format)
            if _audio_container(output_format) != self.audio_format:
                raise SETTServiceAdapterError(
                    "A per-call output_format cannot change the configured container.",
                    kind=AdapterErrorKind.CONFIGURATION,
                    provider="azure_speech", reason_code="adapter.output_format_invalid",
                )
            ssml = (
                f"<speak version='1.0' xml:lang={quoteattr(language_code)}>"
                f"<voice name={quoteattr(voice)}>{escape(text)}</voice></speak>"
            )
            response = checked_request(
                "POST",
                self._endpoint,
                provider="azure_speech",
                adapter_name=type(self).__name__,
                operation="synthesize",
                timeout=lifecycle.effective_timeout(self._timeout_seconds),
                headers={
                    "Ocp-Apim-Subscription-Key": self._api_key,
                    "Content-Type": "application/ssml+xml",
                    "X-Microsoft-OutputFormat": output_format,
                    "User-Agent": "sett-framework",
                },
                data=ssml.encode("utf-8"),
            )
            lifecycle.throw_if_cancelled()
            return response.content

    def __repr__(self) -> str:
        return f"AzureTTSAdapter(region={self._region!r}, voice={self._voice!r})"


class AzureSTTAdapter(STTBase):
    """Transcribe Azure-compatible WAV or OGG short audio.

    ``language_code`` is the canonical BCP-47 argument. The historical
    ``language`` spelling remains a compatibility alias at construction and
    per call.
    """

    def __init__(
        self,
        api_key: str | None = None,
        region: str | None = None,
        *,
        language_code: str | None = None,
        language: str | None = None,
        endpoint: str | None = None,
        timeout_seconds: float = 120,
    ) -> None:
        """Configure an Azure short-audio resource without contacting it."""
        require_requests(adapter_name=type(self).__name__, extra="azure-speech")
        self._api_key = _speech_key(api_key, type(self).__name__)
        self._region = _region(region, type(self).__name__)
        self._language_code = resolve_language_code(
            language_code,
            language,
            default="en-US",
            provider="azure_speech",
            adapter_name=type(self).__name__,
            operation="configure",
        )
        self._endpoint = _endpoint(
            endpoint,
            f"https://{self._region}.stt.speech.microsoft.com/"
            "speech/recognition/conversation/cognitiveservices/v1",
            type(self).__name__,
        )
        self._timeout_seconds = validate_duration_seconds(
            timeout_seconds, name="timeout_seconds"
        )

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Declare the synchronous Azure short-audio implementation."""
        return AdapterCapabilities(
            AdapterKind.STT,
            streaming=CapabilitySupport.NO,
            voice_cloning=CapabilitySupport.NO,
            partial_results=CapabilitySupport.NO,
            connectivity=ConnectivityRequirement.REMOTE_SERVICE,
            input_formats=("audio/wav", "audio/ogg"),
            output_formats=("text/plain",),
            metadata={"api_mode": "short_audio"},
        )

    def transcribe(self, audio: bytes, **kwargs: Any) -> str:
        """Transcribe one Azure-compatible short-audio payload."""
        with adapter_call_scope(self, "transcribe"):
            lifecycle = pop_lifecycle_kwargs(kwargs)
            lifecycle.throw_if_cancelled()
            language_code = resolve_language_code(
                kwargs.pop("language_code", None),
                kwargs.pop("language", None),
                default=self._language_code,
                provider="azure_speech",
                adapter_name=type(self).__name__,
                operation="transcribe",
            )
            url = f"{self._endpoint}?{urlencode({'language': language_code})}"
            response = checked_request(
                "POST",
                url,
                provider="azure_speech",
                adapter_name=type(self).__name__,
                operation="transcribe",
                timeout=lifecycle.effective_timeout(self._timeout_seconds),
                headers={
                    "Ocp-Apim-Subscription-Key": self._api_key,
                    "Content-Type": str(kwargs.get("content_type", "audio/wav")),
                    "Accept": "application/json",
                },
                data=audio,
            )
            payload = response_json(
                response,
                provider="azure_speech",
                adapter_name=type(self).__name__,
                operation="transcribe",
            )
            if not isinstance(payload, dict):
                error = TypeError("Azure Speech returned a non-object response")
                raise normalized_adapter_error(
                    error,
                    error_type=SETTServiceAdapterError,
                    provider="azure_speech",
                    adapter_name=type(self).__name__,
                    operation="transcribe",
                    kind=AdapterErrorKind.INVALID_RESPONSE,
                ) from error
            lifecycle.throw_if_cancelled()
            return str(payload.get("DisplayText", payload.get("Text", "")))

    def __repr__(self) -> str:
        return (
            f"AzureSTTAdapter(region={self._region!r}, "
            f"language_code={self._language_code!r})"
        )
