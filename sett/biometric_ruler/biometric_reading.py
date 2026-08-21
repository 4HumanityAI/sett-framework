"""
SETT Framework: BiometricReading
==============================
Structural data model for physical vital-sign readings, same role in
biometric_ruler that RiskProfile/EnvironmentalContext play in
risk_ruler: a typed value object that ethics_ruler consumes, not a
component that decides anything by itself.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar

from sett.exceptions import SETTValidationError


@dataclass(frozen=True)
class BiometricReading:
    """
    A single point-in-time snapshot of biometric/vital-sign data.

    Usage:
        reading = BiometricReading.from_context(context)
        if reading.is_critical:
            ...

    All fields are optional: a reading with no data present is valid
    and simply never critical (`is_critical` is False when every field
    is None), matching the original behavior in
    ContextAnalyzer._detect_human_at_risk: absence of biometric data is
    not itself a risk signal.
    """

    heart_rate_bpm: float | None = None
    temperature_celsius: float | None = None

    HEART_RATE_MAX_BPM: ClassVar[float] = 150
    HEART_RATE_MIN_BPM: ClassVar[float] = 40
    TEMPERATURE_MAX_C: ClassVar[float] = 39.5
    TEMPERATURE_MIN_C: ClassVar[float] = 35.0
    """
    Thresholds unchanged from the values ContextAnalyzer._detect_human_at_risk
    used inline before this extraction (v0.1.1 through v0.6.0): this
    refactor relocates them, it does not recalibrate them. Recalibrating
    is a separate, deliberate decision for someone with the clinical
    basis to make it.
    """

    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        repr=False,
    )

    def __post_init__(self) -> None:
        """F14 fix (defensive audit #2).

        A mistyped vital sign (a string, a list, anything not a real
        number) used to construct successfully and only fail later, with
        a bare ``TypeError`` from the ``>``/``<`` comparisons inside
        ``is_critical``, raised far from where the bad value actually
        came from, and not the structured exception type the rest of
        this framework raises for invalid data. Validating here fails at
        construction instead, with a message that names the field.
        """
        for name in ("heart_rate_bpm", "temperature_celsius"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise SETTValidationError(
                    f"BiometricReading.{name} must be a number or None, "
                    f"got {type(value).__name__}."
                )
            if not math.isfinite(value):
                raise SETTValidationError(
                    f"BiometricReading.{name} must be finite, got {value!r}."
                )

    @property
    def is_critical(self) -> bool:
        """
        True if any present vital sign is outside the safe range.
        Mirrors exactly the two checks _detect_human_at_risk performed
        inline: heart rate outside (40, 150) bpm, or temperature
        outside (35.0, 39.5) °C. A field that is None is never
        evaluated: missing data is not itself a risk signal.
        """
        if self.heart_rate_bpm is not None and (
            self.heart_rate_bpm > self.HEART_RATE_MAX_BPM
            or self.heart_rate_bpm < self.HEART_RATE_MIN_BPM
        ):
            return True
        return self.temperature_celsius is not None and (
            self.temperature_celsius > self.TEMPERATURE_MAX_C
            or self.temperature_celsius < self.TEMPERATURE_MIN_C
        )

    @classmethod
    def from_context(cls, context: dict[str, Any]) -> BiometricReading:
        """
        Parse a BiometricReading out of an action's context dict.

        Reproduces exactly the v0.1.1 nested/flat fallback that lived
        inline in ContextAnalyzer._detect_human_at_risk before this
        extraction: prefer a nested "health" dict if present and
        non-empty, otherwise read the same keys directly from the
        top-level context. Centralizing this parsing in one place is
        the actual fix for the class of bug v0.1.1 patched: v0.1.1
        fixed the one call site that existed then; without a single
        shared parser, a second call site added later could silently
        reintroduce the identical bug by guessing the shape differently.

        A context with no usable health data, whether missing, empty, or not a
        mapping at all, returns a BiometricReading with both fields
        None, which is never critical. That is the absent case, and it is
        deliberately not an error: a system without a wearable attached
        must still run.

        A value that is present but not a number is a different case and
        does raise SETTValidationError. "fast" where a heart rate belongs
        is a bug in whatever produced it, and silently reading it as
        "no data" would turn a broken sensor into a healthy one, the
        opposite of what this class is for. (An earlier version of this
        docstring said "never raises", which stopped being true when that
        validation was added.)
        """
        health_data = context.get("health", {})
        if not (isinstance(health_data, dict) and health_data):
            health_data = context

        if not isinstance(health_data, dict):
            return cls()

        return cls(
            heart_rate_bpm=health_data.get("heart_rate_bpm"),
            temperature_celsius=health_data.get("temperature_celsius"),
        )

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize for logging/storage. Deliberately does NOT include
        raw vital-sign values by default in any audit-log path this
        framework writes to: ethics_ruler only ever reads
        `is_critical` (a bool) from this class, never the raw numbers,
        so a privacy leak of biometric data into UniversalMemory or the
        audit log is structurally impossible via that path, same
        guarantee RiskProfile already has for its own pillars.
        """
        return {
            "heart_rate_bpm": self.heart_rate_bpm,
            "temperature_celsius": self.temperature_celsius,
            "is_critical": self.is_critical,
            "timestamp": self.timestamp,
        }
