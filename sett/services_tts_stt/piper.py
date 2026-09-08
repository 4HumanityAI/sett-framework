"""Piper local neural text-to-speech adapter."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
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
from sett.exceptions import SETTServiceAdapterError
from sett.services_tts_stt.base import TTSBase


class PiperTTSAdapter(TTSBase):
    """Generate WAV bytes with an explicitly selected local Piper voice model."""

    def __init__(self, model_path: str, *, executable: str = "piper") -> None:
        """Select a local Piper model and executable without loading either."""
        self._model_path = str(Path(model_path))
        self._executable = executable

    @property
    def audio_format(self) -> str:
        """Return the WAV container produced by the Piper command."""
        return "wav"

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Declare local synthesis with an explicitly supplied voice model."""
        return AdapterCapabilities(
            AdapterKind.TTS,
            streaming=CapabilitySupport.NO,
            voice_cloning=CapabilitySupport.NO,
            partial_results=CapabilitySupport.NO,
            connectivity=ConnectivityRequirement.NONE,
            input_formats=("text/plain",),
            output_formats=("audio/wav",),
            metadata={"engine": "piper", "voice_model_required": True},
        )

    def synthesize(self, text: str, **kwargs: Any) -> bytes:
        """Run Piper without a shell and return its generated WAV bytes."""
        with adapter_call_scope(self, "synthesize"):
            lifecycle = pop_lifecycle_kwargs(kwargs)
            lifecycle.throw_if_cancelled()
            executable = shutil.which(self._executable)
            if executable is None:
                raise SETTServiceAdapterError(
                    "The Piper executable is required to use PiperTTSAdapter. "
                    "SETT installs from source; from your checkout run: "
                    "pip install -e \".[piper]\"",
                    kind=AdapterErrorKind.CONFIGURATION,
                    provider="piper",
                    adapter_name=type(self).__name__,
                    reason_code="adapter.dependency_missing",
                )
            path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as output:
                    path = Path(output.name)
                command = [
                    executable,
                    "--model",
                    str(kwargs.get("model_path", self._model_path)),
                    "--output_file",
                    str(path),
                ]
                speaker = kwargs.get("speaker")
                if speaker is not None:
                    command.extend(("--speaker", str(speaker)))
                subprocess.run(
                    command,
                    input=text.encode("utf-8"),
                    capture_output=True,
                    check=True,
                    timeout=lifecycle.effective_timeout(None),
                )
                lifecycle.throw_if_cancelled()
                return path.read_bytes()
            except subprocess.TimeoutExpired as error:
                raise normalized_adapter_error(
                    error,
                    error_type=SETTServiceAdapterError,
                    provider="piper",
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
                    provider="piper",
                    adapter_name=type(self).__name__,
                    operation="synthesize",
                ) from error
            finally:
                if path is not None:
                    path.unlink(missing_ok=True)

    def __repr__(self) -> str:
        return f"PiperTTSAdapter(model_path={self._model_path!r})"
