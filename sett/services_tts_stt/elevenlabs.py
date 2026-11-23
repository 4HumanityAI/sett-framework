"""
SETT Framework: ElevenLabs TTS Adapter
==============================
Text-to-speech adapter for ElevenLabs (https://elevenlabs.io).

Implements TTSBase: fully interchangeable with GoogleTTSAdapter or
any other TTS provider. ElevenLabs has no STT product, so there is no
ElevenLabsSTTAdapter and none is implied by this module: see
services_tts_stt/base.py's docstring on why TTSBase/STTBase are
separate interfaces.

Talks to ElevenLabs' REST API directly via `requests`: no ElevenLabs
SDK dependency, same reasoning as OllamaAdapter using only stdlib for
Ollama's local API (services_llm/ollama.py).

It makes only the ElevenLabs HTTP call: engine selection, local
playback and output-file pruning are application-layer concerns and do
not belong in a stateless adapter.

Requires, from a source checkout: pip install -e ".[elevenlabs]"
"""
from __future__ import annotations

import os
import re
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
from sett.core_ruler.lifecycle import (
    pop_lifecycle_kwargs,
    validate_duration_seconds,
)
from sett.exceptions import (
    DEFAULT_PROVIDER_ERROR_MAX_LENGTH,
    SETTServiceAdapterError,
)
from sett.services_tts_stt._http import path_identifier
from sett.services_tts_stt.base import TTSBase

try:
    import requests
except ImportError:
    requests = None

# ElevenLabs names its MP3 formats mp3_<sample rate>_<bitrate>. The pattern
# accepts the family without listing values: availability is the provider's.
_MP3_FORMAT = re.compile(r"mp3_[0-9]+_[0-9]+")


class ElevenLabsTTSAdapter(TTSBase):
    """
    Text-to-speech adapter for ElevenLabs.

    Usage:
        from sett.services_tts_stt.elevenlabs import ElevenLabsTTSAdapter

        tts = ElevenLabsTTSAdapter(
            api_key="your-key",
            model_id="<model identifier you chose>",
        )
        audio_bytes = tts.synthesize("Hi, how can I help you?")
    """

    DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"
    DEFAULT_STABILITY = 0.5
    DEFAULT_SIMILARITY_BOOST = 0.75
    DEFAULT_STYLE = 0.35
    DEFAULT_TIMEOUT_SECONDS = 60

    def __init__(
        self,
        api_key: str | None = None,
        voice_id: str = DEFAULT_VOICE_ID,
        model_id: str | None = None,
        stability: float = DEFAULT_STABILITY,
        similarity_boost: float = DEFAULT_SIMILARITY_BOOST,
        style: float = DEFAULT_STYLE,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        output_format: str | None = None,
    ) -> None:
        """
        Args:
            api_key: ElevenLabs API key. Falls back to the
                ELEVENLABS_API_KEY environment variable.
            voice_id: ElevenLabs voice ID. Defaults to a stock voice.
            model_id: ElevenLabs model ID. Required; there is no
                default.
            stability: Voice stability (0.0 to 1.0).
            similarity_boost: Voice similarity boost (0.0 to 1.0).
            style: Style exaggeration (0.0 to 1.0).
            timeout_seconds: HTTP request timeout.
            output_format: An ElevenLabs MP3 format identifier, such as
                ``mp3_44100_128``, sent as the provider's ``output_format``
                query parameter. Only MP3-family identifiers
                (``mp3_<sample rate>_<bitrate>``) are accepted, so the
                adapter's ``mp3`` container and ``audio/mpeg`` capability
                stay true. ``None`` sends no parameter and leaves the
                request as it was; the provider then uses its own default.
                Which identifiers an account may use is the provider's
                rule, not SETT's.
        """
        if requests is None:
            raise SETTServiceAdapterError(
                "The 'requests' package is required to use "
                "ElevenLabsTTSAdapter. SETT installs from source; from your "
                "checkout run: pip install -e \".[elevenlabs]\"",
                kind=AdapterErrorKind.CONFIGURATION,
                provider="elevenlabs",
                adapter_name=type(self).__name__,
                reason_code="adapter.dependency_missing",
            )

        # Strip both explicit and environment-sourced credentials so whitespace
        # is rejected locally as configuration, before a provider request.
        self._api_key = (api_key or os.getenv("ELEVENLABS_API_KEY", "")).strip()
        if not self._api_key:
            raise SETTServiceAdapterError(
                "ElevenLabs API key not found. "
                "Pass api_key= or set the ELEVENLABS_API_KEY environment variable.",
                kind=AdapterErrorKind.CONFIGURATION,
                provider="elevenlabs",
                adapter_name=type(self).__name__,
                reason_code="adapter.credentials_missing",
            )

        self._voice_id = path_identifier(
            voice_id,
            provider="elevenlabs",
            adapter_name=type(self).__name__,
            parameter="voice_id",
        )
        self._model_id = required_model(
            model_id,
            error_type=SETTServiceAdapterError,
            provider="elevenlabs",
            adapter_name=type(self).__name__,
            parameter="model_id",
        )
        self._stability = min(1.0, max(0.0, stability))
        self._similarity_boost = min(1.0, max(0.0, similarity_boost))
        self._style = min(1.0, max(0.0, style))
        self._timeout_seconds = validate_duration_seconds(
            timeout_seconds, name="timeout_seconds"
        )
        if output_format is not None and (
            not isinstance(output_format, str)
            or _MP3_FORMAT.fullmatch(output_format) is None
        ):
            raise SETTServiceAdapterError(
                "ElevenLabs output_format must be an MP3 format identifier "
                "such as 'mp3_44100_128', or None.",
                kind=AdapterErrorKind.CONFIGURATION,
                provider="elevenlabs",
                adapter_name=type(self).__name__,
                reason_code="adapter.output_format_invalid",
            )
        self._output_format = output_format

    @property
    def audio_format(self) -> str:
        """The container this adapter returns audio in."""
        return "mp3"

    @property
    def capabilities(self) -> AdapterCapabilities:
        """The configured custom voice path implemented by this adapter."""
        return AdapterCapabilities(
            AdapterKind.TTS,
            streaming=CapabilitySupport.NO,
            voice_cloning=CapabilitySupport.YES,
            partial_results=CapabilitySupport.NO,
            connectivity=ConnectivityRequirement.REMOTE_SERVICE,
            input_formats=("text",),
            output_formats=("audio/mpeg",),
            metadata={"custom_voice_id": True},
        )

    def synthesize(self, text: str, **kwargs: Any) -> bytes:
        """Synthesize speech audio (MP3 bytes) for the given text."""
        with adapter_call_scope(self, "synthesize"):
            lifecycle = pop_lifecycle_kwargs(kwargs)
            lifecycle.throw_if_cancelled()
            # Checked before the URL exists: a per-call identifier must
            # not be able to change the path, add a query or override the
            # configured output format.
            voice_id = path_identifier(
                kwargs.get("voice_id", self._voice_id),
                provider="elevenlabs",
                adapter_name=type(self).__name__,
                parameter="voice_id",
            )
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
            headers = {
                "accept": "audio/mpeg",
                "xi-api-key": self._api_key,
                "content-type": "application/json",
            }
            payload = {
                "text": text,
                "model_id": kwargs.get("model_id", self._model_id),
                "voice_settings": {
                    "stability": kwargs.get("stability", self._stability),
                    "similarity_boost": kwargs.get(
                        "similarity_boost", self._similarity_boost
                    ),
                    "style": kwargs.get("style", self._style),
                    "use_speaker_boost": kwargs.get("use_speaker_boost", True),
                },
            }

            # The format is fixed per instance, so the declared capability
            # stays true; with no configured format the request is unchanged.
            request_options: dict[str, Any] = {}
            if self._output_format is not None:
                request_options["params"] = {"output_format": self._output_format}
            try:
                response = requests.post(
                    url,
                    allow_redirects=False,
                    headers=headers,
                    json=payload,
                    timeout=lifecycle.effective_timeout(self._timeout_seconds),
                    **request_options,
                )
            except requests.exceptions.RequestException as error:
                raise normalized_adapter_error(
                    error,
                    error_type=SETTServiceAdapterError,
                    provider="ElevenLabs",
                    adapter_name=type(self).__name__,
                    operation="synthesize",
                    kind=AdapterErrorKind.UNAVAILABLE,
                ) from error

            if response.status_code != 200:
                body = response.text
                if len(body) > DEFAULT_PROVIDER_ERROR_MAX_LENGTH:
                    body = body[:DEFAULT_PROVIDER_ERROR_MAX_LENGTH] + "... [truncated]"
                error = RuntimeError(body)
                error.status_code = response.status_code  # type: ignore[attr-defined]
                raise normalized_adapter_error(
                    error,
                    error_type=SETTServiceAdapterError,
                    provider="ElevenLabs",
                    adapter_name=type(self).__name__,
                    operation="synthesize",
                    status_code=response.status_code,
                ) from error

            lifecycle.throw_if_cancelled()
            return response.content

    def __repr__(self) -> str:
        return f"ElevenLabsTTSAdapter(voice_id={self._voice_id!r})"
