"""AssemblyAI asynchronous speech-to-text adapter."""
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


class AssemblyAISTTAdapter(STTBase):
    """Upload, submit, and poll one AssemblyAI transcription job."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = "https://api.assemblyai.com",
        timeout_seconds: float = 300,
        poll_interval_seconds: float = 1,
    ) -> None:
        """Configure credentials, endpoint, and bounded polling behavior."""
        require_requests(adapter_name=type(self).__name__, extra="assemblyai")
        self._api_key = credential(
            api_key,
            "ASSEMBLYAI_API_KEY",
            provider="assemblyai",
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
        """Declare the complete, asynchronous remote transcription path."""
        return AdapterCapabilities(
            AdapterKind.STT,
            streaming=CapabilitySupport.NO,
            voice_cloning=CapabilitySupport.NO,
            partial_results=CapabilitySupport.NO,
            connectivity=ConnectivityRequirement.REMOTE_SERVICE,
            input_formats=("audio/*", "video/*"),
            output_formats=("text/plain",),
            metadata={"api_mode": "asynchronous"},
        )

    def transcribe(self, audio: bytes, **kwargs: Any) -> str:
        """Upload audio and return text after the remote job completes."""
        with adapter_call_scope(self, "transcribe"):
            lifecycle = pop_lifecycle_kwargs(kwargs)
            lifecycle.throw_if_cancelled()
            timeout = lifecycle.effective_timeout(self._timeout_seconds)
            headers = {"Authorization": self._api_key}
            upload_response = checked_request(
                "POST",
                f"{self._base_url}/v2/upload",
                provider="assemblyai",
                adapter_name=type(self).__name__,
                operation="upload",
                timeout=timeout,
                headers={**headers, "Content-Type": "application/octet-stream"},
                data=audio,
            )
            upload = response_json(
                upload_response,
                provider="assemblyai",
                adapter_name=type(self).__name__,
                operation="upload",
            )
            try:
                audio_url = str(upload["upload_url"])
            except (KeyError, TypeError) as error:
                raise self._invalid(error, "upload") from error
            request_payload: dict[str, Any] = {"audio_url": audio_url}
            if kwargs.get("language_code"):
                request_payload["language_code"] = str(kwargs["language_code"])
            submit_response = checked_request(
                "POST",
                f"{self._base_url}/v2/transcript",
                provider="assemblyai",
                adapter_name=type(self).__name__,
                operation="submit_transcription",
                timeout=timeout,
                headers={**headers, "Content-Type": "application/json"},
                json=request_payload,
            )
            submitted = response_json(
                submit_response,
                provider="assemblyai",
                adapter_name=type(self).__name__,
                operation="submit_transcription",
            )
            try:
                transcript_id = str(submitted["id"])
            except (KeyError, TypeError) as error:
                raise self._invalid(error, "submit_transcription") from error

            def get_result(request_timeout: float | None) -> Any:
                response = checked_request(
                    "GET",
                    f"{self._base_url}/v2/transcript/{transcript_id}",
                    provider="assemblyai",
                    adapter_name=type(self).__name__,
                    operation="poll_transcription",
                    timeout=request_timeout,
                    headers=headers,
                )
                return response_json(
                    response,
                    provider="assemblyai",
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
                provider="assemblyai",
                adapter_name=type(self).__name__,
                operation="transcribe",
            )
            if not isinstance(result, dict):
                raise self._invalid(TypeError("non-object response"), "transcribe")
            if result.get("status") == "error":
                error = RuntimeError(str(result.get("error", "transcription failed")))
                raise normalized_adapter_error(
                    error,
                    error_type=SETTServiceAdapterError,
                    provider="assemblyai",
                    adapter_name=type(self).__name__,
                    operation="transcribe",
                    kind=AdapterErrorKind.UNAVAILABLE,
                ) from error
            if result.get("status") != "completed":
                raise self._invalid(KeyError("status"), "transcribe")
            lifecycle.throw_if_cancelled()
            return str(result.get("text") or "")

    def _invalid(self, error: BaseException, operation: str) -> SETTServiceAdapterError:
        return normalized_adapter_error(
            error,
            error_type=SETTServiceAdapterError,
            provider="assemblyai",
            adapter_name=type(self).__name__,
            operation=operation,
            kind=AdapterErrorKind.INVALID_RESPONSE,
        )

    def __repr__(self) -> str:
        return "AssemblyAISTTAdapter()"
