"""
templates/phrasing_expert_template.py
==============================
Copy this file, rename it, and fill in the four parts marked TODO.

Use this instead of expert_template.py whenever the expert's job
includes producing text a human will read or hear: a greeting, an
acknowledgment, a summary, an alert. For an expert whose result is
consumed by other code and never shown to anyone, expert_template.py
is the right starting point.

The one rule this class exists to enforce
-----------------------------------------
Deterministic logic decides WHAT IS TRUE. The LLM only decides HOW IT
SOUNDS. It never invents a fact, never alters one, never chooses which
facts to report.

That separation is what makes the LLM optional rather than required:
this file, as it is, runs and produces correct text with no LLM
configured at all. Wire one in later and the same expert says the same
things more naturally. If a provider is down, over quota, or simply
never configured, the user still gets an answer.

This file is real code: it can be instantiated and run without filling
in anything. If something fails right after copying it, the problem is
with your import, not the template.

The four hooks
--------------
Implement these; do NOT override ``resolve()``. It is a template method
that calls the four in order, and the base class emits a warning at
construction time if a subclass overrides it, because bypassing it
silently drops the guarantee above.

    determine_facts()  what is true, computed without the LLM
    build_prompt()     what the LLM should say about those facts
    fallback_text()    the deterministic text, used with no LLM
    verify_facts()     optional: check what the LLM actually said

Verifying your copy
-------------------
    expert = MyPhrasingExpert(name="my_phrasing")
    print(expert.resolve({"items": ["a", "b"]}))

See ``docs/api_reference.md`` for the full contract and
``test_phrasing_expert_template.py`` for the behaviors worth keeping
when you adapt this.
"""
from __future__ import annotations

import re
from typing import Any

from sett.core_ruler.phrasing_expert import PhrasingExpert


# TODO: rename the class after the message it produces
# (e.g. GreetingExpert, BudgetSummaryExpert, AlertPhrasingExpert)
class MyPhrasingExpert(PhrasingExpert):
    """
    TODO: one line describing WHAT this expert tells the user.
    Example: "Reports how many pending items the user has left."
    """

    # TODO: rename to match what this expert produces. The phrased text
    # is merged into the facts dict under this key, so the owning agent
    # reads it by this name (e.g. "greeting", "summary", "alert").
    OUTPUT_KEY = "message"

    # TODO (optional): override SYSTEM_PROMPT if this expert needs a
    # different voice from the framework default. Keep the instruction
    # never to state anything as fact that was not supplied.

    def determine_facts(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        TODO: compute what is TRUE here, with no LLM involved.

        Everything the user will be told must originate in this method.
        If a number, a name or a status is not in the dict you return,
        the LLM has no business mentioning it.

        Args:
            context: the data the owning agent passed in.

        Returns:
            The facts, as a plain dict. They are returned to the agent
            alongside the phrased text, so an agent that needs the raw
            values does not have to parse them back out of a sentence.
        """
        # TODO: replace with your own deterministic logic.
        items = context.get("items") or []
        return {"item_count": len(items), "has_items": bool(items)}

    def build_prompt(self, facts: dict[str, Any], context: dict[str, Any]) -> str:
        """
        TODO: describe what to say, using only the facts.

        Never interpolate raw ``context`` into the prompt. The context
        is unprocessed input, and handing it over reads to the model as
        licence to work from it directly, which is exactly how invented
        facts get in. Pass the facts you computed, and nothing else.

        Args:
            facts: what determine_facts() returned.
            context: available for routing decisions (language, channel),
                not as prompt material.

        Returns:
            The prompt for the LLM.
        """
        # TODO: replace with your own instruction.
        return (
            "Tell the user they have "
            f"{facts['item_count']} pending item(s). "
            "State the number exactly as given. One short sentence."
        )

    def fallback_text(self, facts: dict[str, Any], context: dict[str, Any]) -> str:
        """
        TODO: write the deterministic sentence.

        This is not an error message and not a degraded mode: it is what
        the user gets when no LLM is configured, when the provider
        fails, when it returns nothing, and whenever verify_facts()
        rejects what it said. Write it as the real answer, because most
        of the time it is.

        Args:
            facts: what determine_facts() returned.
            context: the original input.

        Returns:
            A complete, correct sentence built only from the facts.
        """
        # TODO: replace with your own deterministic text.
        if not facts["has_items"]:
            return "You have nothing pending."
        if facts["item_count"] == 1:
            return "You have 1 pending item."
        return f"You have {facts['item_count']} pending items."

    def verify_facts(
        self, phrased: str, facts: dict[str, Any], context: dict[str, Any]
    ) -> str:
        """
        TODO (optional): check what the model actually SAID.

        The LLM call succeeding is not the same as the LLM being right.
        A model can return a perfectly well-formed sentence that agrees
        with the user's wrong assumption instead of the fact it was
        handed. This hook runs after the text exists, from the LLM or
        from fallback_text(), and can replace it.

        Keep the check closed and deterministic: pattern matching
        against facts you already hold. If it needs judgement, it is not
        a verification, it is a second opinion, and it belongs nowhere
        near here.

        Delete this method entirely if your expert does not need it: the
        base class default returns ``phrased`` unchanged.

        Args:
            phrased: the text produced so far.
            facts: what determine_facts() returned.
            context: the original input.

        Returns:
            ``phrased`` when it holds up, or a replacement when it does
            not (usually ``self.fallback_text(facts, context)``).
        """
        # TODO: replace with a check that matters for your facts. This
        # one refuses any phrasing that dropped or changed the number,
        # because the count is what the message exists to deliver.
        #
        # Note the guard on has_items. A check that would reject your own
        # fallback_text() is a bug in the check, not in the text: here
        # the zero case is phrased as "nothing pending" and carries no
        # digit, so applying the number rule to it would discard correct
        # output. Test every verification against your own fallback.
        stated_numbers = set(re.findall(r"\d+", phrased))
        if facts["has_items"] and str(facts["item_count"]) not in stated_numbers:
            return self.fallback_text(facts, context)
        return phrased
