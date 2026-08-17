"""Caller-owned adapter selection without a framework default."""
from sett import (
    AdapterCapabilities,
    AdapterHandle,
    AdapterKind,
    AdapterRequirements,
    CapabilitySupport,
    ConnectivityRequirement,
    PrioritySelectionPolicy,
    select_adapter,
)
from sett.services_tts_stt.base import TTSBase


class DemoTTS(TTSBase):
    """Small no-network adapter used only to demonstrate selection."""

    def __init__(self, label: str) -> None:
        self.label = label

    @property
    def audio_format(self) -> str:
        return "wav"

    @property
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            AdapterKind.TTS,
            streaming=CapabilitySupport.NO,
            connectivity=ConnectivityRequirement.NONE,
            input_formats=("text/plain",),
            output_formats=("audio/wav",),
        )

    def synthesize(self, text: str, **kwargs) -> bytes:
        return f"{self.label}:{text}".encode()


adapters = {
    "workstation": DemoTTS("workstation"),
    "portable": DemoTTS("portable"),
}
handles = [AdapterHandle(name, adapter) for name, adapter in adapters.items()]

# The application, not SETT, owns this order.
selection = select_adapter(
    handles,
    AdapterRequirements(AdapterKind.TTS, output_format="audio/wav"),
    policy=PrioritySelectionPolicy(("portable", "workstation")),
)

if selection.chosen is None:
    raise RuntimeError(selection.safe_view())

audio = adapters[selection.chosen].synthesize("Hello")
print(selection.chosen, audio)
