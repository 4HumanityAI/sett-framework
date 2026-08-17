"""Unofficial community Edge reader-mode text-to-speech adapter.

This adapter uses ``edge-tts``, which calls an internal Microsoft Edge
reader-mode service rather than the supported Azure Speech API.  Availability,
terms, and suitability for commercial use are therefore application-owned
risks.  Use ``AzureTTSAdapter`` when a supported Azure contract is required.
"""
from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import Thread
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
from sett.services_tts_stt.base import TTSBase


class EdgeTTSAdapter(TTSBase):
    """Generate MP3 bytes through the unofficial ``edge-tts`` client."""

    def __init__(
        self,
        *,
        voice: str = "en-US-AriaNeural",
        timeout_seconds: float = 60,
        communicate_factory: Callable[..., Any] | None = None,
    ) -> None:
        """Configure a community voice client without contacting its service."""
        self._voice = voice
        self._timeout_seconds = validate_duration_seconds(
            timeout_seconds, name="timeout_seconds"
        )
        self._communicate_factory = communicate_factory

    @property
    def audio_format(self) -> str:
        """Return the MP3 container produced by edge-tts."""
        return "mp3"

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Declare the unofficial remote implementation and its limitations."""
        return AdapterCapabilities(
            AdapterKind.TTS,
            streaming=CapabilitySupport.NO,
            voice_cloning=CapabilitySupport.NO,
            partial_results=CapabilitySupport.NO,
            connectivity=ConnectivityRequirement.REMOTE_SERVICE,
            input_formats=("text/plain",),
            output_formats=("audio/mpeg",),
            metadata={"community_unofficial": True, "supported_azure_api": False},
        )

    def _factory(self) -> Callable[..., Any]:
        if self._communicate_factory is not None:
            return self._communicate_factory
        try:
            from edge_tts import Communicate
        except ImportError as error:
            raise SETTServiceAdapterError(
                "The 'edge-tts' package is required to use EdgeTTSAdapter. "
                "Install it with: pip install sett-framework[edge-tts]",
                kind=AdapterErrorKind.CONFIGURATION,
                adapter_name=type(self).__name__,
                reason_code="adapter.dependency_missing",
            ) from error
        return Communicate

    def synthesize(self, text: str, **kwargs: Any) -> bytes:
        """Synthesize text with the async-only client and return MP3 bytes."""
        with adapter_call_scope(self, "synthesize"):
            lifecycle = pop_lifecycle_kwargs(kwargs)
            lifecycle.throw_if_cancelled()
            timeout = lifecycle.effective_timeout(self._timeout_seconds)
            path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as output:
                    path = Path(output.name)
                voice = str(kwargs.get("voice", self._voice))
                communicate = self._factory()(text, voice)

                async def save() -> None:
                    task = communicate.save(str(path))
                    if timeout is None:
                        await task
                    else:
                        await asyncio.wait_for(task, timeout=timeout)

                self._run_coroutine(save)
                lifecycle.throw_if_cancelled()
                return path.read_bytes()
            except asyncio.TimeoutError as error:
                raise normalized_adapter_error(
                    error,
                    error_type=SETTServiceAdapterError,
                    provider="edge-tts",
                    adapter_name=type(self).__name__,
                    operation="synthesize",
                    kind=AdapterErrorKind.PROVIDER_TIMEOUT,
                ) from error
            except SETTServiceAdapterError:
                raise
            except Exception as error:
                raise normalized_adapter_error(
                    error,
                    error_type=SETTServiceAdapterError,
                    provider="edge-tts",
                    adapter_name=type(self).__name__,
                    operation="synthesize",
                ) from error
            finally:
                if path is not None:
                    path.unlink(missing_ok=True)

    @staticmethod
    def _run_coroutine(factory: Callable[[], Any]) -> None:
        """Run async-only edge-tts safely even inside an active event loop."""
        outcome: list[BaseException] = []

        def runner() -> None:
            try:
                asyncio.run(factory())
            except Exception as error:  # noqa: BLE001 - re-raised on caller thread
                outcome.append(error)

        thread = Thread(target=runner, daemon=True)
        thread.start()
        thread.join()
        if outcome:
            raise outcome[0]

    def __repr__(self) -> str:
        return f"EdgeTTSAdapter(voice={self._voice!r})"
