"""
SETT Framework: LLM Base Adapter
==============================
Abstract interface that all LLM adapters must implement.

This ensures SETT is not tied to any specific language model.
Swap Claude for GPT, Gemini, or a local model by changing
the adapter: no other code needs to change.

Following the same interchangeability philosophy for TTS/STT in
services_tts_stt/ and sentiment analysis in services_sentiment/.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any


class LLMBase(ABC):
    """
    Abstract base class for all LLM adapters in SETT.

    Any language model must implement this interface to be used
    as the reasoning engine inside a SETTExpert or SETTAgent.

    Implement two methods:
    - complete(): for one-shot prompts (no conversation history)
    - chat(): for multi-turn conversations

    Two reserved keyword arguments (v0.12.0)
    ----------------------------------------
    Every adapter interface in SETT already accepts ``**kwargs``, and two
    names inside it are reserved by the framework rather than by any
    provider:

        timeout             seconds this call may take, at most
        cancellation_token  an observation-only CancellationToken

    Adapters are outside the Agent/Expert tree by design (see the
    conventions doc): they have no memory, no orchestrator registration,
    and no ambient control handed to them the way a component gets one.
    These two kwargs are how the framework's lifecycle reaches them
    anyway, and standardizing the spelling is what keeps them
    interchangeable.

    A custom adapter honours them like this::

        from sett.core_ruler.lifecycle import pop_lifecycle_kwargs

        def complete(self, prompt, system="", **kwargs):
            lifecycle = pop_lifecycle_kwargs(kwargs)   # removes both
            lifecycle.throw_if_cancelled()
            response = self._client.generate(
                prompt=prompt,
                system=system,
                timeout=lifecycle.effective_timeout(self._timeout_seconds),
                **kwargs,
            )
            lifecycle.throw_if_cancelled()
            return response

    Honouring them is strongly encouraged and not enforced: an adapter
    that ignores both still satisfies this interface and still works. What
    it loses is the ability to stop early, and SETT says so plainly rather
    than pretending otherwise - a provider call already in flight is not
    something this framework can take back.

    What an adapter raises
    ----------------------
    An adapter converts its own failures and its provider's failures into
    ``SETTLLMAdapterError`` - misconfiguration at construction included.
    Callers act on that single type: ``PhrasingExpert``, for one, answers
    with ``fallback_text()`` when it sees one, which is what keeps a
    component useful with no model reachable. A provider or transport
    exception that escapes untranslated walks straight through that
    protection, so the safe shape is to translate everything the call can
    raise rather than to enumerate the failures known so far.

    The exception is SETT's own lifecycle exceptions -
    ``SETTCancelledError`` and ``SETTTimeoutError``. Those keep their type
    and propagate: somebody asked for this to stop, or it ran out of the
    time it was given, and neither is the provider failing.

    Example (minimal implementation):
        class MyCustomAdapter(LLMBase):
            @property
            def model_name(self):
                return "my-model-v1"

            def complete(self, prompt, system="", **kwargs):
                return my_api.generate(prompt)

            def chat(self, messages, system="", **kwargs):
                return my_api.chat(messages)
    """

    @abstractmethod
    def complete(self, prompt: str, system: str = "", **kwargs: Any) -> str:
        """
        Generate a single completion without conversation history.
        Use this for one-shot tasks: summarization, classification, extraction.

        Args:
            prompt: The user prompt.
            system: Optional system instruction that sets the model's behavior.
            **kwargs: Model-specific parameters (temperature, max_tokens, etc.)

        Returns:
            The model's text response.
        """
        pass

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, str]],
        system: str = "",
        **kwargs: Any,
    ) -> str:
        """
        Generate a response in a multi-turn conversation.
        Use this for agents that maintain conversation history.

        Args:
            messages: List of {"role": "user"|"assistant", "content": str}.
            system: Optional system instruction.
            **kwargs: Model-specific parameters.

        Returns:
            The model's text response.
        """
        pass

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Return the name or identifier of the underlying model."""
        pass
