"""Gladia v2 asynchronous speech-to-text adapter."""
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
)
from sett.core_ruler.lifecycle import pop_lifecycle_kwargs, validate_duration_seconds
from sett.exceptions import SETTServiceAdapterError
from sett.services_tts_stt._http import (
    checked_request,
    credential,
    poll_json,
    require_requests,
    response_json,
)
from sett.services_tts_stt.base import STTBase


class GladiaSTTAdapter(STTBase):
    """Upload, submit, and poll one Gladia v2 pre-recorded job."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = "https://api.gladia.io",
        timeout_seconds: float = 300,
        poll_interval_seconds: float = 1,
    ) -> None:
        """Configure credentials, endpoint, and bounded polling behavior."""
        require_requests(adapter_name=type(self).__name__, extra="gladia")
        self._api_key = credential(
            api_key,
            "GLADIA_API_KEY",
            provider="gladia",
            adapter_name=type(self).__name__,
        )
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = validate_duration_seconds(
            timeout_seconds, name="timeout_seconds"
        )
        self._poll_interval = validate_duration_seconds(
            poll_interval_seconds, name="poll_interval_seconds"
        )

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Declare the complete Gladia v2 pre-recorded workflow."""
        return AdapterCapabilities(
            AdapterKind.STT,
            streaming=CapabilitySupport.NO,
            voice_cloning=CapabilitySupport.NO,
            partial_results=CapabilitySupport.NO,
            connectivity=ConnectivityRequirement.REMOTE_SERVICE,
            input_formats=("audio/*", "video/*"),
            output_formats=("text/plain",),
            metadata={"api_mode": "pre_recorded_v2"},
        )

    def transcribe(self, audio: bytes, **kwargs: Any) -> str:
        """Upload audio and return text after the remote job completes."""
        with adapter_call_scope(self, "transcribe"):
            lifecycle = pop_lifecycle_kwargs(kwargs)
            lifecycle.throw_if_cancelled()
            timeout = lifecycle.effective_timeout(self._timeout_seconds)
            headers = {"x-gladia-key": self._api_key}
            filename = str(kwargs.get("filename", "audio.wav"))
            content_type = str(kwargs.get("content_type", "audio/wav"))
            upload_response = checked_request(
                "POST",
                f"{self._base_url}/v2/upload",
                provider="gladia",
                adapter_name=type(self).__name__,
                operation="upload",
                timeout=timeout,
                headers=headers,
                files={"audio": (filename, audio, content_type)},
            )
            upload = response_json(
                upload_response,
                provider="gladia",
                adapter_name=type(self).__name__,
                operation="upload",
            )
            try:
                audio_url = str(upload["audio_url"])
            except (KeyError, TypeError) as error:
                raise self._invalid(error, "upload") from error
            request_payload: dict[str, Any] = {"audio_url": audio_url}
            language = kwargs.get("language")
            if language:
                request_payload["language_config"] = {"languages": [str(language)]}
            submit_response = checked_request(
                "POST",
                f"{self._base_url}/v2/pre-recorded",
                provider="gladia",
                adapter_name=type(self).__name__,
                operation="submit_transcription",
                timeout=timeout,
                headers={**headers, "Content-Type": "application/json"},
                json=request_payload,
            )
            submitted = response_json(
                submit_response,
                provider="gladia",
                adapter_name=type(self).__name__,
                operation="submit_transcription",
            )
            try:
                result_url = str(submitted["result_url"])
            except (KeyError, TypeError) as error:
                raise self._invalid(error, "submit_transcription") from error

            def get_result(request_timeout: float | None) -> Any:
                response = checked_request(
                    "GET",
                    result_url,
                    provider="gladia",
                    adapter_name=type(self).__name__,
                    operation="poll_transcription",
                    timeout=request_timeout,
                    headers=headers,
                )
                return response_json(
                    response,
                    provider="gladia",
                    adapter_name=type(self).__name__,
                    operation="poll_transcription",
                )

            result = poll_json(
                get_result,
                is_pending=lambda value: isinstance(value, dict)
                and value.get("status") in {"queued", "processing"},
                lifecycle=lifecycle,
                timeout=timeout,
                poll_interval=self._poll_interval,
                provider="gladia",
                adapter_name=type(self).__name__,
                operation="transcribe",
            )
            if not isinstance(result, dict):
                raise self._invalid(TypeError("non-object response"), "transcribe")
            if result.get("status") == "error":
                error = RuntimeError(str(result.get("error_code", "transcription failed")))
                raise normalized_adapter_error(
                    error,
                    error_type=SETTServiceAdapterError,
                    provider="gladia",
                    adapter_name=type(self).__name__,
                    operation="transcribe",
                    kind=AdapterErrorKind.UNAVAILABLE,
                ) from error
            try:
                transcript = result["result"]["transcription"]["full_transcript"]
            except (KeyError, TypeError) as error:
                raise self._invalid(error, "transcribe") from error
            lifecycle.throw_if_cancelled()
            return str(transcript)

    def _invalid(self, error: BaseException, operation: str) -> SETTServiceAdapterError:
        return normalized_adapter_error(
            error,
            error_type=SETTServiceAdapterError,
            provider="gladia",
            adapter_name=type(self).__name__,
            operation=operation,
            kind=AdapterErrorKind.INVALID_RESPONSE,
        )

    def __repr__(self) -> str:
        return "GladiaSTTAdapter()"
