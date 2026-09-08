"""
SETT Framework: Ethical Rules
==============================
Defines declared harm categories and metadata, plus the thresholds and
principle used by the EthicalFilter.

Only ``EthicalRuleset.reject_threshold``, ``warn_threshold`` and
``principle`` participate in a verdict. Rules and weights are retained
as configuration metadata for human inspection; they do not enter the
numeric score and are not copied into emitted audit entries. Scoring is
owned by ``ContextAnalyzer``.

Directly inspired by an early prototype's moral_eval.py: generalized into a
reusable ruleset system that any SETT deployment can customize.

The guiding principle: "Do not cause direct or indirect harm to human beings."
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class HarmCategory(Enum):
    """
    Declared harm categories available for ruleset documentation.

    Each category has a default metadata weight. The current numeric
    evaluator does not consume that weight.
    """
    PHYSICAL = "physical"           # Bodily harm to a person
    PSYCHOLOGICAL = "psychological" # Emotional or mental harm
    ECONOMIC = "economic"           # Financial damage
    AUTONOMY = "autonomy"           # Violation of someone's right to decide
    OMISSION = "omission"           # Harm caused by NOT acting when action is needed
    AMBIGUITY = "ambiguity"         # Uncertain or unclear consequences


# Declared harm weights: higher means more serious to a human reader.
# ContextAnalyzer, not this mapping, owns the numeric score.
DEFAULT_HARM_WEIGHTS: dict[HarmCategory, float] = {
    HarmCategory.PHYSICAL: 10.0,
    HarmCategory.PSYCHOLOGICAL: 8.0,
    HarmCategory.ECONOMIC: 6.0,
    HarmCategory.AUTONOMY: 5.0,
    HarmCategory.OMISSION: 4.0,
    HarmCategory.AMBIGUITY: 2.0,
}


@dataclass
class EthicalRule:
    """
    Human-readable ruleset metadata with an inspectable active state.

    ``active`` controls ``EthicalRuleset.get_active_rules()``. It does
    not change the score or verdict.
    """
    name: str
    category: HarmCategory
    weight: float
    description: str
    active: bool = True

    def deactivate(self) -> None:
        """Exclude this rule from ``get_active_rules()``."""
        self.active = False

    def activate(self) -> None:
        """Include this rule in ``get_active_rules()`` again."""
        self.active = True


@dataclass
class EthicalRuleset:
    """
    Verdict thresholds, principle, and inspectable rule metadata.

    Adjust ``reject_threshold`` and ``warn_threshold`` to change verdict
    boundaries. Adding, removing, weighting or toggling ``rules`` does
    not change the numeric evaluation. Subclass or register a
    ``ContextAnalyzer`` when scoring itself has to change.
    """
    name: str
    principle: str = "Do not cause direct or indirect harm to human beings."
    rules: list[EthicalRule] = field(default_factory=list)
    reject_threshold: float = 8.0   # Score at or above this → REJECT action
    warn_threshold: float = 4.0     # Score at or above this → WARN (action logged)

    def add_rule(self, rule: EthicalRule) -> None:
        """Add a rule to this ruleset."""
        self.rules.append(rule)

    def get_active_rules(self) -> list[EthicalRule]:
        """Return only the rules that are currently active."""
        return [r for r in self.rules if r.active]

    def get_rule(self, name: str) -> EthicalRule | None:
        """Find a rule by name."""
        return next((r for r in self.rules if r.name == name), None)


def default_ruleset() -> EthicalRuleset:
    """
    Returns the default SETT ethical ruleset.

    Its metadata covers the six declared harm categories. The filter
    uses its thresholds and principle automatically when no custom
    ruleset is provided; ContextAnalyzer still owns the score.
    """
    ruleset = EthicalRuleset(name="sett_default")

    ruleset.add_rule(EthicalRule(
        name="no_physical_harm",
        category=HarmCategory.PHYSICAL,
        weight=DEFAULT_HARM_WEIGHTS[HarmCategory.PHYSICAL],
        description="Action must not cause or facilitate physical harm to any person.",
    ))

    ruleset.add_rule(EthicalRule(
        name="no_psychological_harm",
        category=HarmCategory.PSYCHOLOGICAL,
        weight=DEFAULT_HARM_WEIGHTS[HarmCategory.PSYCHOLOGICAL],
        description="Action must not cause emotional distress, manipulation, or psychological damage.",
    ))

    ruleset.add_rule(EthicalRule(
        name="no_economic_harm",
        category=HarmCategory.ECONOMIC,
        weight=DEFAULT_HARM_WEIGHTS[HarmCategory.ECONOMIC],
        description="Action must not cause unauthorized financial damage to the user.",
    ))

    ruleset.add_rule(EthicalRule(
        name="respect_autonomy",
        category=HarmCategory.AUTONOMY,
        weight=DEFAULT_HARM_WEIGHTS[HarmCategory.AUTONOMY],
        description="Action must not override the user's right to make their own decisions.",
    ))

    ruleset.add_rule(EthicalRule(
        name="no_harmful_omission",
        category=HarmCategory.OMISSION,
        weight=DEFAULT_HARM_WEIGHTS[HarmCategory.OMISSION],
        description="System must not withhold critical information or action when a person is at risk.",
    ))

    ruleset.add_rule(EthicalRule(
        name="flag_ambiguity",
        category=HarmCategory.AMBIGUITY,
        weight=DEFAULT_HARM_WEIGHTS[HarmCategory.AMBIGUITY],
        description="Uncertain or unclear consequences must be flagged, not ignored.",
    ))

    return ruleset
