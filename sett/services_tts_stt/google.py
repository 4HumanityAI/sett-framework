"""
SETT Framework: Google Cloud TTS/STT Adapters
==============================
Text-to-speech and speech-to-text adapters backed by Google Cloud
(Text-to-Speech API and Speech-to-Text API).

Implements TTSBase and STTBase: fully interchangeable with any other
provider's adapters (e.g. ElevenLabsTTSAdapter for synthesis).

Ported from the recovered Virtual-assistant-1st-attempt prototype
(Speaker/Listener classes, Nov. 2024): same underlying Google Cloud
calls, reshaped to the stateless adapter contract: no UI coupling, no
playback, no listening loop. Those concerns stayed in the recovered
code as reference (Archivo_legado/Prototipos_2022-2024/
Virtual-assistant-1st-attempt/recovered_original/), they were
intentionally not carried over here.

Requires: pip install sett-framework[google-tts-stt]
"""
from __future__ import annotations
from typing import Any

from sett.core_ruler.lifecycle import pop_lifecycle_kwargs
from sett.services_google_auth import (
    google_credentials_from_path,
    build_google_client,
)
from sett.services_tts_stt.base import TTSBase, STTBase
from sett.exceptions import SETTServiceAdapterError


class GoogleTTSAdapter(TTSBase):
    """
    Text-to-speech adapter for Google Cloud Text-to-Speech.

    Usage:
        from sett.services_tts_stt.google import GoogleTTSAdapter

        tts = GoogleTTSAdapter(language_code="es-AR")
        audio_bytes = tts.synthesize("Hi, how can I help you?")
    """

    DEFAULT_LANGUAGE_CODE = "en-US"
    DEFAULT_VOICE_GENDER = "NEUTRAL"

    def __init__(
        self,
        language_code: str = DEFAULT_LANGUAGE_CODE,
        voice_gender: str = DEFAULT_VOICE_GENDER,
        credentials_path: str | None = None,
    ) -> None:
        """
        Args:
            language_code: BCP-47 language tag for the synthesized voice.
            voice_gender: One of "NEUTRAL", "FEMALE", "MALE".
            credentials_path: Path to a Google Cloud service account
                JSON key, scoped to this instance only. When omitted, the
                client resolves Application Default Credentials on its
                own, in full: GOOGLE_APPLICATION_CREDENTIALS, a gcloud
                login, GCE/GKE metadata or workload identity all apply,
                because this adapter does not second-guess which one is
                in use.
        """
        try:
            from google.cloud import texttospeech
            self._texttospeech = texttospeech
        except ImportError:
            raise SETTServiceAdapterError(
                "The 'google-cloud-texttospeech' package is required to use "
                "GoogleTTSAdapter. Install it with: "
                "pip install sett-framework[google-tts-stt]"
            )

        credentials = google_credentials_from_path(credentials_path)

        self._language_code = language_code
        self._voice_gender = voice_gender.upper()
        self._client = build_google_client(
            self._texttospeech.TextToSpeechClient,
            credentials=credentials,
            adapter_name="GoogleTTSAdapter",
        )

    @property
    def audio_format(self) -> str:
        """The container this adapter returns audio in."""
        return "mp3"

    def synthesize(self, text: str, **kwargs: Any) -> bytes:
        """Synthesize speech audio (MP3 bytes) for the given text."""
        lifecycle = pop_lifecycle_kwargs(kwargs)
        lifecycle.throw_if_cancelled()
        try:
            language_code = kwargs.get("language_code", self._language_code)
            gender_name = kwargs.get("voice_gender", self._voice_gender).upper()
            ssml_gender = getattr(
                self._texttospeech.SsmlVoiceGender,
                gender_name,
                self._texttospeech.SsmlVoiceGender.NEUTRAL,
            )

            response = self._client.synthesize_speech(
                timeout=lifecycle.effective_timeout(None),
                input=self._texttospeech.SynthesisInput(text=text),
                voice=self._texttospeech.VoiceSelectionParams(
                    language_code=language_code,
                    ssml_gender=ssml_gender,
                ),
                audio_config=self._texttospeech.AudioConfig(
                    audio_encoding=self._texttospeech.AudioEncoding.MP3,
                ),
            )
            return response.audio_content
        except SETTServiceAdapterError:
            raise
        except Exception as e:
            raise SETTServiceAdapterError(
                f"Google Cloud TTS error during synthesize(): {e}"
            ) from e

    def __repr__(self) -> str:
        return f"GoogleTTSAdapter(language_code={self._language_code!r})"


class GoogleSTTAdapter(STTBase):
    """
    Speech-to-text adapter for Google Cloud Speech-to-Text.

    Expects LINEAR16-encoded audio (standard WAV PCM): the same
    encoding `speech_recognition.Microphone` produces via
    `audio.get_wav_data()`, which is what the recovered Listener class
    fed it.

    Usage:
        from sett.services_tts_stt.google import GoogleSTTAdapter

        stt = GoogleSTTAdapter(language_code="es-AR")
        text = stt.transcribe(wav_bytes)
    """

    DEFAULT_LANGUAGE_CODE = "en-US"
    DEFAULT_SAMPLE_RATE_HERTZ = 16000

    def __init__(
        self,
        language_code: str = DEFAULT_LANGUAGE_CODE,
        sample_rate_hertz: int = DEFAULT_SAMPLE_RATE_HERTZ,
        credentials_path: str | None = None,
    ) -> None:
        """
        Args:
            language_code: BCP-47 language tag to recognize.
            sample_rate_hertz: Sample rate of the audio that will be
                passed to transcribe(). Must match the actual audio.
            credentials_path: Path to a Google Cloud service account
                JSON key, scoped to this instance only. When omitted, the
                client resolves Application Default Credentials on its
                own, in full: GOOGLE_APPLICATION_CREDENTIALS, a gcloud
                login, GCE/GKE metadata or workload identity all apply,
                because this adapter does not second-guess which one is
                in use.
        """
        try:
            from google.cloud import speech
            self._speech = speech
        except ImportError:
            raise SETTServiceAdapterError(
                "The 'google-cloud-speech' package is required to use "
                "GoogleSTTAdapter. Install it with: "
                "pip install sett-framework[google-tts-stt]"
            )

        credentials = google_credentials_from_path(credentials_path)

        self._language_code = language_code
        self._sample_rate_hertz = sample_rate_hertz
        self._client = build_google_client(
            self._speech.SpeechClient,
            credentials=credentials,
            adapter_name="GoogleSTTAdapter",
        )

    def transcribe(self, audio: bytes, **kwargs: Any) -> str:
        """Transcribe LINEAR16 WAV audio bytes to text."""
        lifecycle = pop_lifecycle_kwargs(kwargs)
        lifecycle.throw_if_cancelled()
        try:
            language_code = kwargs.get("language_code", self._language_code)
            sample_rate_hertz = kwargs.get(
                "sample_rate_hertz", self._sample_rate_hertz
            )

            response = self._client.recognize(
                timeout=lifecycle.effective_timeout(None),
                config=self._speech.RecognitionConfig(
                    encoding=self._speech.RecognitionConfig.AudioEncoding.LINEAR16,
                    sample_rate_hertz=sample_rate_hertz,
                    language_code=language_code,
                ),
                audio=self._speech.RecognitionAudio(content=audio),
            )
            if not response.results:
                return ""
            return response.results[0].alternatives[0].transcript
        except SETTServiceAdapterError:
            raise
        except Exception as e:
            raise SETTServiceAdapterError(
                f"Google Cloud STT error during transcribe(): {e}"
            ) from e

    def __repr__(self) -> str:
        return f"GoogleSTTAdapter(language_code={self._language_code!r})"
