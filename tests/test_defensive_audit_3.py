"""
Regression tests for defensive audit #3.

One test per finding, each written so that reverting its fix fails here.
The two HIGH findings get the most attention, because both were
fail-open: one let the governance layer reach ALLOW without any rule
having allowed anything, and the other let policy approve one payload
while a different one was performed.
"""
from __future__ import annotations

import copy
import threading
import time
from datetime import datetime, timezone

import pytest

from sett import (
    Action,
    BiometricReading,
    EnvironmentalContext,
    EthicalFilter,
    ExecutionContext,
    ExecutionState,
    ExecutionStatus,
    InMemoryIdempotencyStore,
    LifecyclePolicy,
    PrivateMemory,
    RetryPolicy,
    RiskLevel,
    SETTAgent,
    SETTExecutor,
    SETTOrchestrator,
    SETTValidationError,
    StubDomainAgent,
    UniversalMemory,
)
from sett.core_ruler.lifecycle import Deadline, pop_lifecycle_kwargs
from sett.ethics_ruler.ethic_kernel.context_analyzer import (
    ContextAnalysis,
    ContextAnalyzer,
)


def _analysis(action: str, score, emotional_state: str = "unknown"):
    return ContextAnalysis(
        action=action,
        risk_score=score,
        emotional_state=emotional_state,
        reasoning="deliberate",
        consequences=[],
    )


# ── a non-finite harm score cannot reach ALLOW ──────────────────────────────


@pytest.mark.parametrize(
    "score", [float("nan"), float("inf"), float("-inf"), -1.0, "high", True, None]
)
def test_a_score_that_cannot_be_compared_is_refused(score):
    """``nan >= threshold`` is False, so NaN cleared every threshold.

    A score that no comparison can order is not a low score; it is the
    absence of one, and letting it fall through to ALLOW is a fail-open
    in the layer this framework exists to make fail-closed.
    """
    with pytest.raises(SETTValidationError):
        _analysis("charge_payment", score)


def test_an_ordinary_score_is_unaffected():
    assert _analysis("charge_payment", 0).risk_score == 0.0
    assert _analysis("charge_payment", 3).risk_score == 3.0
    assert _analysis("charge_payment", 7.5).risk_score == 7.5


def test_no_ceiling_is_imposed_on_the_score():
    """The scale belongs to the ruleset; a hardcoded cap would silently
    flatten scores a stricter ruleset means to act on."""
    assert _analysis("charge_payment", 1_000.0).risk_score == 1_000.0


def test_a_nan_producing_analyzer_cannot_reach_a_handler():
    """The end-to-end shape of the fail-open, through the effect boundary."""

    class NaNAnalyzer(ContextAnalyzer):
        def analyze(
            self,
            action,
            context,
            emotional_state="unknown",
            risk_profile=None,
            environmental_context=None,
        ):
            return _analysis(action, float("nan"), emotional_state)

    performed: list[int] = []
    orchestrator = SETTOrchestrator()
    orchestrator._ethical_filter.register_analyzer("charge", NaNAnalyzer())
    executor = SETTExecutor()
    orchestrator.register_executor(executor)
    executor.register_handler(
        "charge", lambda payload: performed.append(1) or {"done": True}
    )

    with pytest.raises(SETTValidationError):
        executor.submit(Action("charge", {"amount": 10_000_000}))

    assert performed == []


# ── what policy approved is what runs ───────────────────────────────────────


class _GatedAnalyzer(ContextAnalyzer):
    """Holds the evaluation open so the payload can be mutated mid-flight."""

    def __init__(self, seen: dict, gate: threading.Event) -> None:
        self.seen = seen
        self.gate = gate

    def analyze(
        self,
        action,
        context,
        emotional_state="unknown",
        risk_profile=None,
        environmental_context=None,
    ):
        self.seen["policy"] = copy.deepcopy(context)
        self.gate.set()
        time.sleep(0.15)
        return _analysis(action, 0.0, emotional_state)


def test_mutating_an_action_mid_submission_cannot_change_what_runs():
    seen: dict = {}
    gate = threading.Event()

    orchestrator = SETTOrchestrator()
    orchestrator._ethical_filter.register_analyzer(
        "charge", _GatedAnalyzer(seen, gate)
    )
    executor = SETTExecutor()
    orchestrator.register_executor(executor)
    executor.register_handler(
        "charge",
        lambda payload: seen.__setitem__("handler", copy.deepcopy(payload)),
    )

    action = Action("charge", {"amount": 1}, idempotency_key="toctou-1")
    worker = threading.Thread(target=lambda: executor.submit(action))
    worker.start()
    gate.wait(timeout=5)
    action.payload["amount"] = 999  # the caller still holds the object
    worker.join(timeout=5)

    assert seen["policy"]["amount"] == 1
    assert seen["handler"]["amount"] == 1


def test_mutating_a_published_result_mid_write_cannot_change_what_is_stored():
    seen: dict = {}
    gate = threading.Event()

    orchestrator = SETTOrchestrator()
    orchestrator._ethical_filter.register_analyzer(
        "memory_write", _GatedAnalyzer(seen, gate)
    )
    payload = {"value": "safe"}

    class Publisher(SETTAgent):
        def __init__(self) -> None:
            super().__init__(name="publisher", domain="publisher")

        def process(self, input_data):
            self._publish_to_universal(payload)
            return {}

    orchestrator.register_agent(Publisher())
    worker = threading.Thread(
        target=lambda: orchestrator.process({}, domain="publisher")
    )
    worker.start()
    gate.wait(timeout=5)
    payload["value"] = "forbidden"
    worker.join(timeout=5)

    assert "forbidden" not in str(orchestrator.read_universal_memory())


def test_the_fingerprint_describes_the_payload_that_was_performed():
    """The idempotency half: the key must not be reserved against one
    payload while a different one is executed under it."""
    seen: dict = {}
    gate = threading.Event()
    store = InMemoryIdempotencyStore()

    orchestrator = SETTOrchestrator()
    orchestrator._ethical_filter.register_analyzer(
        "charge", _GatedAnalyzer(seen, gate)
    )
    executor = SETTExecutor(idempotency_store=store)
    orchestrator.register_executor(executor)
    executor.register_handler(
        "charge",
        lambda payload: seen.__setitem__("handler", copy.deepcopy(payload)),
    )

    action = Action("charge", {"amount": 1}, idempotency_key="toctou-2")
    worker = threading.Thread(
        target=lambda: executor.submit_controlled(action)
    )
    worker.start()
    gate.wait(timeout=5)
    action.payload["amount"] = 999
    worker.join(timeout=5)

    from sett.core_ruler.idempotency import action_fingerprint

    recorded = store.get("toctou-2").fingerprint
    performed = Action(
        "charge", seen["handler"], idempotency_key="toctou-2"
    )
    assert recorded == action_fingerprint(performed)


# ── separators must not decide ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "api.key", "api/key", "api|key", "api:key", "api key",
        "api-key", "api_key", "apiKey", "API.KEY",
        "private.key", "private/key", "x-api-key",
    ],
)
def test_a_sensitive_key_is_refused_under_any_separator(key):
    with pytest.raises(SETTValidationError):
        ExecutionContext.create(metadata={key: "SUPERSECRET"})


def test_an_ordinary_key_is_still_accepted():
    context = ExecutionContext.create(metadata={"turn_index": 3})
    assert context.metadata_dict() == {"turn_index": 3}


# ── attempt_number counts against the key, not against one call ────────────
#
# Audit #3 reported this as reproduced ([1, 1] handed to the handler while
# the store's own attempt_count went 1 -> 2), then the response round
# could not reproduce it and left it pending. The miss traces to a
# RetryPolicy built without retry_on=: with the default empty tuple,
# classify_error() marks the failure PERMANENT rather than TRANSIENT,
# mark_failed(retryable=False) leaves the record FAILED_FINAL instead of
# FAILED_RETRYABLE, and the second submit_controlled() call is refused at
# the reservation step before the handler ever runs a second time, which
# reads exactly like "could not reproduce" without the bug being fictional.
# These tests set retry_on explicitly, which is what actually exercises it.


def test_attempt_number_climbs_across_separate_submissions_of_the_same_key():
    seen: list[int] = []
    calls = {"n": 0}

    def handler(payload, context):
        seen.append(context.attempt_number)
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("fails once, then the caller resubmits")
        return {"ok": True}

    orchestrator = SETTOrchestrator()
    executor = SETTExecutor()
    orchestrator.register_executor(executor)
    executor.register_controlled_handler("charge", handler)

    policy = RetryPolicy(max_attempts=1, retry_on=(ValueError,))
    action = Action("charge", {"amount": 100}, idempotency_key="order-m02-a")

    first = executor.submit_controlled(action, retry_policy=policy)
    second = executor.submit_controlled(action, retry_policy=policy)
    record = executor.idempotency_store.get("order-m02-a")

    assert first.state.status is ExecutionStatus.FAILED
    assert second.state.status is ExecutionStatus.SUCCESS
    assert record.attempt_count == 2
    # The bug: this used to be [1, 1]; the store's own count was ignored.
    assert seen == [1, 2]


def test_attempt_number_within_one_submission_is_unaffected():
    """Retries inside a single submit_controlled() call already matched
    the store before this fix, and must keep matching after it."""
    seen: list[int] = []
    calls = {"n": 0}

    def handler(payload, context):
        seen.append(context.attempt_number)
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("fails twice, then succeeds")
        return {"ok": True}

    orchestrator = SETTOrchestrator()
    executor = SETTExecutor()
    orchestrator.register_executor(executor)
    executor.register_controlled_handler("charge", handler)

    policy = RetryPolicy(max_attempts=3, retry_on=(ValueError,))
    action = Action("charge", {"amount": 100}, idempotency_key="order-m02-b")

    result = executor.submit_controlled(action, retry_policy=policy)

    assert result.state.status is ExecutionStatus.SUCCESS
    assert seen == [1, 2, 3]


def test_attempt_number_climbs_across_two_different_executor_instances():
    """The realistic case IdempotencyStore exists for: two callers - two
    requests, two processes - sharing one durable store."""
    shared_store = InMemoryIdempotencyStore()
    seen: list[int] = []
    policy = RetryPolicy(max_attempts=1, retry_on=(ValueError,))
    action = Action("charge", {"amount": 250}, idempotency_key="order-m02-c")

    def make_handler(fails_first):
        calls = {"n": 0}

        def handler(payload, context):
            seen.append(context.attempt_number)
            calls["n"] += 1
            if calls["n"] <= fails_first:
                raise ValueError("still flaky")
            return {"ok": True}

        return handler

    executor_a = SETTExecutor(idempotency_store=shared_store)
    SETTOrchestrator().register_executor(executor_a)
    executor_a.register_controlled_handler("charge", make_handler(fails_first=1))
    executor_a.submit_controlled(action, retry_policy=policy)

    executor_b = SETTExecutor(idempotency_store=shared_store)
    SETTOrchestrator().register_executor(executor_b)
    executor_b.register_controlled_handler("charge", make_handler(fails_first=0))
    executor_b.submit_controlled(action, retry_policy=policy)

    assert seen == [1, 2]
    assert shared_store.get("order-m02-c").attempt_count == 2


def test_without_a_key_attempt_number_is_still_this_submissions_own_count():
    """No key means no cross-submission identity to be authoritative
    about, and retries themselves require a key (retry.key_required), so
    the only reachable shape is a single attempt. Falling back to the
    local count for it is honest, not a regression."""
    seen: list[int] = []

    def handler(payload, context):
        seen.append(context.attempt_number)
        return {"ok": True}

    orchestrator = SETTOrchestrator()
    executor = SETTExecutor()
    orchestrator.register_executor(executor)
    executor.register_controlled_handler("charge", handler)

    action = Action("charge", {"amount": 10})  # no idempotency_key
    result = executor.submit_controlled(action)  # default policy: 1 attempt

    assert result.state.status is ExecutionStatus.SUCCESS
    assert seen == [1]


# ── the environmental namespace is reserved ─────────────────────────────────


def test_a_domain_cannot_collide_with_the_environmental_namespace():
    memory = UniversalMemory()
    memory.set_ethical_filter(EthicalFilter())
    with pytest.raises(SETTValidationError):
        memory.update(agent="__env_ctx__health", result={"x": 1})


def test_an_ordinary_domain_is_unaffected():
    memory = UniversalMemory()
    memory.set_ethical_filter(EthicalFilter())
    memory.update(agent="health", result={"x": 1})
    assert "health" in memory.read_all()


# ── idempotency keys do not travel in exported traces ───────────────────────


def test_an_idempotency_key_is_not_written_out_in_full():
    orchestrator = SETTOrchestrator()
    executor = SETTExecutor()
    orchestrator.register_executor(executor)
    executor.register_handler("notify", lambda payload: {})
    root = ExecutionContext.create()
    orchestrator._trace_recorder.record(
        root,
        kind="trace.started",
        component_type="orchestrator",
        component_name="TestHarness",
        status="started",
    )
    executor.submit_controlled(
        Action(
            "notify",
            {},
            idempotency_key="notification:appointment-992:reminder-24h",
        ),
        execution_context=root,
    )

    exported = str(orchestrator.export_trace(root.trace_id, view="sanitized"))
    assert "appointment-992" not in exported
    assert "idempotency_key_digest" in exported


def test_the_digest_still_correlates_events_for_one_operation():
    from sett.core_ruler.idempotency import key_digest

    assert key_digest("payment:invoice-1") == key_digest("payment:invoice-1")
    assert key_digest("payment:invoice-1") != key_digest("payment:invoice-2")


# ── the bound is named for what it does ─────────────────────────────────────


def test_the_provider_error_helper_is_named_for_bounding_not_redaction():
    from sett import exceptions

    assert hasattr(exceptions, "bound_provider_error_message")
    assert not hasattr(exceptions, "sanitize_provider_error_message"), (
        "The old name promised redaction it never performed."
    )


def test_the_bound_actually_bounds():
    from sett.exceptions import bound_provider_error_message

    bounded = bound_provider_error_message(RuntimeError("x" * 5_000))
    assert len(bounded) < 5_000


# ── history is a record, not a draft ────────────────────────────────────────


def test_private_memory_history_cannot_be_edited_through_its_getter():
    memory = PrivateMemory(owner="agent")
    memory.write("k", {"nested": [1, 2]})

    history = memory.get_history()
    history[0]["key"] = "modified"
    history[0]["injected"] = True

    fresh = memory.get_history()
    assert fresh[0]["key"] == "k"
    assert "injected" not in fresh[0]


# ── Application Default Credentials, in full ────────────────────────────────


def test_the_credential_helper_no_longer_pre_checks_the_environment():
    """ADC resolves from five sources; checking one refused four.

    On GKE that made the framework unusable without a service account key
    file, the credential type Google recommends avoiding there, so the
    guard pushed applications toward the weaker option in the name of
    safety.
    """
    import sett.services_google_auth as auth

    assert not hasattr(auth, "require_google_credentials_configured")
    assert hasattr(auth, "build_google_client")


def test_a_client_that_constructs_without_the_env_var_is_accepted(monkeypatch):
    from sett.services_google_auth import build_google_client

    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    sentinel = object()
    client = build_google_client(
        lambda credentials: sentinel, credentials=None, adapter_name="Test"
    )
    assert client is sentinel


def test_the_client_librarys_refusal_becomes_a_framework_error():
    from sett._adapters.google_auth import build_google_client
    from sett.exceptions import SETTServiceAdapterError

    class DefaultCredentialsError(Exception):
        pass

    def factory(credentials):
        raise DefaultCredentialsError("Your default credentials were not found.")

    with pytest.raises(SETTServiceAdapterError) as excinfo:
        build_google_client(factory, credentials=None, adapter_name="Test")
    assert "credentials" in str(excinfo.value).lower()


# ── docstrings that describe the code ───────────────────────────────────────


def test_from_context_documents_that_a_bad_value_raises():
    """Checks the claim, not the words.

    The docstring quotes its own former "never raises" wording while
    explaining why that stopped being true; the project records
    corrections rather than rewriting them silently, so searching for
    that phrase would flag the explanation. What has to hold is that the
    raising case is documented and that it really raises.
    """
    doc = BiometricReading.from_context.__doc__ or ""
    assert "SETTValidationError" in doc
    with pytest.raises(SETTValidationError):
        BiometricReading.from_context({"heart_rate_bpm": "fast"})


def test_absent_health_data_is_still_not_an_error():
    reading = BiometricReading.from_context({})
    assert reading.heart_rate_bpm is None


def test_the_threshold_modifier_is_documented_with_the_sign_it_returns():
    doc = EnvironmentalContext.filter_threshold_modifier.__doc__ or ""
    assert "non-negative" in doc
    for level in RiskLevel:
        context = EnvironmentalContext(risk_level=level, location_id="l")
        assert context.filter_threshold_modifier >= 0


# ── one duration rule, every call site ──────────────────────────────────────


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True, "5"])
def test_every_duration_entry_point_refuses_the_same_values(bad):
    now = datetime.now(timezone.utc)
    entry_points = [
        lambda v: Deadline.after(v),
        lambda v: LifecyclePolicy(run_timeout_seconds=v),
        lambda v: LifecyclePolicy(agent_timeouts={"a": v}),
        lambda v: RetryPolicy(initial_backoff_seconds=v),
        lambda v: ExecutionState(
            status=ExecutionStatus.SUCCESS,
            started_at=now,
            finished_at=now,
            duration_seconds=v,
        ),
        lambda v: pop_lifecycle_kwargs({"timeout": v}),
    ]
    for build in entry_points:
        with pytest.raises(SETTValidationError):
            build(bad)


def test_zero_is_allowed_where_it_is_meaningful_and_refused_where_it_is_not():
    now = datetime.now(timezone.utc)
    assert pop_lifecycle_kwargs({"timeout": 0.0}).timeout == 0.0
    assert RetryPolicy(initial_backoff_seconds=0.0).initial_backoff_seconds == 0.0
    assert (
        ExecutionState(
            status=ExecutionStatus.SUCCESS,
            started_at=now,
            finished_at=now,
            duration_seconds=0.0,
        ).duration_seconds
        == 0.0
    )
    with pytest.raises(SETTValidationError):
        Deadline.after(0)


def test_there_is_one_duration_validator_not_one_per_module():
    """The fifth copy is how the adapter kwarg ended up the one place
    that still accepted NaN."""
    import inspect

    from sett.core_ruler import execution_result, lifecycle, retry

    assert hasattr(lifecycle, "validate_duration_seconds")
    for module in (retry, execution_result):
        source = inspect.getsource(module)
        assert "math.isfinite" not in source, (
            f"{module.__name__} re-implements the finiteness check instead "
            f"of calling validate_duration_seconds()."
        )


# ── release hygiene and the replacement contract ────────────────────────────


def test_no_unreleased_marker_survives_in_a_released_tree():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    # This file is exempt: it necessarily contains the literal it
    # searches for, the same way the Spanish-detection guard exempts
    # itself from its own pattern.
    self_path = pathlib.Path(__file__).resolve()
    offenders = [
        f"{path.relative_to(root)}:{lineno}"
        for path in root.rglob("*")
        if path.suffix in {".py", ".md", ".toml"}
        and "__pycache__" not in path.parts
        and path.resolve() != self_path
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
        )
        if "[Unreleased]" in line
    ]
    assert not offenders, offenders


def test_replacing_a_stub_is_quiet_and_replacing_a_real_agent_warns(caplog):
    import logging

    class Real(SETTAgent):
        def __init__(self, name: str) -> None:
            super().__init__(name=name, domain="health")

        def process(self, input_data):
            return {}

    orchestrator = SETTOrchestrator()
    with caplog.at_level(logging.WARNING, logger="sett.core_ruler.orchestrator"):
        orchestrator.register_agent(StubDomainAgent("health"))
        orchestrator.register_agent(Real("RealAgent"))
    assert not caplog.records, "swapping a stub for the real agent is documented"

    with caplog.at_level(logging.WARNING, logger="sett.core_ruler.orchestrator"):
        orchestrator.register_agent(Real("AnotherAgent"))
    assert any("replaced" in record.message for record in caplog.records)


def test_register_agent_documents_that_it_replaces():
    doc = SETTOrchestrator.register_agent.__doc__ or ""
    assert "REPLACES" in doc or "replaces" in doc
