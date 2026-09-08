"""Keep templates/phrasing_expert_template.py honest about its contract.

The template's whole claim is that deterministic logic owns the facts
and the LLM only owns the wording. These tests hold it to that claim
against the live base class: the facts must be identical with an LLM,
without one, and when the provider misbehaves in each of the ways a
real one does.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from sett.core_ruler.phrasing_expert import PhrasingExpert
from sett.exceptions import SETTLLMAdapterError
from sett.services_llm.base import LLMBase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "templates"))

from phrasing_expert_template import MyPhrasingExpert

_CONTEXT = {"items": ["a", "b", "c"]}
_FACTS = {"item_count": 3, "has_items": True}


class _ScriptedLLM(LLMBase):
    """An LLM that says exactly what the test tells it to say."""

    def __init__(self, reply: str | BaseException) -> None:
        self._reply = reply
        self.prompts: list[str] = []

    def complete(self, prompt: str, system: str = "", **kwargs: Any) -> str:
        self.prompts.append(prompt)
        if isinstance(self._reply, BaseException):
            raise self._reply
        return self._reply

    def chat(self, messages: list[dict[str, str]], system: str = "", **kw: Any) -> str:
        raise NotImplementedError

    @property
    def model_name(self) -> str:
        return "scripted"


def test_template_runs_with_no_llm_configured() -> None:
    result = MyPhrasingExpert(name="my_phrasing").resolve(_CONTEXT)
    assert result == {**_FACTS, "message": "You have 3 pending items."}


def test_template_does_not_override_the_template_method() -> None:
    # The base class warns at construction when resolve() is overridden,
    # because that is how the facts/wording separation gets bypassed.
    assert MyPhrasingExpert.resolve is PhrasingExpert.resolve


def test_facts_are_identical_with_and_without_an_llm() -> None:
    without = MyPhrasingExpert(name="a").resolve(_CONTEXT)
    with_llm = MyPhrasingExpert(
        name="b", llm=_ScriptedLLM("Three things are waiting: 3 of them.")
    ).resolve(_CONTEXT)
    assert {k: v for k, v in without.items() if k != "message"} == _FACTS
    assert {k: v for k, v in with_llm.items() if k != "message"} == _FACTS


def test_the_llm_changes_only_the_wording() -> None:
    llm = _ScriptedLLM("Just 3 left to go.")
    result = MyPhrasingExpert(name="c", llm=llm).resolve(_CONTEXT)
    assert result["message"] == "Just 3 left to go."
    assert result["item_count"] == 3


def test_the_prompt_carries_facts_and_not_raw_context() -> None:
    # The model is handed the computed count, never the raw entries it
    # was derived from: unprocessed input in a prompt is how a model
    # ends up reporting something nobody computed.
    llm = _ScriptedLLM("3 pending.")
    context = {"items": ["renew passport", "call the bank"]}
    MyPhrasingExpert(name="d", llm=llm).resolve(context)
    prompt = llm.prompts[0]
    assert "2" in prompt
    assert "3" not in prompt
    assert all(entry not in prompt for entry in context["items"])


def test_verification_replaces_a_reply_that_dropped_the_number() -> None:
    llm = _ScriptedLLM("You have a few things pending.")
    result = MyPhrasingExpert(name="e", llm=llm).resolve(_CONTEXT)
    assert result["message"] == "You have 3 pending items."


def test_verification_rejects_a_different_number_containing_the_right_digit() -> None:
    llm = _ScriptedLLM("You have 13 things pending.")
    result = MyPhrasingExpert(name="e2", llm=llm).resolve(_CONTEXT)
    assert result["message"] == "You have 3 pending items."


def test_verification_does_not_reject_its_own_fallback() -> None:
    # The zero case carries no digit on purpose; a number rule applied
    # to it would discard correct output.
    result = MyPhrasingExpert(name="f").resolve({"items": []})
    assert result["message"] == "You have nothing pending."


def test_a_failing_provider_falls_back_instead_of_raising() -> None:
    llm = _ScriptedLLM(SETTLLMAdapterError("provider is down"))
    result = MyPhrasingExpert(name="g", llm=llm).resolve(_CONTEXT)
    assert result["message"] == "You have 3 pending items."


def test_an_empty_reply_falls_back() -> None:
    result = MyPhrasingExpert(name="h", llm=_ScriptedLLM("   ")).resolve(_CONTEXT)
    assert result["message"] == "You have 3 pending items."


def test_singular_and_plural_are_deterministic() -> None:
    one = MyPhrasingExpert(name="i").resolve({"items": ["only"]})
    assert one["message"] == "You have 1 pending item."
