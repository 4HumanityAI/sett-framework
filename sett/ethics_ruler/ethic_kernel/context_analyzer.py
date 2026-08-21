"""
SETT Framework: ContextAnalyzer
==============================
Evaluates the context of a proposed action using all three layers
of the SETT hybrid risk system:

  Layer 1 (Action)      : What is about to happen?
  Layer 2 (User)        : Who is this person right now? (RiskProfile)
  Layer 3 (Environment) : What is the state of the space around them?
                           (EnvironmentalContext)

The ContextAnalyzer answers WHY we arrived at a potential action,
what the emotional and situational context is, and what the consequences
would be. It always prioritizes the protection of the human.

In a real deployment, a sentiment-analysis agent would feed the RiskProfile here.
Wearable sensors feed the health data.
The EnvironmentalContext is read from UniversalMemory.
"""
from __future__ import annotations

import math
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

from sett.exceptions import SETTValidationError

if TYPE_CHECKING:
    from sett.risk_ruler.environmental_context import EnvironmentalContext
    from sett.risk_ruler.risk_level import RiskLevel
    from sett.risk_ruler.risk_profile import RiskProfile


@dataclass(frozen=True, slots=True)
class SafetyAssessment:
    """Separates the situation from the proposed action.

    ``situation_urgency`` answers how serious the human context is.
    ``action_harm_risk`` estimates harm caused by the proposed action.
    ``omission_risk`` estimates harm caused by not acting.
    ``protective_action`` marks actions intended to reduce that omission risk.

    The EthicalFilter's verdict is still based on ``ContextAnalysis.risk_score``;
    domain analyzers decide how these dimensions should affect that score. This
    prevents the framework from assuming that a severe situation automatically
    makes a protective action dangerous.
    """

    situation_urgency: float = 0.0
    action_harm_risk: float = 0.0
    omission_risk: float = 0.0
    protective_action: bool = False

    def __post_init__(self) -> None:
        for field_name in ("situation_urgency", "action_harm_risk", "omission_risk"):
            value = getattr(self, field_name)
            object.__setattr__(self, field_name, max(0.0, min(10.0, float(value))))


def canonicalize_for_keyword_match(text: str) -> str:
    """Put ``text`` in the one spelling this analyzer matches keywords against.

    Three steps, in this order:

    1. Drop every character of Unicode category ``Cf`` (format characters:
       zero-width space, zero-width joiner, soft hyphen, the bidi
       overrides). They are invisible, so ``"ha\\u200brm"`` reads as
       ``harm`` to a human while ``"harm" in text`` answers False.
    2. Normalize to ``NFKC``, which folds compatibility forms - fullwidth
       ``ｄｅｌｅｔｅ``, the ligature ``ﬁ``, superscripts - onto their
       ordinary spellings.
    3. ``casefold()`` rather than ``lower()``, which is the case-insensitive
       comparison Unicode actually defines.

    What this does NOT do, stated plainly rather than left to be
    discovered: it does not defeat homoglyphs. Cyrillic ``а`` (U+0430) is
    a different letter from Latin ``a``, not a different spelling of it,
    and NFKC correctly leaves it alone. ``"hаrm"`` written that way still
    misses the keyword. A homoglyph table is not the answer either -
    every hand-maintained one is incomplete, and an incomplete one
    produces confidence instead of safety.

    The answer is that this matcher is not where a critical action is
    decided; see ``ContextAnalyzer`` for what is.
    """
    without_format_chars = "".join(
        ch for ch in text if unicodedata.category(ch) != "Cf"
    )
    return unicodedata.normalize("NFKC", without_format_chars).casefold()


def _validated_risk_score(score: object) -> float:
    """Reject a harm score that cannot be compared against a threshold.

    Every verdict in SETT comes from comparing this number against the
    ruleset's thresholds, and Python answers ``float("nan") >= anything``
    with ``False``. A single non-finite score therefore clears the reject
    threshold *and* the warn threshold and arrives at ALLOW, a fail-open
    in the one layer this framework exists to make fail-closed. An
    analyzer can produce one without any bad intent: a division by a
    count that turned out to be zero, a model returning an unexpected
    value, a corrupt input.

    Validated here rather than in ``EthicalFilter`` because this is the
    one type every analyzer must return, so it is the only chokepoint
    that cannot be bypassed by a custom analyzer; validating in both
    places would create two rules that have to agree, which is the drift
    this project has already had to remove once.

    No upper bound is imposed: the scale belongs to the ruleset, whose
    thresholds an application configures, and inventing a ceiling here
    would silently cap scores that a stricter ruleset means to act on.
    """
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise SETTValidationError(
            "ContextAnalysis.risk_score must be a real number, got "
            f"{type(score).__name__}. A harm score that cannot be compared "
            "against a threshold cannot produce a verdict."
        )
    if not math.isfinite(score):
        raise SETTValidationError(
            "ContextAnalysis.risk_score must be finite. A NaN or infinite "
            "score compares False against every threshold, which would "
            "reach ALLOW without any rule having allowed it."
        )
    if score < 0:
        raise SETTValidationError(
            "ContextAnalysis.risk_score must not be negative; harm is "
            "measured from zero upward."
        )
    return float(score)


class ContextAnalysis:
    """Result of a full three-layer context analysis."""

    def __init__(
        self,
        action: str,
        risk_score: float,
        emotional_state: str,
        reasoning: str,
        consequences: list[str],
        human_at_risk: bool = False,
        risk_level: RiskLevel | None = None,
        safety_assessment: SafetyAssessment | None = None,
    ) -> None:
        """Hold one analysis; refuses a risk_score no threshold can compare."""
        self.action = action
        self.risk_score = _validated_risk_score(risk_score)
        self.emotional_state = emotional_state
        self.reasoning = reasoning
        self.consequences = consequences
        self.human_at_risk = human_at_risk
        self.risk_level = risk_level       # environmental layer result
        self.safety_assessment = safety_assessment or SafetyAssessment(
            situation_urgency=risk_score if human_at_risk else 0.0,
            action_harm_risk=risk_score,
            omission_risk=0.0,
            protective_action=False,
        )
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def __repr__(self) -> str:
        return (
            f"ContextAnalysis(action={self.action!r}, "
            f"risk_score={self.risk_score:.2f}, "
            f"human_at_risk={self.human_at_risk}, "
            f"env_level={self.risk_level})"
        )


class ContextAnalyzer:
    """
    Three-layer context analyzer for the EthicalFilter.

    Subclass this and override analyze() to integrate with:
    - The SentimentAnalyzerAgent (emotional state + RiskProfile)
    - Wearable biometric data (health indicators)
    - UniversalMemory (EnvironmentalContext for the user's location)

    What this generic analyzer is, and is not
    -----------------------------------------
    Layer 1 here is a keyword heuristic. It exists so that an application
    that has registered no analyzer of its own still gets a non-zero,
    roughly-right score instead of silence, following the same fail-honest
    scaffolding idea as ``StubDomainAgent``. It is a floor, not a policy.

    It is deliberately not the thing standing between a critical action
    and its effect. Three other things are, and they are structural
    rather than textual:

    - ``SETTExecutor`` resolves a handler by exact ``action_type`` and
      fails closed when there is no exact match, so an action type this
      matcher failed to recognise has no handler to reach either.
    - A domain analyzer registered with ``register_analyzer()`` decides
      from the structured fields of its own domain, not from prose.
    - The ruleset's thresholds and the audit trail are what a deployment
      actually tunes and reviews.

    Text arriving here is canonicalized first (see
    ``canonicalize_for_keyword_match``), which closes the invisible and
    compatibility-form spellings. It does not close homoglyphs, and no
    keyword list can. An operation whose consequences you would not
    accept being under-scored needs a domain analyzer, which is the
    supported way to say so.
    """

    # Emotional state → base risk modifier
    EMOTIONAL_RISK_MODIFIERS: ClassVar[Mapping[str, float]] = MappingProxyType(
        {
            "calm": 0.0,
            "happy": 0.0,
            "neutral": 0.0,
            "sad": 1.0,
            "anxious": 1.5,
            "angry": 2.0,
            "confused": 1.5,
            "distressed": 3.0,
            "crisis": 5.0,
            "unknown": 0.5,
        }
    )

    def analyze(
        self,
        action: str,
        context: dict[str, Any],
        emotional_state: str = "unknown",
        risk_profile: RiskProfile | None = None,
        environmental_context: EnvironmentalContext | None = None,
    ) -> ContextAnalysis:
        """
        Full three-layer analysis of a proposed action.

        Layer 1: Action analysis: what keywords and data does this action carry?
        Layer 2: User analysis: what does the RiskProfile say about this person now?
        Layer 3: Environment: what is the EnvironmentalContext of the space?

        Args:
            action: Description of the action about to be executed.
            context: Data associated with this action.
            emotional_state: Detected emotional state (from Sentiment Analyzer).
            risk_profile: Three-pillar user assessment (from private memory).
            environmental_context: Shared environmental state (from universal memory).

        Returns:
            A ContextAnalysis with the combined risk assessment.
        """

        # ── Layer 1: Action base score ────────────────────────────────────────
        base_score = self._base_risk_score(action, context)

        # ── Layer 2: User emotional state modifier ───────────────────────────
        emotional_modifier = self.EMOTIONAL_RISK_MODIFIERS.get(
            canonicalize_for_keyword_match(emotional_state), 0.5
        )

        # ── Layer 2: RiskProfile modifier ────────────────────────────────────
        profile_modifier = 0.0
        if risk_profile is not None:
            # Composite score (0.0–1.0) scales to a modifier (0.0–3.0)
            profile_modifier = risk_profile.composite_score * 3.0

        # ── Layer 3: Environmental context modifier ──────────────────────────
        env_modifier = 0.0
        env_level = None
        if environmental_context is not None:
            env_modifier = environmental_context.filter_threshold_modifier
            env_level = environmental_context.risk_level

        # ── Combine all layers ────────────────────────────────────────────────
        combined_score = min(
            10.0,
            base_score + emotional_modifier + profile_modifier + env_modifier
        )

        # ── Situation urgency is distinct from action harm ───────────────────
        human_at_risk = self._detect_human_at_risk(
            context, combined_score, risk_profile, environmental_context
        )
        biometric_urgency = 4.0 if human_at_risk else 0.0
        situation_urgency = min(
            10.0, emotional_modifier + profile_modifier + env_modifier + biometric_urgency
        )
        safety_assessment = SafetyAssessment(
            situation_urgency=situation_urgency,
            action_harm_risk=combined_score,
            omission_risk=0.0,
            protective_action=False,
        )

        consequences = self._assess_consequences(action, context, environmental_context)
        reasoning = self._build_reasoning(
            action, base_score, emotional_modifier,
            profile_modifier, env_modifier, emotional_state,
            risk_profile, env_level,
        )

        return ContextAnalysis(
            action=action,
            risk_score=combined_score,
            emotional_state=emotional_state,
            reasoning=reasoning,
            consequences=consequences,
            human_at_risk=human_at_risk,
            risk_level=env_level,
            safety_assessment=safety_assessment,
        )

    def _base_risk_score(self, action: str, context: dict[str, Any]) -> float:
        """Layer 1: keyword-based base risk from the action itself.

        A fallback heuristic, not a policy: see the class docstring for
        what actually guards a critical action. Everything matched here
        goes through ``canonicalize_for_keyword_match`` first.
        """
        score = 0.0
        # A governed universal-memory write may describe the real proposed
        # operation in an explicit field. Inspect only those structured fields,
        # never arbitrary user text, to avoid turning ordinary content into an
        # accidental action classification.
        action_parts = [action]
        for key in ("proposed_action", "action_type"):
            value = context.get(key)
            if isinstance(value, str):
                action_parts.append(value)
        action_lower = canonicalize_for_keyword_match(" ".join(action_parts))

        high_risk_kw = [
            "emergency", "danger", "harm", "delete", "send_all",
            "medical", "crisis", "alert", "panic", "call_911",
        ]
        medium_risk_kw = [
            "send", "share", "publish", "contact", "notify", "execute",
        ]

        for kw in high_risk_kw:
            if kw in action_lower:
                score += 3.0
                break
        for kw in medium_risk_kw:
            if kw in action_lower:
                score += 1.0
                break

        return min(score, 10.0)

    def _detect_human_at_risk(
        self,
        context: dict[str, Any],
        risk_score: float,
        risk_profile: RiskProfile | None,
        environmental_context: EnvironmentalContext | None,
    ) -> bool:
        """
        Determines if a human is in immediate risk.
        Combines score threshold, biometrics, RiskProfile, and EnvironmentalContext.

        Since v0.11.0: biometric parsing (nested "health" dict vs. flat
        context, vital-sign thresholds) moved to
        biometric_ruler.BiometricReading: this method no longer reads
        context["health"]/context["heart_rate_bpm"] itself. Behavior is
        unchanged (same thresholds, same nested/flat fallback introduced
        in v0.1.1); only the parsing moved to a typed data model instead
        of living inline as dict access here. See Convention #23.
        """
        from sett.biometric_ruler.biometric_reading import BiometricReading

        # High combined score
        if risk_score >= 7.0:
            return True

        # Biometric indicators (only if data is actually present).
        if BiometricReading.from_context(context).is_critical:
            return True

        # RiskProfile: high emotional instability alone is a signal
        if (
            risk_profile is not None
            and risk_profile.emotional_instability >= 0.85
        ):
            return True

        # Environmental context: level 4+ means humans are at risk by definition
        return (
            environmental_context is not None
            and environmental_context.risk_level.is_critical()
        )

    def _assess_consequences(
        self,
        action: str,
        context: dict[str, Any],
        environmental_context: EnvironmentalContext | None = None,
    ) -> list[str]:
        """Build a list of possible consequences for this action."""
        consequences = []
        action_lower = canonicalize_for_keyword_match(action)

        if "send" in action_lower:
            consequences.append("Information will be transmitted to an external party.")
        if "memory_write" in action_lower:
            consequences.append("State will be persisted in universal memory.")
        if "emergency" in action_lower or "911" in action_lower:
            consequences.append("Emergency services will be contacted.")
            consequences.append("User location and health data may be shared.")

        if environmental_context and environmental_context.requires_response:
            consequences.append(
                f"Action occurs in a {environmental_context.risk_level.label} "
                f"environment: consequences may be amplified."
            )

        if not consequences:
            consequences.append("No significant external consequences identified.")

        return consequences

    def _build_reasoning(
        self,
        action: str,
        base_score: float,
        emotional_modifier: float,
        profile_modifier: float,
        env_modifier: float,
        emotional_state: str,
        risk_profile: RiskProfile | None,
        env_level: RiskLevel | None,
    ) -> str:
        """Build a human-readable explanation of the full analysis."""
        parts = [
            f"Action '{action}': base score {base_score:.1f}",
            f"emotional state '{emotional_state}' (+{emotional_modifier:.1f})",
        ]
        if profile_modifier > 0:
            dominant = risk_profile.dominant_pillar if risk_profile else "unknown"
            parts.append(f"risk profile (+{profile_modifier:.1f}, dominant: {dominant})")
        if env_modifier > 0 and env_level is not None:
            parts.append(f"environment {env_level.emoji} {env_level.label} (+{env_modifier:.1f})")
        parts.append("Human protection always prioritized.")
        return ". ".join(parts) + "."
