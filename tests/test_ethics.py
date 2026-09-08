"""
SETT Framework: Tests: Ethics and Risk System
======================================================
Tests for the full three-layer hybrid risk evaluation:

  Layer 1: EthicalFilter (action harm score)
  Layer 2: RiskProfile (three-pillar user assessment)
  Layer 3: EnvironmentalContext (RiskLevel 0–5)

Key contracts:
  - REJECT always raises SETTEthicalFilterRejectedError
  - Environmental context tightens filter thresholds automatically
  - RiskProfile composite score maps correctly to RiskLevel
  - Human at risk is detected via biometrics, profile, or environment
  - Audit log records every decision with full context
  - Privacy: RiskProfile never leaks to universal memory
"""
import pytest

from sett import (
    EnvironmentalContext,
    EthicalFilter,
    EthicalRule,
    EthicalRuleset,
    FilterVerdict,
    HarmCategory,
    RiskLevel,
    RiskProfile,
    SETTEthicalFilterRejectedError,
    SETTMemoryAccessDeniedError,
    SETTValidationError,
    default_ruleset,
)
from sett.ethics_ruler.ethic_kernel.context_analyzer import (
    ContextAnalyzer,
    canonicalize_for_keyword_match,
)

# ── RiskProfile tests ────────────────────────────────────────────────────────

class TestRiskProfile:

    def test_baseline_profile_has_zero_values(self):
        p = RiskProfile.baseline()
        assert p.emotional_instability == 0.0
        assert p.influence_vulnerability == 0.0
        assert p.collateral_damage_potential == 0.0

    def test_composite_score_is_weighted_combination(self):
        p = RiskProfile(
            emotional_instability=1.0,
            influence_vulnerability=0.0,
            collateral_damage_potential=0.0,
        )
        # Weight for emotional_instability is 0.45
        assert abs(p.composite_score - 0.45) < 0.001

    def test_composite_score_max_is_1(self):
        p = RiskProfile(
            emotional_instability=1.0,
            influence_vulnerability=1.0,
            collateral_damage_potential=1.0,
        )
        assert abs(p.composite_score - 1.0) < 0.001

    def test_values_out_of_range_raise_error(self):
        with pytest.raises(ValueError):
            RiskProfile(emotional_instability=1.5)
        with pytest.raises(ValueError):
            RiskProfile(influence_vulnerability=-0.1)

    def test_suggested_level_baseline_is_normal(self):
        p = RiskProfile.baseline()
        assert p.suggested_level == RiskLevel.LEVEL_0

    def test_suggested_level_high_instability_is_danger(self):
        p = RiskProfile(
            emotional_instability=0.9,
            influence_vulnerability=0.5,
            collateral_damage_potential=0.5,
        )
        # composite ≈ 0.9*0.45 + 0.5*0.25 + 0.5*0.30 = 0.405+0.125+0.15 = 0.68
        assert p.suggested_level >= RiskLevel.LEVEL_3

    def test_dominant_pillar_is_highest_value(self):
        p = RiskProfile(
            emotional_instability=0.2,
            influence_vulnerability=0.8,
            collateral_damage_potential=0.3,
        )
        assert p.dominant_pillar == "influence_vulnerability"

    def test_serialization_round_trip(self):
        p = RiskProfile(
            emotional_instability=0.4,
            influence_vulnerability=0.6,
            collateral_damage_potential=0.2,
        )
        d = p.to_dict()
        p2 = RiskProfile.from_dict(d)
        assert abs(p2.emotional_instability - 0.4) < 0.001
        assert abs(p2.influence_vulnerability - 0.6) < 0.001
        assert abs(p2.collateral_damage_potential - 0.2) < 0.001

    def test_timestamp_survives_the_round_trip(self):
        """F12 regression (defensive audit #2).

        to_dict() always serializes `timestamp`, but from_dict() used to
        ignore it and mint a fresh one instead: every roundtrip through
        PrivateMemory silently replaced the moment this assessment was
        actually made.
        """
        p = RiskProfile(emotional_instability=0.4)
        d = p.to_dict()
        p2 = RiskProfile.from_dict(d)
        assert p2.timestamp == p.timestamp

    def test_weights_that_do_not_sum_to_one_are_rejected(self):
        """F13 regression (defensive audit #2): the composite_score
        invariant (always in [0.0, 1.0]) depends on the weights summing to
        1.0. An unvalidated custom weighting could silently break it.
        """
        with pytest.raises(SETTValidationError):
            RiskProfile(
                emotional_instability=0.5,
                _weights={
                    "emotional_instability": 0.9,
                    "influence_vulnerability": 0.9,
                    "collateral_damage_potential": 0.9,
                },
            )

    def test_a_negative_weight_is_rejected(self):
        with pytest.raises(SETTValidationError):
            RiskProfile(
                _weights={
                    "emotional_instability": 1.3,
                    "influence_vulnerability": -0.3,
                    "collateral_damage_potential": 0.0,
                },
            )

    def test_weights_cannot_be_mutated_after_construction(self):
        """Blocking item assignment closes the other half of F13: even a
        validated set of weights must not drift after construction.
        """
        p = RiskProfile(emotional_instability=0.5)
        with pytest.raises(TypeError):
            p._weights["emotional_instability"] = 0.99

    def test_a_risk_profile_survives_deepcopy(self):
        """The frozen `_weights` mapping must not break deepcopy(), which
        PrivateMemory.write() and UniversalMemory storage both rely on.
        """
        import copy

        p = RiskProfile(emotional_instability=0.7)
        p2 = copy.deepcopy(p)
        assert p2 is not p
        assert p2.composite_score == p.composite_score


# ── RiskLevel tests ───────────────────────────────────────────────────────────

class TestRiskLevel:

    def test_all_six_levels_exist(self):
        levels = list(RiskLevel)
        assert len(levels) == 6
        assert RiskLevel.LEVEL_0 in levels
        assert RiskLevel.LEVEL_5 in levels

    def test_level_0_is_not_elevated(self):
        assert not RiskLevel.LEVEL_0.is_elevated()

    def test_levels_1_to_5_are_elevated(self):
        for level in [RiskLevel.LEVEL_1, RiskLevel.LEVEL_2,
                      RiskLevel.LEVEL_3, RiskLevel.LEVEL_4, RiskLevel.LEVEL_5]:
            assert level.is_elevated()

    def test_only_4_and_5_are_critical(self):
        assert not RiskLevel.LEVEL_3.is_critical()
        assert RiskLevel.LEVEL_4.is_critical()
        assert RiskLevel.LEVEL_5.is_critical()

    def test_levels_are_comparable(self):
        assert RiskLevel.LEVEL_0 < RiskLevel.LEVEL_3
        assert RiskLevel.LEVEL_5 > RiskLevel.LEVEL_4
        assert RiskLevel.LEVEL_2 == RiskLevel.LEVEL_2

    def test_each_level_has_emoji(self):
        for level in RiskLevel:
            assert level.emoji  # non-empty string

    def test_each_level_has_description(self):
        for level in RiskLevel:
            assert len(level.description) > 10


# ── EnvironmentalContext tests ───────────────────────────────────────────────

class TestEnvironmentalContext:

    def test_normal_context_factory(self):
        ctx = EnvironmentalContext.normal("zone_a")
        assert ctx.risk_level == RiskLevel.LEVEL_0
        assert ctx.location_id == "zone_a"
        assert not ctx.requires_response

    def test_critical_context_should_notify_emergency(self):
        """F20 (defensive audit #2): renamed from auto_notify_emergency,
        whose name implied SETT automatically performs the notification.
        This field is only a recommendation derived from risk_level."""
        ctx = EnvironmentalContext(
            risk_level=RiskLevel.LEVEL_4, location_id="zone"
        )
        assert ctx.should_notify_emergency is True
        assert ctx.requires_evacuation is True

    def test_non_critical_does_not_recommend_notify(self):
        ctx = EnvironmentalContext(
            risk_level=RiskLevel.LEVEL_2, location_id="zone"
        )
        assert ctx.should_notify_emergency is False
        assert ctx.requires_evacuation is False
        assert ctx.requires_response is True

    def test_level_0_has_no_threshold_modifier(self):
        ctx = EnvironmentalContext.normal()
        assert ctx.filter_threshold_modifier == 0.0

    def test_higher_levels_have_larger_modifiers(self):
        modifiers = [
            EnvironmentalContext(
                risk_level=lvl, location_id="x"
            ).filter_threshold_modifier
            for lvl in RiskLevel
        ]
        # Modifiers should be monotonically non-decreasing
        for i in range(len(modifiers) - 1):
            assert modifiers[i] <= modifiers[i + 1]

    def test_serialization_round_trip(self):
        ctx = EnvironmentalContext(
            risk_level=RiskLevel.LEVEL_3,
            location_id="store_42",
            source_domain="health",
            message="Test alert",
        )
        d = ctx.to_dict()
        ctx2 = EnvironmentalContext.from_dict(d)
        assert ctx2.risk_level == RiskLevel.LEVEL_3
        assert ctx2.location_id == "store_42"
        assert ctx2.source_domain == "health"

    def test_timestamp_survives_the_round_trip(self):
        """F03 regression (defensive audit #2).

        to_dict() always serializes `timestamp`, but from_dict() used to
        ignore it and mint a fresh one instead: every read through
        UniversalMemory silently replaced the real publish time with
        whatever moment it happened to be read.
        """
        ctx = EnvironmentalContext(
            risk_level=RiskLevel.LEVEL_1, location_id="zone"
        )
        d = ctx.to_dict()
        ctx2 = EnvironmentalContext.from_dict(d)
        assert ctx2.timestamp == ctx.timestamp

    def test_does_not_contain_personal_data_in_dict(self):
        """
        The serialized form of EnvironmentalContext must never contain
        personal identifiers: only level, location, source, and message.
        """
        ctx = EnvironmentalContext(
            risk_level=RiskLevel.LEVEL_2, location_id="loc"
        )
        d = ctx.to_dict()
        # These keys must NOT be present
        forbidden_keys = [
            "user_id", "name", "heart_rate", "temperature",
            "emotional_instability", "influence_vulnerability",
        ]
        for key in forbidden_keys:
            assert key not in d, f"Personal data key '{key}' found in EnvironmentalContext dict"


# ── EthicalFilter tests ───────────────────────────────────────────────────────

class TestEthicalFilter:

    def test_safe_action_is_allowed(self):
        f = EthicalFilter()
        verdict = f.evaluate(action="read_schedule", context={})
        assert verdict == FilterVerdict.ALLOW

    def test_reject_raises_error(self):
        """A high-risk action must raise SETTEthicalFilterRejectedError."""
        strict = EthicalRuleset(
            name="strict_test",
            reject_threshold=1.0,
            warn_threshold=0.5,
        )
        f = EthicalFilter(ruleset=strict)
        with pytest.raises(SETTEthicalFilterRejectedError):
            f.evaluate(action="any_action", context={}, emotional_state="crisis")

    def test_audit_log_records_every_decision(self):
        f = EthicalFilter()
        f.evaluate("action_a", {})
        f.evaluate("action_b", {})
        log = f.get_audit_log()
        assert len(log) == 2
        assert log[0]["action"] == "action_a"
        assert log[1]["action"] == "action_b"

    def test_audit_log_contains_verdict(self):
        f = EthicalFilter()
        f.evaluate("safe_action", {})
        entry = f.get_audit_log()[0]
        assert "verdict" in entry
        assert entry["verdict"] in ("allow", "warn", "reject")

    def test_audit_log_contains_harm_score(self):
        f = EthicalFilter()
        f.evaluate("action", {})
        entry = f.get_audit_log()[0]
        assert "harm_score" in entry
        assert isinstance(entry["harm_score"], float)

    def test_environmental_context_tightens_thresholds(self):
        """
        With a high environmental risk level, the filter should reject
        an action that only warns under normal conditions.
        """
        # Under normal conditions, the action reaches the warning threshold.
        f_normal = EthicalFilter()
        verdict_normal = f_normal.evaluate(
            action="send_message",
            context={},
            emotional_state="distressed",
        )
        assert verdict_normal is FilterVerdict.WARN
        # Under a LEVEL_5 environment, same action with same state should be rejected
        f_strict = EthicalFilter()
        env_ctx = EnvironmentalContext(
            risk_level=RiskLevel.LEVEL_5, location_id="zone"
        )
        with pytest.raises(SETTEthicalFilterRejectedError):
            f_strict.evaluate(
                action="send_message",
                context={},
                emotional_state="distressed",
                environmental_context=env_ctx,
            )

    def test_risk_profile_increases_harm_score(self):
        """A high-risk profile should increase the final harm score."""
        f = EthicalFilter()
        # Evaluate the same action with a baseline vs high-risk profile
        f.evaluate("send_message", {}, risk_profile=RiskProfile.baseline())
        f.evaluate("send_message", {}, risk_profile=RiskProfile(
            emotional_instability=0.9,
            influence_vulnerability=0.9,
            collateral_damage_potential=0.9,
        ))
        log = f.get_audit_log()
        score_baseline = log[0]["harm_score"]
        score_high_risk = log[1]["harm_score"]
        assert score_high_risk > score_baseline

    def test_human_at_risk_promotes_unclassified_benign_action_to_warn(self):
        """Critical biometrics preserve harm score but require cautious review."""
        f_baseline = EthicalFilter()
        f_biometric = EthicalFilter()

        f_baseline.evaluate("memory_write", {})
        f_biometric.evaluate(
            "memory_write",
            context={"health": {"heart_rate_bpm": 180, "temperature_celsius": 40.5}},
        )
        baseline = f_baseline.get_audit_log()[0]
        biometric = f_biometric.get_audit_log()[0]

        assert biometric["human_at_risk"] is True
        assert biometric["situation_urgency"] > baseline["situation_urgency"]
        assert biometric["harm_score"] == baseline["harm_score"]
        assert biometric["verdict"] == "warn"
        assert biometric["decision_reason_codes"] == [
            "human_at_risk_without_protective_classification"
        ]

    def test_three_layers_combined_cause_reject(self):
        """
        The REJECT verdict requires the combined score from all three layers
        to exceed the reject threshold.

        crisis (+5.0) + max risk profile (+3.0) = 8.0 = reject threshold → REJECT

        The generic analyzer may still reject when the proposed action's
        combined harm score reaches the threshold. Human-at-risk is logged
        separately and no longer forces a verdict by itself.
        """
        from sett import RiskProfile
        f = EthicalFilter()
        with pytest.raises(SETTEthicalFilterRejectedError):
            f.evaluate(
                action="memory_write",
                context={},
                emotional_state="crisis",          # +5.0
                risk_profile=RiskProfile(          # +3.0 (all pillars at max)
                    emotional_instability=1.0,
                    influence_vulnerability=1.0,
                    collateral_damage_potential=1.0,
                ),
            )

    def test_ruleset_can_be_replaced(self):
        """The EthicalFilter should accept a custom ruleset."""
        custom = EthicalRuleset(
            name="permissive",
            reject_threshold=9.5,
            warn_threshold=8.0,
        )
        f = EthicalFilter(ruleset=custom)
        assert f.principle == custom.principle
        assert f._ruleset.name == "permissive"

    def test_rule_metadata_does_not_change_a_verdict(self):
        """Rule metadata is inspectable but ContextAnalyzer owns scoring."""
        action = "say hello"
        baseline_ruleset = default_ruleset()
        baseline_ruleset.reject_threshold = 1.0
        baseline_ruleset.warn_threshold = 0.5
        baseline = EthicalFilter(ruleset=baseline_ruleset).evaluate(action, {})

        variants = []
        zero_weight = default_ruleset()
        for rule in zero_weight.rules:
            rule.weight = 0.0
        variants.append(zero_weight)

        inactive = default_ruleset()
        for rule in inactive.rules:
            rule.deactivate()
        variants.append(inactive)

        variants.append(EthicalRuleset(name="empty"))
        for ruleset in variants:
            ruleset.reject_threshold = 1.0
            ruleset.warn_threshold = 0.5
            assert EthicalFilter(ruleset=ruleset).evaluate(action, {}) is baseline

    def test_rule_description_is_not_copied_into_audit_evidence(self):
        marker = "rule-description-must-remain-configuration-metadata"
        ruleset = EthicalRuleset(
            name="documented",
            reject_threshold=1.0,
            warn_threshold=0.5,
            rules=[
                EthicalRule(
                    name="documented-rule",
                    category=HarmCategory.AMBIGUITY,
                    weight=2.0,
                    description=marker,
                )
            ],
        )
        ethical_filter = EthicalFilter(ruleset=ruleset)
        ethical_filter.evaluate("say hello", {})

        assert marker not in repr(ethical_filter.get_audit_log())

    def test_principle_is_accessible(self):
        f = EthicalFilter()
        assert len(f.principle) > 10

    def test_audit_log_records_env_level(self):
        """Audit log should record the environmental risk level used."""
        f = EthicalFilter()
        ctx = EnvironmentalContext(
            risk_level=RiskLevel.LEVEL_2, location_id="zone"
        )
        f.evaluate("safe_action", {}, environmental_context=ctx)
        entry = f.get_audit_log()[0]
        assert entry["env_risk_level"] == 2

    def test_reject_error_message_contains_useful_info(self):
        """The rejection error should tell the developer what happened."""
        strict = EthicalRuleset(name="strict", reject_threshold=0.1)
        f = EthicalFilter(ruleset=strict)
        try:
            f.evaluate("blocked_action", {}, emotional_state="crisis")
        except SETTEthicalFilterRejectedError as e:
            msg = str(e)
            assert "blocked_action" in msg
            assert "Score" in msg or "score" in msg


# ── Integration: three-layer system ─────────────────────────────────────────

class TestThreeLayerIntegration:

    def test_emotional_risk_modifiers_are_immutable_across_instances(self):
        first = ContextAnalyzer()
        second = ContextAnalyzer()
        baseline = second.analyze(
            "say hello", {}, emotional_state="crisis"
        ).risk_score

        with pytest.raises(TypeError):
            first.EMOTIONAL_RISK_MODIFIERS["crisis"] = 0.0

        assert second.analyze(
            "say hello", {}, emotional_state="crisis"
        ).risk_score == baseline

    def test_all_three_layers_combine_correctly(self):
        """
        Verify that all three layers contribute to the final score
        by testing with each layer isolated and then combined.
        """
        analyzer = ContextAnalyzer()

        # Only action (Layer 1)
        a1 = analyzer.analyze("send", {})
        # Action + emotional state (Layer 1 + 2 partial)
        a2 = analyzer.analyze("send", {}, emotional_state="distressed")
        # Action + profile (Layer 1 + 2 full)
        a3 = analyzer.analyze("send", {}, risk_profile=RiskProfile(
            emotional_instability=0.8, influence_vulnerability=0.5,
            collateral_damage_potential=0.5,
        ))
        # All three layers
        a4 = analyzer.analyze(
            "send", {},
            emotional_state="distressed",
            risk_profile=RiskProfile(
                emotional_instability=0.8, influence_vulnerability=0.5,
                collateral_damage_potential=0.5,
            ),
            environmental_context=EnvironmentalContext(
                risk_level=RiskLevel.LEVEL_3, location_id="zone"
            ),
        )
        # Each additional layer should increase the score
        assert a2.risk_score >= a1.risk_score
        assert a3.risk_score >= a1.risk_score
        assert a4.risk_score >= a3.risk_score

    def test_almacen_scenario(self):
        """
        The 'warehouse scenario': Instance A detects a critical risk,
        publishes EnvironmentalContext Level 4.
        Instance B reads it and its filter becomes more strict.
        """
        from sett.memory_ruler.universal import UniversalMemory

        # Shared memory simulating the same location
        shared_mem = UniversalMemory()

        # Instance A detects critical risk and publishes
        ctx_published = EnvironmentalContext(
            risk_level=RiskLevel.LEVEL_4,
            location_id="store_42",
            source_domain="health",
            message="Critical biometric indicators in environment.",
        )
        shared_mem.publish_environmental_context(ctx_published)

        # Instance B reads the context
        ctx_read = shared_mem.read_environmental_context("store_42")
        assert ctx_read is not None
        assert ctx_read.risk_level == RiskLevel.LEVEL_4
        assert ctx_read.should_notify_emergency is True

        # Instance B's filter with this context rejects normally-safe actions
        f_b = EthicalFilter()
        with pytest.raises(SETTEthicalFilterRejectedError):
            f_b.evaluate(
                action="send_message",
                context={},
                emotional_state="distressed",
                environmental_context=ctx_read,
            )

    def test_privacy_contract_risk_profile_never_in_universal_memory(self):
        """
        The most important privacy test:
        A RiskProfile must NEVER appear in UniversalMemory.
        Only EnvironmentalContext (anonymized) is shared.
        """
        from sett import SETTAgent, SETTExpert, SETTOrchestrator

        class HealthExpert(SETTExpert):
            def resolve(self, context):
                profile = RiskProfile(
                    emotional_instability=0.9,
                    influence_vulnerability=0.7,
                    collateral_damage_potential=0.8,
                )
                # Store profile in PRIVATE memory only
                if self._private_memory:
                    self._private_memory.write("risk_profile", profile.to_dict())
                # Return ONLY anonymized result
                return {
                    "alert": True,
                    "suggested_env_level": profile.suggested_level.value,
                }

        class HealthAgent(SETTAgent):
            def __init__(self):
                super().__init__("HealthAgent", "health")
                self.register_expert(HealthExpert("health_expert"))

            def process(self, input_data):
                result = self.get_expert("health_expert").resolve(input_data)
                self._publish_to_universal(result)
                return result

        o = SETTOrchestrator()
        o.register_agent(HealthAgent())
        o.process({}, domain="health")

        universal = o.read_universal_memory()

        # The raw RiskProfile pillar values must NOT be in universal memory
        health_result = universal.get("health", {})
        forbidden = [
            "emotional_instability", "influence_vulnerability",
            "collateral_damage_potential", "composite_score",
        ]
        for key in forbidden:
            assert key not in health_result, (
                f"Privacy violation: '{key}' found in universal memory"
            )

    def test_a_careless_expert_cannot_publish_a_raw_risk_profile(self):
        """F02 regression (defensive audit #2).

        The test above only proves that a *cooperative* Expert, one that
        remembers to strip everything sensitive before returning, keeps
        the boundary. Before this fix, nothing in UniversalMemory.update()
        actually enforced it: an Expert that simply forgot, or one built
        adversarially, could publish a raw RiskProfile instance and it
        would be accepted and stored like any other value.
        """
        from sett import SETTAgent, SETTOrchestrator

        class CarelessAgent(SETTAgent):
            def __init__(self):
                super().__init__("CarelessAgent", "careless")

            def process(self, input_data):
                profile = RiskProfile(
                    emotional_instability=0.9,
                    influence_vulnerability=0.7,
                    collateral_damage_potential=0.8,
                )
                # A mistake, not malice: the whole profile object ends up
                # nested inside the published result.
                result = {"alert": True, "assessment": {"profile": profile}}
                self._publish_to_universal(result)
                return result

        o = SETTOrchestrator()
        o.register_agent(CarelessAgent())

        with pytest.raises(SETTMemoryAccessDeniedError):
            o.process({}, domain="careless")

        universal = o.read_universal_memory()
        assert "careless" not in universal

    def test_an_analyzer_that_mutates_context_cannot_taint_stored_memory(self):
        """F10 regression (defensive audit #2).

        UniversalMemory.update() used to hand the filter a shallow copy
        (``dict(result)``) of the published result: nested dicts/lists
        were shared with the caller's own ``result``. An analyzer that
        mutated something it was given during evaluation silently tainted
        what later got stored, with no signal it had happened.
        """
        from sett import SETTAgent, SETTOrchestrator
        from sett.ethics_ruler.ethic_kernel.context_analyzer import (
            ContextAnalyzer,
        )

        class MutatingAnalyzer(ContextAnalyzer):
            def analyze(
                self,
                action,
                context,
                emotional_state="unknown",
                risk_profile=None,
                environmental_context=None,
            ):
                nested = context.get("nested")
                if isinstance(nested, dict):
                    nested["tampered"] = True
                return super().analyze(
                    action, context, emotional_state, risk_profile,
                    environmental_context,
                )

        class TaintedAgent(SETTAgent):
            def __init__(self):
                super().__init__("TaintedAgent", "tainted")

            def process(self, input_data):
                result = {"nested": {"value": 1}}
                self._publish_to_universal(result)
                return result

        ethical_filter = EthicalFilter()
        ethical_filter.register_analyzer("memory_write", MutatingAnalyzer())
        o = SETTOrchestrator(ethical_filter=ethical_filter)
        o.register_agent(TaintedAgent())
        o.process({}, domain="tainted")

        stored = o.read_universal_memory()["tainted"]
        assert "tampered" not in stored["nested"]

    def test_reserved_pillar_keys_cannot_be_published_even_without_the_object(
        self,
    ):
        """The same boundary, reached without ever constructing a RiskProfile.

        A serialized dict with the right field names is just as much a
        leak as the object itself, since RiskProfile.to_dict() produces exactly
        this shape.
        """
        from sett import SETTAgent, SETTOrchestrator

        class SerializingAgent(SETTAgent):
            def __init__(self):
                super().__init__("SerializingAgent", "serializing")

            def process(self, input_data):
                result = {
                    "nested": {"emotional_instability": 0.9},
                }
                self._publish_to_universal(result)
                return result

        o = SETTOrchestrator()
        o.register_agent(SerializingAgent())

        with pytest.raises(SETTMemoryAccessDeniedError):
            o.process({}, domain="serializing")


# ─────────────────────────────────────────────────────────────────────
# AUD4-ROB-01: Unicode canonicalization of the Layer 1 keyword matcher
# ─────────────────────────────────────────────────────────────────────

class TestKeywordCanonicalizationPolicy:
    """
    The policy, stated as tests: invisible characters and compatibility
    forms are folded away before keywords are matched. Homoglyphs are
    not, and the framework says so rather than implying otherwise.
    """

    @pytest.mark.parametrize(
        "written, canonical",
        [
            ("ha\u200brm", "harm"),          # zero-width space
            ("ha\u200crm", "harm"),          # zero-width non-joiner
            ("emer\u200dgency", "emergency"),  # zero-width joiner
            ("de\u00adlete", "delete"),      # soft hyphen
            ("\u202eharm\u202c", "harm"),    # bidi overrides
            ("ｄｅｌｅｔｅ", "delete"),  # fullwidth
            ("DELETE", "delete"),
            ("Emergency", "emergency"),
            ("STRASSE", "strasse"),          # casefold, not lower()
            ("harm", "harm"),                # already canonical
        ],
    )
    def test_canonical_forms(self, written, canonical):
        assert canonicalize_for_keyword_match(written) == canonical

    def test_homoglyphs_are_not_folded_and_that_is_deliberate(self):
        """
        Cyrillic 'а' is a different letter, not a different spelling.
        NFKC correctly leaves it alone, so this stays outside what
        canonicalization can reach, documented instead of papered over
        with an incomplete homoglyph table.
        """
        assert canonicalize_for_keyword_match("hаrm") != "harm"


class TestLayer1ScoresInvisibleSpellingsTheSameWay:

    ANALYZER = ContextAnalyzer()

    def _score(self, action, context=None):
        return self.ANALYZER.analyze(action, context or {}).risk_score

    @pytest.mark.parametrize(
        "obfuscated, plain",
        [
            ("ha\u200brm the user", "harm the user"),
            ("emer\u200bgency call", "emergency call"),
            ("de\u00adlete everything", "delete everything"),
            ("s\u200bend the file", "send the file"),
            ("ｓｅｎｄ the file", "send the file"),
        ],
    )
    def test_obfuscated_action_scores_like_the_plain_one(self, obfuscated, plain):
        assert self._score(obfuscated) == self._score(plain)

    @pytest.mark.parametrize("field", ["proposed_action", "action_type"])
    def test_structured_fields_are_canonicalized_too(self, field):
        obfuscated = self._score("memory write", {field: "de\u00adlete_all"})
        plain = self._score("memory write", {field: "delete_all"})
        assert obfuscated == plain
        assert plain > self._score("memory write", {field: "read_one"})

    def test_consequences_are_listed_for_invisible_spellings_too(self):
        consequences = self.ANALYZER.analyze("emer\u200bgency call", {}).consequences
        assert any("Emergency services" in c for c in consequences)

    def test_emotional_state_survives_an_invisible_character(self):
        obfuscated = self._score("say hello", {})
        analyzer = self.ANALYZER
        with_zwsp = analyzer.analyze(
            "say hello", {}, emotional_state="cri\u200bsis"
        ).risk_score
        with_plain = analyzer.analyze(
            "say hello", {}, emotional_state="crisis"
        ).risk_score
        assert with_zwsp == with_plain
        assert with_plain > obfuscated
