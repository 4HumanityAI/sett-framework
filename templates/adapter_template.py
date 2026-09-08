"""
templates/adapter_template.py
==============================
Copy this file, rename it, and fill in the parts marked TODO.

An adapter is SETT's boundary for an external model, speech or
sentiment provider. It implements an independent provider contract,
not an Agent or Expert contract. Nothing here sees an ambient execution
control, which is why the framework reserves two keyword arguments
(``timeout`` and ``cancellation_token``) to carry a run's remaining
time and its cancellation into a provider call.

This file, as it is, is real code: it can be instantiated and inspected
without filling in anything, and it satisfies the static adapter
contract kit in ``sett.testing.adapter_contract``. Calling it before
implementing the provider and result mapping fails closed with a
structured configuration error.

What this template does NOT do is contact a provider. It declares that
honestly (``connectivity=NONE``, ``template=True`` in metadata) and
raises a structured configuration error, so a half-finished copy can
never be mistaken for a working integration or fabricate a neutral
measurement.

Choosing your base class
------------------------
This template implements ``SentimentBase`` because it is the smallest
contract in the framework: one method. The plumbing below is identical
for every adapter kind, so switching is a two-line change:

  - LLM:  ``from sett.services_llm.base import LLMBase`` and
          ``AdapterKind.LLM``
  - TTS:  ``from sett.services_tts_stt.base import TTSBase`` and
          ``AdapterKind.TTS``
  - STT:  ``from sett.services_tts_stt.base import STTBase`` and
          ``AdapterKind.STT``

Then implement that base's abstract method instead of ``analyze()``,
keeping the same five-step body documented in it.

Verifying your copy
-------------------
The framework ships checks so you do not have to trust this file:

    from sett.core_ruler.adapter import AdapterKind
    from sett.testing.adapter_contract import assert_adapter_contract

    assert_adapter_contract(MyAdapter(), AdapterKind.SENTIMENT)

See ``docs/adapter_contract.md`` for the full contract and
``assert_cancellation_preflight`` / ``assert_normalized_adapter_error``
for the two behavioral probes.
"""
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
from sett.core_ruler.lifecycle import pop_lifecycle_kwargs
from sett.exceptions import (
    SETTAdapterError,
    SETTCancelledError,
    SETTServiceAdapterError,
    SETTTimeoutError,
)
from sett.services_sentiment.base import SentimentBase, SentimentResult

# TODO: name the provider once, here, and use it everywhere below.
# It reaches trace events and error views, so keep it stable, lowercase
# and free of credentials or account identifiers.
_PROVIDER = "template_provider"


# TODO: rename the class after your provider
# (e.g. AcmeSentimentAdapter, LocalToneAdapter)
class MyAdapter(SentimentBase):
    """
    TODO: one line describing WHICH provider this adapter integrates.
    Example: "Sentiment analysis backed by the Acme NLU REST API."
    """

    def __init__(self, api_key: str | None = None) -> None:
        """
        Args:
            api_key: Provider credential, scoped to this instance only.
                Never read a credential from a module-level global: two
                adapters of the same kind must be able to coexist with
                different accounts.

        TODO: if your provider needs an optional third-party package,
        import it here and translate the failure, so a missing extra is
        a configuration error and not an ImportError from inside a run:

        A copied adapter belongs to its author, not to SETT's package, so
        name the provider SDK directly. Extras such as ``.[anthropic]`` are
        only available to adapters that ship inside SETT's source tree.

            try:
                import acme_sdk
            except ImportError as error:
                raise SETTServiceAdapterError(
                    "The 'acme-sdk' package is required to use "
                    "MyAdapter. Install it with: "
                    "pip install acme-sdk",
                    kind=AdapterErrorKind.CONFIGURATION,
                    provider=_PROVIDER,
                    adapter_name=type(self).__name__,
                    reason_code="adapter.dependency_missing",
                ) from error

        TODO: construct the provider client here, inside a try/except
        that translates any construction failure the same way. A client
        that raises on bad configuration must not leak the raw provider
        exception past this boundary.
        """
        # Private by convention AND by exposure: no property, no repr,
        # no capabilities entry. The contract kit's
        # assert_normalized_adapter_error(secrets=(...)) probe checks
        # that this value never reaches an error's safe_view().
        self._api_key = api_key

    @property
    def capabilities(self) -> AdapterCapabilities:
        """
        TODO: declare what THIS provider does, not what you hope it does.

        Every field is a tri-state: use ``UNKNOWN`` when you have not
        verified the behavior. Missing evidence is not evidence of
        absence, and declaring ``NO`` for something you simply have not
        tested makes ``select_adapter()`` reject a viable provider.

        Build a new object on every access, as below: the caller must
        not be able to mutate a shared declaration.
        """
        return AdapterCapabilities(
            AdapterKind.SENTIMENT,
            streaming=CapabilitySupport.NO,
            voice_cloning=CapabilitySupport.NO,
            partial_results=CapabilitySupport.NO,
            connectivity=ConnectivityRequirement.NONE,
            input_formats=("text",),
            output_formats=("sentiment",),
            metadata={"template": True},
        )

    def analyze(self, text: str, **kwargs: Any) -> SentimentResult:
        """
        TODO: replace the placeholder body with your provider call.

        The five steps below are the contract. Keep all five, in this
        order, whichever base class you end up implementing.

        Args:
            text: The text to analyze.
            **kwargs: the framework's reserved ``timeout`` and
                ``cancellation_token`` are honoured; anything else is
                forwarded to the provider.

        Returns:
            A SentimentResult after both provider and mapping TODOs are
            implemented. The unmodified template fails closed.
        """
        # 1. Wrap the whole call. This is what updates passive health
        #    and emits sanitized trace events; without it your adapter
        #    stays UNKNOWN forever and never appears in a trace.
        with adapter_call_scope(self, "analyze"):
            # 2. Take the reserved kwargs OUT of **kwargs, so what
            #    remains can be forwarded to an SDK that would reject
            #    unknown arguments.
            lifecycle = pop_lifecycle_kwargs(kwargs)

            # 3. Refuse before spending anything. A run cancelled while
            #    queued must not reach a billable provider, so this
            #    check goes before the first provider statement, not
            #    after it.
            lifecycle.throw_if_cancelled()

            response_received = False
            try:
                response = self._call_provider(
                    text, lifecycle.effective_timeout(None), **kwargs
                )
                response_received = True

                # 4. Check again after the provider returns. The call
                #    may have taken long enough for the run to be
                #    cancelled meanwhile, and delivering a result into
                #    a cancelled run is the effect you were avoiding.
                lifecycle.throw_if_cancelled()

                return self._to_result(response)

            # 5. Normalize every failure. Framework errors pass through
            #    untouched; anything the provider raises is translated
            #    into a SETT error that carries a category and no
            #    provider message, credential or payload.
            except (SETTCancelledError, SETTTimeoutError, SETTAdapterError):
                raise
            except Exception as error:
                raise normalized_adapter_error(
                    error,
                    error_type=SETTServiceAdapterError,
                    provider=_PROVIDER,
                    adapter_name=type(self).__name__,
                    operation="analyze",
                    # A failure after the response arrived is a parsing
                    # problem, not an availability problem: saying so
                    # keeps retry policy from retrying a bad mapping.
                    kind=(
                        AdapterErrorKind.INVALID_RESPONSE
                        if response_received
                        else None
                    ),
                ) from error

    def _call_provider(self, text: str, timeout: float | None, **kwargs: Any) -> Any:
        """
        TODO: this is the ONLY method that may touch your provider.

        Keeping the provider call in one place is what makes the error
        path testable: a copy of this template can subclass it, raise
        whatever the real SDK raises, and assert that analyze() turns
        that into a SETT error without leaking a credential. Spread the
        call across the method above and you lose that seam.

        Pass ``timeout`` through to the SDK: it is what remains of the
        run's deadline, not a constant you choose here.

        The template contacts nothing and fails closed.
        """
        del text, timeout, kwargs
        raise SETTServiceAdapterError(
            "The adapter template has no provider implementation.",
            kind=AdapterErrorKind.CONFIGURATION,
            provider=_PROVIDER,
            adapter_name=type(self).__name__,
            reason_code="adapter.template_not_configured",
        )

    def _to_result(self, response: Any) -> SentimentResult:
        """
        TODO: map the provider's response onto the framework type.

        Report what you measured and nothing else. Deciding what a
        score means (which label, which threshold, which action) is
        application logic, and an adapter that decides it makes itself
        impossible to swap for another provider.

        The template fails closed rather than inventing a measurement
        it never took.
        """
        del response
        raise SETTServiceAdapterError(
            "The adapter template has no result mapping.",
            kind=AdapterErrorKind.CONFIGURATION,
            provider=_PROVIDER,
            adapter_name=type(self).__name__,
            reason_code="adapter.template_not_configured",
        )

    # ``health`` and ``check_health()`` are inherited and already
    # correct: health starts UNKNOWN, adapter_call_scope updates it,
    # and check_health() never performs hidden or billable I/O. Override
    # them only if your provider exposes a real, free status endpoint.

    def __repr__(self) -> str:
        # TODO: keep this free of credentials. A repr reaches logs and
        # debuggers, which is precisely where a key must not appear.
        return f"{type(self).__name__}()"
