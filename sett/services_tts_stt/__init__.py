"""
SETT Framework: services_tts_stt
==============================
Text-to-speech and speech-to-text adapters. Concrete providers remain
explicitly constructed peers; this package never chooses one for a caller.
"""

from sett.services_tts_stt.assemblyai import AssemblyAISTTAdapter
from sett.services_tts_stt.azure import AzureSTTAdapter, AzureTTSAdapter
from sett.services_tts_stt.base import STTBase, TTSBase
from sett.services_tts_stt.cartesia import CartesiaTTSAdapter
from sett.services_tts_stt.deepgram import DeepgramSTTAdapter
from sett.services_tts_stt.edge import EdgeTTSAdapter
from sett.services_tts_stt.elevenlabs import ElevenLabsTTSAdapter
from sett.services_tts_stt.gladia import GladiaSTTAdapter
from sett.services_tts_stt.google import GoogleSTTAdapter, GoogleTTSAdapter
from sett.services_tts_stt.kokoro import KokoroTTSAdapter
from sett.services_tts_stt.local import (
    FasterWhisperSTTAdapter,
    LocalSTTAdapter,
    LocalTTSAdapter,
    PyttsxTTSAdapter,
)
from sett.services_tts_stt.openai_audio import WhisperSTTAdapter
from sett.services_tts_stt.piper import PiperTTSAdapter

__all__ = [
    "AssemblyAISTTAdapter",
    "AzureSTTAdapter",
    "AzureTTSAdapter",
    "CartesiaTTSAdapter",
    "DeepgramSTTAdapter",
    "EdgeTTSAdapter",
    "ElevenLabsTTSAdapter",
    "FasterWhisperSTTAdapter",
    "GladiaSTTAdapter",
    "GoogleSTTAdapter",
    "GoogleTTSAdapter",
    "KokoroTTSAdapter",
    "LocalSTTAdapter",
    "LocalTTSAdapter",
    "PiperTTSAdapter",
    "PyttsxTTSAdapter",
    "STTBase",
    "TTSBase",
    "WhisperSTTAdapter",
]
