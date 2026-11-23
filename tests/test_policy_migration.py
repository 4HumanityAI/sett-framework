"""Policy import compatibility, exports and explicit configuration boundaries.

These cases cover PolicyRuler and PolicyEvaluation ownership, mandatory policy
injection, resolver placement and declared bindings. Condition declarations
alone do not verify conditions or confer authority. Governed integration is
covered separately by the condition, producer and executor tests.
"""

import importlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import sett
import sett.authorization_ruler as authorization
import sett.policy_ruler as policy
from sett._governance.validation import canonical_json_bytes
from sett.authorization_ruler import (
    AuthorizationRequest,
    AuthorizationRuler,
    AuthorizationVerdict,
    FieldTrust,
    FieldValue,
    InMemoryAuthorizationEvidence,
    InMemoryAuthorizationReceiptStore,
    SETTAuthorizationConfigurationError,
    StrictJSONAuthorizationFingerprint,
)
from sett.policy_ruler._handoff import PRODUCED_SLOT_NAMES, VerifiedStateSlot

POLICY_VALUES = (
    "PolicySourceReference",
    "PolicyOrigin",
    "PolicyBinding",
    "PolicyApplicabilityState",
    "PolicyApplicability",
    "PolicySelection",
    "PolicyDecision",
)
SHARED_GROUPS = (
    (
        "models",
        "_governance.values",
        (
            "FieldTrust",
            "ExecutionMode",
            "ReceiptUse",
            "FieldValue",
            "ApprovalInput",
            "ApprovalRequirement",
            "VerifiedApproval",
            "AuthorizationConstraint",
            "AuthorizationRequest",
            "AuthorizationDecision",
            "AuthorizationReceipt",
            "AuthorizationOutcome",
        ),
    ),
    (
        "verdict",
        "_governance.verdict",
        (
            "AuthorizationVerdict",
            "permits_effect",
            "validate_verdict_semantics",
            "additional_authority_may_be_evaluated",
        ),
    ),
    ("contracts", "_governance.protocols", ("AuthorizationClock",)),
    (
        "evidence",
        "_governance.evidence",
        (
            "AuthorizationEvent",
            "AuthorizationEvidenceSink",
            "InMemoryAuthorizationEvidence",
        ),
    ),
)


@pytest.mark.parametrize("name", POLICY_VALUES)
def test_policy_aliases_are_identical(name):
    old = importlib.import_module("sett.authorization_ruler.models")
    new = importlib.import_module("sett.policy_ruler.models")
    value = getattr(new, name)
    assert (
        getattr(old, name)
        is getattr(authorization, name)
        is getattr(policy, name)
        is value
    )
    assert value.__module__ == "sett.policy_ruler.models"


@pytest.mark.parametrize("old_module,new_module,names", SHARED_GROUPS)
def test_shared_definitions_keep_old_exports(old_module, new_module, names):
    old = importlib.import_module("sett.authorization_ruler." + old_module)
    new = importlib.import_module("sett." + new_module)
    for name in names:
        value = getattr(new, name)
        assert getattr(old, name) is getattr(authorization, name) is value
        assert value.__module__ == "sett." + new_module
        if hasattr(sett, name):
            assert getattr(sett, name) is value


def test_protocol_alias_and_existing_runtime_membership():
    old = importlib.import_module("sett.authorization_ruler.contracts")
    new = importlib.import_module("sett.policy_ruler.contracts")
    assert (
        old.AuthorizationPolicy is new.AuthorizationPolicy is policy.AuthorizationPolicy
    )
    assert authorization.AuthorizationPolicy is policy.AuthorizationPolicy
    assert policy.AuthorizationPolicy.__module__ == "sett.policy_ruler.contracts"
    assert isinstance(StaticPolicy(True), old.AuthorizationPolicy)
    assert isinstance(StaticPolicy(True), new.AuthorizationPolicy)
    assert not isinstance(object(), new.AuthorizationPolicy)


def test_existing_root_policy_decision_and_prepared_foundation_surface():
    models = importlib.import_module("sett.authorization_ruler.models")
    assert models.AuthorizationVerdict is authorization.AuthorizationVerdict
    assert sett.PolicyDecision is authorization.PolicyDecision is policy.PolicyDecision
    # These are the policy exports. Condition values
    # only declare checks or report observations; they verify nothing and authorize
    # no effect. Root is unchanged.
    # The governance journal is an independent byte journal; it does not wire a real
    # durability producer or confer effect authority.
    # Review declarations, requests and facts install no
    # verifier, producer, scheduler, clock or deadline calculation.
    assert set(policy.__all__) == set(POLICY_VALUES) | {
        "GovernanceRecord",
        "GovernanceSubmission",
        "GovernanceEntry",
        "GovernanceReadResult",
        "GovernanceWriteResult",
        "GovernanceReconciliation",
        "GovernanceJournal",
        "DeadlineStatus",
        "DeadlineStatusFact",
        "NotificationDeliveredFact",
        "PolicyReviewObligation",
        "ReviewCompletedFact",
        "ReviewFact",
        "ReviewMode",
        "ReviewOutcome",
        "ReviewVerification",
        "ReviewVerificationRequest",
        "ReviewVerifier",
        "AuthorizationPolicy",
        "ConditionValidityMode",
        "ConditionVerification",
        "ConditionVerificationState",
        "ConditionVerifier",
        "PolicyCondition",
        "PolicyEvaluation",
        "PolicyRuler",
        "PolicySelectionResolver",
        "SETTPolicyConfigurationError",
    }
    for name in ("PolicyRuler", "PolicyEvaluation", "PolicySelectionResolver"):
        assert hasattr(policy, name)
    assert authorization.PolicySelectionResolver is policy.PolicySelectionResolver
    for name in ("PolicyRuler", "PolicyEvaluation", "PolicyCondition"):
        assert not hasattr(authorization, name)
    assert hasattr(policy, "PolicyCondition")
    for name in set(POLICY_VALUES) - {"PolicyDecision"}:
        assert not hasattr(sett, name)
    for name in (
        "PolicyRuler",
        "PolicyEvaluation",
        "PolicySelectionResolver",
        "SETTPolicyConfigurationError",
    ):
        assert not hasattr(sett, name)


@pytest.mark.parametrize("first", ("sett.policy_ruler", "sett.authorization_ruler"))
def test_fresh_process_import_orders(first):
    second = (
        "sett.authorization_ruler"
        if first == "sett.policy_ruler"
        else "sett.policy_ruler"
    )
    script = f"""
import importlib
importlib.import_module({first!r})
importlib.import_module({second!r})
import sett
import sett.authorization_ruler as old
import sett.policy_ruler as new
assert old.PolicyDecision is new.PolicyDecision is sett.PolicyDecision
assert old.PolicySelection is new.PolicySelection
assert old.AuthorizationPolicy is new.AuthorizationPolicy
assert old.PolicySelectionResolver is new.PolicySelectionResolver
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_mixed_path_nested_exact_types_and_detached_canonical_view():
    source = policy.PolicySourceReference(
        source_id="test_fixture.source",
        source_version="test_fixture.edition",
        locator="test_fixture.locator",
    )
    origin = authorization.PolicyOrigin(
        requirement_id="test_fixture.requirement",
        requirement_revision="test_fixture.revision",
        layer_id="test_fixture.layer",
        source=source,
        source_absence_reason=None,
    )
    binding = policy.PolicyBinding(
        policy_id="test_fixture.policy",
        policy_version="test_fixture.version",
        contract_version="2.0",
        origins=[origin],
        profile_id=None,
        profile_revision=None,
    )
    applicability = authorization.PolicyApplicability(
        policy_id=binding.policy_id,
        policy_version=binding.policy_version,
        state=policy.PolicyApplicabilityState.APPLIES,
        justification="Synthetic test declaration",
        fact_names=["test_fixture.fact"],
        evidence_refs=[],
    )
    selection = policy.PolicySelection(
        selection_id="test_fixture.selection",
        selection_revision="test_fixture.revision",
        contract_version="2.0",
        authority_ref="test_fixture.authority",
        effective_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        action_id="test_fixture.action",
        action_type="test_fixture.effect",
        required=[binding],
        informative=[],
        applicability=[applicability],
        facts={
            "test_fixture.fact": FieldValue(
                None, source_id="test_fixture.source", trust=FieldTrust.AUTHORITATIVE
            )
        },
    )
    assert type(selection) is authorization.PolicySelection
    assert type(selection.required[0]) is authorization.PolicyBinding
    assert (
        selection.canonical_view()["required"][0]["origins"][0]["source"]
        == source.canonical_view()
    )
    view = selection.canonical_view()
    view["required"].clear()
    assert len(selection.canonical_view()["required"]) == 1


@pytest.mark.parametrize(
    "version,reason",
    (
        ("3.0", "authorization.policy_contract_unsupported"),
        ("", "authorization.policy_binding_invalid"),
    ),
)
def test_existing_version_diagnostics_from_new_path(version, reason):
    origin = policy.PolicyOrigin(
        requirement_id="test_fixture.requirement",
        requirement_revision="test_fixture.revision",
        layer_id="test_fixture.layer",
        source=None,
        source_absence_reason="Synthetic test fixture",
    )
    with pytest.raises(SETTAuthorizationConfigurationError) as error:
        policy.PolicyBinding(
            policy_id="test_fixture.policy",
            policy_version="test_fixture.version",
            contract_version=version,
            origins=[origin],
            profile_id=None,
            profile_revision=None,
        )
    assert error.value.reason_code == reason
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


class FixedClock:
    def now(self):
        return datetime(2030, 1, 1, tzinfo=timezone.utc)


class StaticPolicy:
    policy_id = "test_fixture.policy"

    def __init__(self, grant):
        self.grant = grant
        self.calls = 0

    def evaluate(self, request, verified_approvals):
        self.calls += 1
        constructor = (
            policy.PolicyDecision.grant if self.grant else policy.PolicyDecision.deny
        )
        return constructor(self.policy_id, conditions=(), review_obligations=())


def declared_binding(policy_id="test_fixture.policy"):
    return policy.PolicyBinding(
        policy_id=policy_id,
        policy_version="1",
        contract_version="2.0",
        origins=[
            policy.PolicyOrigin(
                requirement_id="test_fixture.requirement",
                requirement_revision="1",
                layer_id="test_fixture.layer",
                source=None,
                source_absence_reason="Synthetic test fixture",
            )
        ],
        profile_id=None,
        profile_revision=None,
    )


class SingleSelectionResolver:
    resolver_id = "test_fixture.resolver"

    def __init__(self):
        self.binding = declared_binding()

    def resolve(self, request):
        return policy.PolicySelection(
            selection_id="test_fixture.selection",
            selection_revision="1",
            contract_version="2.0",
            authority_ref="test_fixture.authority",
            effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            action_id=request.action_id,
            action_type=request.action_type,
            required=[self.binding],
            informative=[],
            applicability=[
                policy.PolicyApplicability(
                    policy_id=self.binding.policy_id,
                    policy_version=self.binding.policy_version,
                    state=policy.PolicyApplicabilityState.APPLIES,
                    justification="Synthetic test declaration",
                    fact_names=[],
                    evidence_refs=[],
                )
            ],
            facts={},
        )


class SyntheticStateProducer:
    schema_revision = "1"

    def __init__(self, slot):
        self.slot = slot
        self.producer_id = "test_fixture.producer." + slot

    def produce(self, context):
        value_bytes = b"{}"
        if self.slot == "conditions":
            value_bytes = canonical_json_bytes({
                "inventory": json.loads(context.condition_inventory),
                "observations": {},
                "test_fixture_value_hex": value_bytes.hex(),
            }, field="synthetic condition state")
        return VerifiedStateSlot(
            slot=self.slot,
            producer_id=self.producer_id,
            schema_revision=self.schema_revision,
            epoch=0,
            action_id=context.request.action_id,
            checkpoint_id=context.checkpoint_id,
            required_keys=(),
            observed_keys=(),
            value_bytes=value_bytes,
            evidence_refs=(),
            valid_until=None,
        )

    def confirm(self, slot):
        return True


def configured_pair(*, producers=PRODUCED_SLOT_NAMES):
    evidence = InMemoryAuthorizationEvidence()
    clock = FixedClock()
    owner = policy.PolicyRuler(
        ruler_id="test_fixture.policy_ruler",
        entry_checkpoint_id="test_fixture.entry",
        evaluation_ttl_seconds=3_600,
        max_live_evaluations=8,
        clock=clock,
        evidence_sink=evidence,
        selection_resolver=SingleSelectionResolver(),
    )
    for name in producers:
        owner._register_state_producer(SyntheticStateProducer(name))
    ruler = AuthorizationRuler(
        issuer_id="test_fixture.issuer",
        receipt_ttl_seconds=10,
        fingerprint_policy=StrictJSONAuthorizationFingerprint(),
        receipt_store=InMemoryAuthorizationReceiptStore(),
        evidence_sink=evidence,
        clock=clock,
        policy_ruler=owner,
    )
    return ruler, owner


@pytest.mark.parametrize("grant", (True, False))
def test_relocated_policy_result_in_unchanged_authorization_runtime(grant):
    callback = StaticPolicy(grant)
    ruler, _owner = configured_pair()
    ruler.register_policy(
        "test_fixture.effect", callback, binding=declared_binding()
    )
    request = AuthorizationRequest(
        action_id="test_fixture.action",
        trace_id="test_fixture.trace",
        action_type="test_fixture.effect",
        payload={},
        principal=FieldValue(
            "test_fixture.subject",
            source_id="test_fixture.source",
            trust=FieldTrust.AUTHORITATIVE,
        ),
    )
    outcome = ruler.authorize(request)
    assert callback.calls == 1
    assert outcome.decision.verdict is (
        AuthorizationVerdict.GRANT if grant else AuthorizationVerdict.DENY
    )
    assert (outcome.receipt is not None) is grant


def test_integration_stays_held_without_real_state_producers():
    """No product producer exists for four closed slots, so authority blocks."""
    callback = StaticPolicy(True)
    ruler, _owner = configured_pair(producers=())
    ruler.register_policy(
        "test_fixture.effect", callback, binding=declared_binding()
    )
    request = AuthorizationRequest(
        action_id="test_fixture.action",
        trace_id="test_fixture.trace",
        action_type="test_fixture.effect",
        payload={},
        principal=FieldValue(
            "test_fixture.subject",
            source_id="test_fixture.source",
            trust=FieldTrust.AUTHORITATIVE,
        ),
    )

    with pytest.raises(Exception) as error:
        ruler.authorize(request)

    assert error.value.reason_code == "authorization.policy_evaluation_incomplete"
