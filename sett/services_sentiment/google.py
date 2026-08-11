"""
SETT Framework: Google Cloud Sentiment Adapter
==============================
Sentiment analysis adapter backed by Google Cloud Natural Language.

Implements SentimentBase: fully interchangeable with any other
provider's adapter.

Ported from the recovered Virtual-assistant-1st-attempt prototype
(SentimentAnalyzer class, Nov. 2024): same underlying sentiment call
(`language_v1.LanguageServiceClient().analyze_sentiment`), reduced to
just that. Two things present in the original were deliberately not
carried over:

  - Storing results in Google Cloud Storage + loading them to
    BigQuery: that was infrastructure for a solo experiment logging
    its own history, not something a sentiment adapter needs to do to
    answer "what is the sentiment of this text": an application that
    wants to log results can do so with the SentimentResult this
    adapter already returns, without the adapter reaching into two
    more cloud services on every call.
  - `enhance_response()` (a Gemini call to generate text): that is
    text generation, not sentiment analysis: it belongs to an LLM
    adapter/PhrasingExpert, not here. Mixing it into this class was
    exactly the kind of two-responsibilities-in-one-expert pattern
    Convention #1's corollary already documents splitting.

Requires: pip install sett-framework[google-sentiment]
"""
from __future__ import annotations
from typing import Any

from sett.core_ruler.lifecycle import pop_lifecycle_kwargs
from sett.services_google_auth import (
    google_credentials_from_path,
    build_google_client,
)
from sett.services_sentiment.base import SentimentBase, SentimentResult, SentenceSentiment
from sett.exceptions import SETTServiceAdapterError


class GoogleSentimentAdapter(SentimentBase):
    """
    Sentiment analysis adapter for Google Cloud Natural Language.

    Usage:
        from sett.services_sentiment.google import GoogleSentimentAdapter

        sentiment = GoogleSentimentAdapter()
        result = sentiment.analyze("I'm quite frustrated with this.")
        print(result.score, result.magnitude)
    """

    def __init__(self, credentials_path: str | None = None) -> None:
        """
        Args:
            credentials_path: Path to a Google Cloud service account
                JSON key, scoped to this instance only. When omitted, the
                client resolves Application Default Credentials on its
                own, in full: GOOGLE_APPLICATION_CREDENTIALS, a gcloud
                login, GCE/GKE metadata or workload identity all apply,
                because this adapter does not second-guess which one is
                in use.
        """
        try:
            from google.cloud import language_v1
            self._language_v1 = language_v1
        except ImportError:
            raise SETTServiceAdapterError(
                "The 'google-cloud-language' package is required to use "
                "GoogleSentimentAdapter. Install it with: "
                "pip install sett-framework[google-sentiment]"
            )

        credentials = google_credentials_from_path(credentials_path)

        self._client = build_google_client(
            self._language_v1.LanguageServiceClient,
            credentials=credentials,
            adapter_name="GoogleSentimentAdapter",
        )

    def analyze(self, text: str, **kwargs: Any) -> SentimentResult:
        """
        Analyze the sentiment of the given text.

        Args:
            text: The text to analyze.
            **kwargs: the framework's reserved ``timeout`` and
                ``cancellation_token`` are honoured; anything else is
                accepted for interface compatibility and ignored.

        Returns:
            A SentimentResult with document-level score/magnitude and
            a per-sentence breakdown (Google Cloud NL always returns
            sentence-level detail, so it is always populated here).
        """
        lifecycle = pop_lifecycle_kwargs(kwargs)
        lifecycle.throw_if_cancelled()
        try:
            document = self._language_v1.Document(
                content=text,
                type_=self._language_v1.Document.Type.PLAIN_TEXT,
            )
            response = self._client.analyze_sentiment(
                request={"document": document},
                timeout=lifecycle.effective_timeout(None),
            )
            sentences = tuple(
                SentenceSentiment(
                    text=sentence.text.content,
                    score=sentence.sentiment.score,
                )
                for sentence in response.sentences
            )
            return SentimentResult(
                score=response.document_sentiment.score,
                magnitude=response.document_sentiment.magnitude,
                sentences=sentences,
            )
        except SETTServiceAdapterError:
            raise
        except Exception as e:
            raise SETTServiceAdapterError(
                f"Google Cloud Natural Language error during analyze(): {e}"
            ) from e

    def __repr__(self) -> str:
        return "GoogleSentimentAdapter()"
