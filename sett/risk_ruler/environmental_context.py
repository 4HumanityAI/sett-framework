"""
SETT Framework: EnvironmentalContext
==============================
The bridge between the user layer (RiskProfile) and the shared environment.

An EnvironmentalContext is what a SETT instance publishes to a shared space
when it detects that the environment around it requires attention.
Other SETT instances in the same location can read this and adjust their behavior.

This is the mechanism that enables the "warehouse scenario":
    - Instance A detects a user with elevated risk profile
    - A publishes EnvironmentalContext(level=LEVEL_4, location="store_42")
    - Instances B, C, D in the same location read this
    - Each instance advises its own user to act accordingly
    - Emergency services are notified if level >= LEVEL_4

Privacy design:
    EnvironmentalContext is SHAPED to carry only non-personal signals:
    a RiskLevel, a location identifier, the domain of the publishing
    agent, a timestamp, and an optional free-text `message`. Nothing
    about the object's structure requires personal data.

    This is a structural intent, not a runtime guarantee (F11,
    defensive audit #2): `location_id`, `source_domain`, and `message`
    are plain strings, and nothing in this class inspects or rejects
    their content. The caller building an EnvironmentalContext is
    responsible for keeping personal data out of it; in particular,
    do not put names, biometrics, or other identifiers into `message`.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sett.risk_ruler.risk_level import RiskLevel


@dataclass
class EnvironmentalContext:
    """
    Shared environmental state published by a SETT instance
    and readable by all instances in the same location.

    This is the SETT mechanism for multi-instance coordination:

        Instance A (detects risk) ──publishes──> UniversalMemory[location_id]
        Instance B (same location) ──reads──> adjusts own behavior
        Instance C (same location) ──reads──> adjusts own behavior

    The RiskLevel in this context is the environment's level,
    derived from (but not exposing) the user's RiskProfile.

    Usage:
        orchestrator.publish_environmental_context(
            risk_level=RiskLevel.LEVEL_3,
            location_id="store_42",
            source_domain="health",
        )
        # publish_environmental_context() builds the EnvironmentalContext
        # itself from these fields (F25, defensive audit #2: this example
        # previously constructed the object by hand and passed it in, but
        # the Orchestrator's real signature takes the raw fields, not a
        # ready-made EnvironmentalContext, since that call belongs to
        # UniversalMemory.publish_environmental_context(context), not this
        # one).

        # From another instance:
        ctx = orchestrator.read_environmental_context("store_42")
        if ctx and ctx.requires_response:
            agent.adjust_behavior(ctx.risk_level)
    """

    risk_level: RiskLevel
    """The current risk level of this environment."""

    location_id: str = "global"
    """
    Identifier of the shared space. Can be:
    - A physical location ID ("store_42", "hospital_ward_3")
    - A logical group ID ("family_group_123", "workplace_456")
    - "global" for system-wide alerts
    """

    source_domain: str = "unknown"
    """
    The domain of the agent that published this context.
    Example: "health", "environment", "emergency".
    Never contains user identification.
    """

    message: str = ""
    """
    Optional human-readable description of why this level was triggered.
    This is a free-text field: nothing in EnvironmentalContext inspects
    or filters its content (F11, defensive audit #2). The caller
    constructing this context is responsible for keeping it free of
    personal information; it is broadcast to every other instance
    reading this location's environmental context.
    Example: "Elevated biometric indicators detected in environment."
    """

    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    should_notify_emergency: bool = field(init=False)
    """
    True when this context's risk level (LEVEL_4 or LEVEL_5) warrants
    notifying emergency services.

    This is a recommendation derived from risk_level, not an effect:
    setting this field never contacts anyone (F20, defensive audit #2 -
    renamed from `auto_notify_emergency`, whose name implied an
    automatic side effect that no code in this framework performs). A
    caller that wants the real notification to happen must submit it
    as an explicit, auditable Action through SETTExecutor.
    """

    def __post_init__(self) -> None:
        self.should_notify_emergency = self.risk_level.is_critical()

    @property
    def requires_response(self) -> bool:
        """True if this context requires any active response (level >= 2)."""
        return self.risk_level >= RiskLevel.LEVEL_2

    @property
    def requires_evacuation(self) -> bool:
        """True if evacuation or immediate action is required (level >= 4)."""
        return self.risk_level >= RiskLevel.LEVEL_4

    @property
    def is_systemic_emergency(self) -> bool:
        """True only for LEVEL_5: maximum protocol."""
        return self.risk_level == RiskLevel.LEVEL_5

    @property
    def filter_threshold_modifier(self) -> float:
        """
        How much this environmental context should tighten the EthicalFilter.
        Higher RiskLevel → lower thresholds → filter becomes more strict.

        Returns a non-negative float that the filter SUBTRACTS from its
        thresholds, so a larger number means stricter. (This line used to
        say "negative", which described neither the values below nor the
        subtraction the filter performs.)
        At LEVEL_0: no change (0.0)
        At LEVEL_5: thresholds drop by 4.0 (very strict)
        """
        modifiers = {
            RiskLevel.LEVEL_0: 0.0,
            RiskLevel.LEVEL_1: 0.5,
            RiskLevel.LEVEL_2: 1.0,
            RiskLevel.LEVEL_3: 2.0,
            RiskLevel.LEVEL_4: 3.0,
            RiskLevel.LEVEL_5: 4.0,
        }
        return modifiers[self.risk_level]

    def to_dict(self) -> dict[str, Any]:
        """Serialize for storage in UniversalMemory."""
        return {
            "risk_level": self.risk_level.value,
            "risk_label": self.risk_level.label,
            "risk_emoji": self.risk_level.emoji,
            "location_id": self.location_id,
            "source_domain": self.source_domain,
            "message": self.message,
            "timestamp": self.timestamp,
            "requires_response": self.requires_response,
            "requires_evacuation": self.requires_evacuation,
            "should_notify_emergency": self.should_notify_emergency,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EnvironmentalContext":
        """Reconstruct from UniversalMemory storage.

        F03 fix (defensive audit #2): ``timestamp`` used to be dropped
        here even though ``to_dict()`` always serializes it, so every
        read through UniversalMemory silently replaced the real publish
        time with the moment it happened to be read, the same class of
        bug as RiskProfile.from_dict()'s own timestamp loss (F12).
        """
        kwargs: dict[str, Any] = {
            "risk_level": RiskLevel(data["risk_level"]),
            "location_id": data.get("location_id", "global"),
            "source_domain": data.get("source_domain", "unknown"),
            "message": data.get("message", ""),
        }
        timestamp = data.get("timestamp")
        if timestamp is not None:
            kwargs["timestamp"] = timestamp
        return cls(**kwargs)

    @classmethod
    def normal(cls, location_id: str = "global") -> "EnvironmentalContext":
        """Returns a baseline normal context for a location."""
        return cls(
            risk_level=RiskLevel.LEVEL_0,
            location_id=location_id,
            source_domain="system",
            message="Normal operating conditions.",
        )

    def __str__(self) -> str:
        return (
            f"EnvironmentalContext("
            f"{self.risk_level.emoji} {self.risk_level.label}, "
            f"location={self.location_id!r}, "
            f"source={self.source_domain!r})"
        )
