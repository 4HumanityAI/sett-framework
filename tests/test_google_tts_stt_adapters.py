"""
SETT Framework: Tests: GoogleTTSAdapter / GoogleSTTAdapter
======================================================
google-cloud-texttospeech and google-cloud-speech are optional
dependencies (pip install sett-framework[google-tts-stt]): this test
environment does not have them installed, matching how
AnthropicAdapter/OpenAIAdapter/GeminiAdapter have no dedicated test
file today (they need their real SDK to test meaningfully and this
project does not pretend otherwise, see SETT_Conventions.md #16).

What IS tested here without the real SDK:
  - the "package not installed" path (real behavior in this
    environment right now, made deterministic via sys.modules so it
    does not silently pass/fail depending on what happens to be pip
    installed);
  - the full synthesize()/transcribe() behavior using a fake
    google.cloud.texttospeech / google.cloud.speech module injected
    into sys.modules: the adapter code cannot tell the difference
    between this and the real package, since both are reached through
    the same `from google.cloud import texttospeech` lazy import.
"""
import sys
import pytest
from unittest.mock import MagicMock, patch

from sett.exceptions import SETTServiceAdapterError


# ---------------------------------------------------------------------------
# "Package not installed": deterministic regardless of the environment.
# ---------------------------------------------------------------------------

class TestMissingDependency:

    def test_tts_adapter_missing_package_raises_sett_service_adapter_error(self):
        with patch.dict(sys.modules, {"google.cloud.texttospeech": None,
                                       "google.cloud": None}):
            from sett.services_tts_stt.google import GoogleTTSAdapter
            with pytest.raises(SETTServiceAdapterError) as exc_info:
                GoogleTTSAdapter()
            assert "google-cloud-texttospeech" in str(exc_info.value)

    def test_stt_adapter_missing_package_raises_sett_service_adapter_error(self):
        with patch.dict(sys.modules, {"google.cloud.speech": None,
                                       "google.cloud": None}):
            from sett.services_tts_stt.google import GoogleSTTAdapter
            with pytest.raises(SETTServiceAdapterError) as exc_info:
                GoogleSTTAdapter()
            assert "google-cloud-speech" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Fake google.cloud.texttospeech / google.cloud.speech modules, injected via
# sys.modules: the adapter's lazy `from google.cloud import X` cannot tell
# these apart from the real package.
# ---------------------------------------------------------------------------

def _fake_google_cloud_sys_modules(submodule_name: str, submodule_mock: MagicMock) -> dict:
    """
    Build the full sys.modules patch for `from google.cloud import <submodule_name>`.

    Not enough to only patch sys.modules["google.cloud.<name>"]: Python's
    IMPORT_FROM opcode tries `getattr(sys.modules["google.cloud"], name)`
    FIRST, and a plain MagicMock parent auto-generates that attribute
    instead of raising AttributeError: so it would silently return an
    unrelated, unconfigured mock instead of falling through to the
    sys.modules submodule lookup. Setting the attribute explicitly on the
    parent mock is what makes `from google.cloud import X` actually reach
    the specific submodule_mock configured by the test.
    """
    google_cloud_mock = MagicMock(name="google.cloud")
    setattr(google_cloud_mock, submodule_name, submodule_mock)

    # F18 fix: the adapter now loads credentials explicitly via
    # google.oauth2.service_account instead of mutating os.environ, so
    # that import must resolve too - same parent-attribute caveat as
    # google.cloud above.
    service_account_mock = MagicMock(name="google.oauth2.service_account")
    oauth2_mock = MagicMock(name="google.oauth2")
    oauth2_mock.service_account = service_account_mock

    google_mock = MagicMock(name="google")
    google_mock.cloud = google_cloud_mock
    google_mock.oauth2 = oauth2_mock
    return {
        "google": google_mock,
        "google.cloud": google_cloud_mock,
        f"google.cloud.{submodule_name}": submodule_mock,
        "google.oauth2": oauth2_mock,
        "google.oauth2.service_account": service_account_mock,
    }


def _fake_texttospeech_module():
    fake = MagicMock(name="texttospeech")
    fake.SsmlVoiceGender.NEUTRAL = "NEUTRAL"
    fake.SsmlVoiceGender.FEMALE = "FEMALE"
    fake.SsmlVoiceGender.MALE = "MALE"
    fake.AudioEncoding.MP3 = "MP3"

    client_instance = MagicMock(name="TextToSpeechClient_instance")
    client_instance.synthesize_speech.return_value = MagicMock(
        audio_content=b"fake-google-mp3-bytes"
    )
    fake.TextToSpeechClient.return_value = client_instance
    return fake, client_instance


def _fake_speech_module():
    fake = MagicMock(name="speech")
    fake.RecognitionConfig.AudioEncoding.LINEAR16 = "LINEAR16"

    client_instance = MagicMock(name="SpeechClient_instance")
    fake.SpeechClient.return_value = client_instance
    return fake, client_instance


@pytest.fixture
def fake_google_credentials(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/fake/path/creds.json")


class TestGoogleTTSAdapterSynthesize:

    def test_synthesize_returns_audio_bytes(self, fake_google_credentials):
        fake_module, client = _fake_texttospeech_module()
        with patch.dict(sys.modules, _fake_google_cloud_sys_modules("texttospeech", fake_module)):
            from sett.services_tts_stt.google import GoogleTTSAdapter
            adapter = GoogleTTSAdapter(language_code="es-AR")
            result = adapter.synthesize("Hi, how can I help you?")
            assert result == b"fake-google-mp3-bytes"

    def test_synthesize_uses_configured_language_code(self, fake_google_credentials):
        fake_module, client = _fake_texttospeech_module()
        with patch.dict(sys.modules, _fake_google_cloud_sys_modules("texttospeech", fake_module)):
            from sett.services_tts_stt.google import GoogleTTSAdapter
            adapter = GoogleTTSAdapter(language_code="es-AR")
            adapter.synthesize("hola")

            _, call_kwargs = client.synthesize_speech.call_args
            fake_module.VoiceSelectionParams.assert_called_with(
                language_code="es-AR", ssml_gender="NEUTRAL"
            )

    def test_audio_format_is_mp3(self, fake_google_credentials):
        fake_module, _ = _fake_texttospeech_module()
        with patch.dict(sys.modules, _fake_google_cloud_sys_modules("texttospeech", fake_module)):
            from sett.services_tts_stt.google import GoogleTTSAdapter
            adapter = GoogleTTSAdapter()
            assert adapter.audio_format == "mp3"

    def test_missing_credentials_surface_as_a_framework_error(self, monkeypatch):
        """The client library decides; SETT only retypes its refusal.

        This used to assert that an unset GOOGLE_APPLICATION_CREDENTIALS
        was itself the error. That encoded a defect: Application Default
        Credentials resolve from five sources and the environment variable
        is one, so the check refused four valid setups - including GKE
        workload identity, where the only way to satisfy it was the
        service account key file Google recommends avoiding.
        """
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        fake_module, _ = _fake_texttospeech_module()

        class DefaultCredentialsError(Exception):
            pass

        fake_module.TextToSpeechClient.side_effect = DefaultCredentialsError(
            "Your default credentials were not found."
        )
        with patch.dict(sys.modules, _fake_google_cloud_sys_modules("texttospeech", fake_module)):
            from sett.services_tts_stt.google import GoogleTTSAdapter
            with pytest.raises(SETTServiceAdapterError) as excinfo:
                GoogleTTSAdapter()
        assert "credentials" in str(excinfo.value).lower()

    def test_ambient_credentials_without_the_env_var_are_accepted(
        self, monkeypatch
    ):
        """The regression itself: metadata / workload identity must work.

        With no environment variable and no explicit path, the client
        constructs successfully because the environment supplied
        credentials some other way. SETT must not stand in front of that.
        """
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        fake_module, _ = _fake_texttospeech_module()
        with patch.dict(sys.modules, _fake_google_cloud_sys_modules("texttospeech", fake_module)):
            from sett.services_tts_stt.google import GoogleTTSAdapter
            GoogleTTSAdapter()
            fake_module.TextToSpeechClient.assert_called_once_with(credentials=None)

    def test_credentials_path_argument_builds_explicit_credentials(
        self, monkeypatch
    ):
        """F18 regression (defensive audit #2): see the matching test in
        test_google_sentiment_adapter.py for the full rationale. The
        environment must stay untouched and the client must receive an
        explicit `credentials=` built from the given path.
        """
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        fake_module, _ = _fake_texttospeech_module()
        modules = _fake_google_cloud_sys_modules("texttospeech", fake_module)
        fake_credentials = object()
        modules[
            "google.oauth2.service_account"
        ].Credentials.from_service_account_file.return_value = fake_credentials
        with patch.dict(sys.modules, modules):
            from sett.services_tts_stt.google import GoogleTTSAdapter
            GoogleTTSAdapter(credentials_path="/explicit/creds.json")

            modules[
                "google.oauth2.service_account"
            ].Credentials.from_service_account_file.assert_called_once_with(
                "/explicit/creds.json"
            )
            fake_module.TextToSpeechClient.assert_called_once_with(
                credentials=fake_credentials
            )
            import os
            assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ

    def test_synthesize_wraps_client_error(self, fake_google_credentials):
        fake_module, client = _fake_texttospeech_module()
        client.synthesize_speech.side_effect = RuntimeError("quota exceeded")
        with patch.dict(sys.modules, _fake_google_cloud_sys_modules("texttospeech", fake_module)):
            from sett.services_tts_stt.google import GoogleTTSAdapter
            adapter = GoogleTTSAdapter()
            with pytest.raises(SETTServiceAdapterError) as exc_info:
                adapter.synthesize("hola")
            assert "quota exceeded" in str(exc_info.value)


class TestGoogleSTTAdapterTranscribe:

    def test_transcribe_returns_transcript(self, fake_google_credentials):
        fake_module, client = _fake_speech_module()
        result_mock = MagicMock()
        result_mock.alternatives[0].transcript = "hola, necesito ayuda"
        client.recognize.return_value = MagicMock(results=[result_mock])

        with patch.dict(sys.modules, _fake_google_cloud_sys_modules("speech", fake_module)):
            from sett.services_tts_stt.google import GoogleSTTAdapter
            adapter = GoogleSTTAdapter(language_code="es-AR")
            text = adapter.transcribe(b"fake-wav-bytes")
            assert text == "hola, necesito ayuda"

    def test_transcribe_no_results_returns_empty_string(self, fake_google_credentials):
        fake_module, client = _fake_speech_module()
        client.recognize.return_value = MagicMock(results=[])

        with patch.dict(sys.modules, _fake_google_cloud_sys_modules("speech", fake_module)):
            from sett.services_tts_stt.google import GoogleSTTAdapter
            adapter = GoogleSTTAdapter()
            assert adapter.transcribe(b"silence") == ""

    def test_transcribe_wraps_client_error(self, fake_google_credentials):
        fake_module, client = _fake_speech_module()
        client.recognize.side_effect = RuntimeError("bad audio encoding")

        with patch.dict(sys.modules, _fake_google_cloud_sys_modules("speech", fake_module)):
            from sett.services_tts_stt.google import GoogleSTTAdapter
            adapter = GoogleSTTAdapter()
            with pytest.raises(SETTServiceAdapterError) as exc_info:
                adapter.transcribe(b"bad-bytes")
            assert "bad audio encoding" in str(exc_info.value)

    def test_credentials_path_argument_builds_explicit_credentials(
        self, monkeypatch
    ):
        """F18 regression (defensive audit #2), STT side."""
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        fake_module, _ = _fake_speech_module()
        modules = _fake_google_cloud_sys_modules("speech", fake_module)
        fake_credentials = object()
        modules[
            "google.oauth2.service_account"
        ].Credentials.from_service_account_file.return_value = fake_credentials
        with patch.dict(sys.modules, modules):
            from sett.services_tts_stt.google import GoogleSTTAdapter
            GoogleSTTAdapter(credentials_path="/explicit/creds.json")

            fake_module.SpeechClient.assert_called_once_with(
                credentials=fake_credentials
            )
            import os
            assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ

    def test_missing_credentials_surface_as_a_framework_error(self, monkeypatch):
        """The client library decides; SETT only retypes its refusal.

        This used to assert that an unset GOOGLE_APPLICATION_CREDENTIALS
        was itself the error. That encoded a defect: Application Default
        Credentials resolve from five sources and the environment variable
        is one, so the check refused four valid setups - including GKE
        workload identity, where the only way to satisfy it was the
        service account key file Google recommends avoiding.
        """
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        fake_module, _ = _fake_speech_module()

        class DefaultCredentialsError(Exception):
            pass

        fake_module.SpeechClient.side_effect = DefaultCredentialsError(
            "Your default credentials were not found."
        )
        with patch.dict(sys.modules, _fake_google_cloud_sys_modules("speech", fake_module)):
            from sett.services_tts_stt.google import GoogleSTTAdapter
            with pytest.raises(SETTServiceAdapterError) as excinfo:
                GoogleSTTAdapter()
        assert "credentials" in str(excinfo.value).lower()

    def test_ambient_credentials_without_the_env_var_are_accepted(
        self, monkeypatch
    ):
        """The regression itself: metadata / workload identity must work.

        With no environment variable and no explicit path, the client
        constructs successfully because the environment supplied
        credentials some other way. SETT must not stand in front of that.
        """
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        fake_module, _ = _fake_speech_module()
        with patch.dict(sys.modules, _fake_google_cloud_sys_modules("speech", fake_module)):
            from sett.services_tts_stt.google import GoogleSTTAdapter
            GoogleSTTAdapter()
            fake_module.SpeechClient.assert_called_once_with(credentials=None)
