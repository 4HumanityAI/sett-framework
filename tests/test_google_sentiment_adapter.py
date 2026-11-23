"""
SETT Framework: Tests: GoogleSentimentAdapter
======================================================
google-cloud-language is an optional dependency (from a source checkout,
pip install -e ".[google-sentiment]"): not installed in this test
environment, same situation as the TTS/STT Google adapters. See
test_google_tts_stt_adapters.py's module docstring for why sys.modules
injection (not mocking an installed SDK) is the testing strategy here,
and why a plain MagicMock parent module is not enough on its own.
"""
import sys
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock, patch

import pytest

from sett.exceptions import SETTServiceAdapterError
from sett.services_sentiment.base import SentenceSentiment, SentimentResult


def _fake_google_cloud_sys_modules(submodule_name: str, submodule_mock: MagicMock) -> dict:
    """Same construction as test_google_tts_stt_adapters.py: see that
    file for why the parent attribute must be set explicitly.

    Also registers a fake google.oauth2.service_account: the
    adapter now loads credentials explicitly from a file via
    google.oauth2.service_account.Credentials.from_service_account_file()
    instead of mutating os.environ, so that import must resolve too.
    """
    google_cloud_mock = MagicMock(name="google.cloud")
    setattr(google_cloud_mock, submodule_name, submodule_mock)

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


def _fake_language_v1_module():
    fake = MagicMock(name="language_v1")
    fake.Document.Type.PLAIN_TEXT = "PLAIN_TEXT"

    client_instance = MagicMock(name="LanguageServiceClient_instance")
    fake.LanguageServiceClient.return_value = client_instance
    return fake, client_instance


def _fake_sentiment_response(score: float, magnitude: float, sentences: list[tuple[str, float]]):
    """Build a fake response shaped like Google NL's AnalyzeSentimentResponse."""
    response = MagicMock()
    response.document_sentiment.score = score
    response.document_sentiment.magnitude = magnitude

    sentence_mocks = []
    for text, sentence_score in sentences:
        s = MagicMock()
        s.text.content = text
        s.sentiment.score = sentence_score
        sentence_mocks.append(s)
    response.sentences = sentence_mocks
    return response


@pytest.fixture
def fake_google_credentials(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/fake/path/creds.json")


class TestMissingDependency:

    def test_missing_package_raises_sett_service_adapter_error(self):
        with patch.dict(sys.modules, {"google.cloud.language_v1": None,
                                       "google.cloud": None}):
            from sett.services_sentiment.google import GoogleSentimentAdapter
            with pytest.raises(SETTServiceAdapterError) as exc_info:
                GoogleSentimentAdapter()
            assert "google-cloud-language" in str(exc_info.value)


class TestGoogleSentimentAdapterAnalyze:

    def test_analyze_returns_score_and_magnitude(self, fake_google_credentials):
        fake_module, client = _fake_language_v1_module()
        client.analyze_sentiment.return_value = _fake_sentiment_response(
            score=-0.6, magnitude=1.2, sentences=[]
        )
        with patch.dict(sys.modules, _fake_google_cloud_sys_modules("language_v1", fake_module)):
            from sett.services_sentiment.google import GoogleSentimentAdapter
            adapter = GoogleSentimentAdapter()
            result = adapter.analyze("I'm quite frustrated with this.")

            assert isinstance(result, SentimentResult)
            assert result.score == -0.6
            assert result.magnitude == 1.2

    def test_analyze_returns_per_sentence_breakdown(self, fake_google_credentials):
        fake_module, client = _fake_language_v1_module()
        client.analyze_sentiment.return_value = _fake_sentiment_response(
            score=0.1,
            magnitude=0.9,
            sentences=[
                ("Great help you gave.", -0.5),
                ("As always.", -0.4),
            ],
        )
        with patch.dict(sys.modules, _fake_google_cloud_sys_modules("language_v1", fake_module)):
            from sett.services_sentiment.google import GoogleSentimentAdapter
            adapter = GoogleSentimentAdapter()
            result = adapter.analyze("Great help you gave. As always.")

            assert len(result.sentences) == 2
            assert result.sentences[0] == SentenceSentiment(
                text="Great help you gave.", score=-0.5
            )
            assert result.sentences[1] == SentenceSentiment(
                text="As always.", score=-0.4
            )

    def test_analyze_sends_text_as_plain_text_document(self, fake_google_credentials):
        fake_module, client = _fake_language_v1_module()
        client.analyze_sentiment.return_value = _fake_sentiment_response(
            score=0.0, magnitude=0.0, sentences=[]
        )
        with patch.dict(sys.modules, _fake_google_cloud_sys_modules("language_v1", fake_module)):
            from sett.services_sentiment.google import GoogleSentimentAdapter
            adapter = GoogleSentimentAdapter()
            adapter.analyze("hola")

            fake_module.Document.assert_called_with(
                content="hola", type_="PLAIN_TEXT"
            )

    def test_missing_credentials_surface_as_a_framework_error(self, monkeypatch):
        """The client library decides; SETT only retypes its refusal.

        This used to assert that an unset GOOGLE_APPLICATION_CREDENTIALS
        was itself the error. That encoded a defect: Application Default
        Credentials resolve from five sources and the environment variable
        is one, so the check refused four valid setups, including GKE
        workload identity, where the only way to satisfy it was the
        service account key file Google recommends avoiding.
        """
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        fake_module, _ = _fake_language_v1_module()

        class DefaultCredentialsError(Exception):
            pass

        fake_module.LanguageServiceClient.side_effect = DefaultCredentialsError(
            "Your default credentials were not found."
        )
        with patch.dict(sys.modules, _fake_google_cloud_sys_modules("language_v1", fake_module)):
            from sett.services_sentiment.google import GoogleSentimentAdapter
            with pytest.raises(SETTServiceAdapterError) as excinfo:
                GoogleSentimentAdapter()
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
        fake_module, _ = _fake_language_v1_module()
        with patch.dict(sys.modules, _fake_google_cloud_sys_modules("language_v1", fake_module)):
            from sett.services_sentiment.google import GoogleSentimentAdapter
            GoogleSentimentAdapter()
            fake_module.LanguageServiceClient.assert_called_once_with(credentials=None)

    def test_credentials_path_argument_builds_explicit_credentials(
        self, monkeypatch
    ):
        """Regression: credentials are loaded without touching os.environ.

        credentials_path used to be applied by mutating the process-wide
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"], global state that
        a second adapter instance configured with a different path would
        silently overwrite for the first. It is now loaded into an
        explicit Credentials object and passed directly to the client
        constructor, and the environment is never touched.
        """
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        fake_module, _ = _fake_language_v1_module()
        modules = _fake_google_cloud_sys_modules("language_v1", fake_module)
        fake_credentials = object()
        modules[
            "google.oauth2.service_account"
        ].Credentials.from_service_account_file.return_value = fake_credentials
        with patch.dict(sys.modules, modules):
            from sett.services_sentiment.google import GoogleSentimentAdapter
            GoogleSentimentAdapter(credentials_path="/explicit/creds.json")

            modules[
                "google.oauth2.service_account"
            ].Credentials.from_service_account_file.assert_called_once_with(
                "/explicit/creds.json"
            )
            fake_module.LanguageServiceClient.assert_called_once_with(
                credentials=fake_credentials
            )
            import os
            assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ

    def test_analyze_wraps_client_error(self, fake_google_credentials):
        fake_module, client = _fake_language_v1_module()
        client.analyze_sentiment.side_effect = RuntimeError("quota exceeded")
        with patch.dict(sys.modules, _fake_google_cloud_sys_modules("language_v1", fake_module)):
            from sett.services_sentiment.google import GoogleSentimentAdapter
            adapter = GoogleSentimentAdapter()
            with pytest.raises(SETTServiceAdapterError) as exc_info:
                adapter.analyze("hola")
            # The provider's own text is reachable through the
            # chained cause, never through the public message.
            assert "quota exceeded" not in str(exc_info.value)
            assert "google_cloud_sentiment analyze failed" in str(exc_info.value)
            assert "quota exceeded" in str(exc_info.value.__cause__)


class TestSentimentResultAndSentenceSentiment:

    def test_sentiment_result_defaults_to_empty_sentences(self):
        result = SentimentResult(score=0.5, magnitude=0.5)
        assert result.sentences == ()

    def test_sentiment_result_is_frozen(self):
        result = SentimentResult(score=0.5, magnitude=0.5)
        with pytest.raises(FrozenInstanceError):
            result.score = 0.9

    def test_sentence_sentiment_is_frozen(self):
        s = SentenceSentiment(text="hola", score=0.1)
        with pytest.raises(FrozenInstanceError):
            s.score = 0.9
