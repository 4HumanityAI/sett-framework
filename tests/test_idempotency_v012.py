"""
Phase 5 of v0.12.0: idempotency, attempts and retries at the effect boundary.

This is the phase where getting it wrong charges somebody twice, so the
tests lean on the cases where a naive implementation is silently wrong
rather than loudly broken: a retry whose payload arrived through a
different serialization path, an attempt whose outcome nobody knows, a
rejection somebody tries to retry past the filter.

The rule underneath all of it: SETT does not promise exactly-once,
because nothing talking to an external system can. What it promises is
that it will not repeat a keyed action it has a record for, and that when
it cannot tell whether an effect happened it says so and stops.
"""
from __future__ import annotations

import threading
import time

import pytest

from sett import (
    Action,
    ErrorClass,
    EthicalFilter,
    ExecutionContext,
    ExecutionStatus,
    HandlerContext,
    IdempotencyStatus,
    InMemoryIdempotencyStore,
    RetryPolicy,
    SETTCancelledError,
    SETTConfigurationError,
    SETTEthicalFilterRejectedError,
    SETTError,
    SETTExecutor,
    SETTIdempotencyConflictError,
    SETTOrchestrator,
    SETTTimeoutError,
    SETTValidationError,
    action_fingerprint,
    classify_error,
)
from sett.core_ruler.retry import is_retryable
from sett.ethics_ruler.ethic_kernel.rules import EthicalRuleset


def build(store=None) -> tuple[SETTOrchestrator, SETTExecutor]:
    orchestrator = SETTOrchestrator()
    executor = SETTExecutor(idempotency_store=store) if store else SETTExecutor()
    orchestrator.register_executor(executor)
    return orchestrator, executor


def charge(amount=1500, currency="ARS", key="payment:invoice-2481") -> Action:
    return Action(
        action_type="charge_payment",
        payload={"invoice_id": "invoice-2481", "amount": amount,
                 "currency": currency},
        proposed_by="billing",
        idempotency_key=key,
    )


# ── Fingerprints ────────────────────────────────────────────────────────────


def test_the_same_action_fingerprints_identically():
    assert action_fingerprint(charge()) == action_fingerprint(charge())


def test_an_int_and_an_equal_float_fingerprint_identically():
    """The regression: 1500 and 1500.0 describe the same charge."""
    assert action_fingerprint(charge(amount=1500)) == action_fingerprint(
        charge(amount=1500.0)
    )


def test_true_and_one_do_not_fingerprint_identically():
    """A real type difference, not a serialization artifact."""
    truthy = Action("flag", {"enabled": True}, idempotency_key="k")
    numeric = Action("flag", {"enabled": 1}, idempotency_key="k")
    assert action_fingerprint(truthy) != action_fingerprint(numeric)


def test_a_non_integer_float_is_left_exactly_as_it_is():
    assert action_fingerprint(charge(amount=150.5)) != action_fingerprint(
        charge(amount=150)
    )
    assert action_fingerprint(charge(amount=150.5)) == action_fingerprint(
        charge(amount=150.5)
    )


def test_different_amounts_fingerprint_differently():
    assert action_fingerprint(charge(amount=1500)) != action_fingerprint(
        charge(amount=1600)
    )


def test_key_order_does_not_change_the_fingerprint():
    first = Action("t", {"a": 1, "b": 2}, idempotency_key="k")
    second = Action("t", {"b": 2, "a": 1}, idempotency_key="k")
    assert action_fingerprint(first) == action_fingerprint(second)


def test_a_non_json_safe_payload_fails_closed_instead_of_using_repr():
    """M-05 regression (severe audit).

    The old ``default=repr`` fallback was unstable in both directions: an
    object's default repr() embeds its memory address (the same logical
    payload would never fingerprint the same way twice, so a retry could
    never match its own first attempt), and two different objects can
    share a custom repr() and collide onto the same fingerprint (a
    different operation silently treated as a duplicate). Both are wrong
    for a value that decides whether an external effect happens once or
    twice, so a non-JSON-safe payload is now rejected outright.
    """

    class Unserializable:
        pass

    action = Action(
        "t", {"thing": Unserializable()}, idempotency_key="k"
    )
    with pytest.raises(SETTValidationError):
        action_fingerprint(action)


def test_nested_values_are_canonicalized_too():
    nested_int = Action("t", {"items": [{"amount": 100}]}, idempotency_key="k")
    nested_float = Action(
        "t", {"items": [{"amount": 100.0}]}, idempotency_key="k"
    )
    assert action_fingerprint(nested_int) == action_fingerprint(nested_float)


# ── The store ───────────────────────────────────────────────────────────────


def test_a_first_reservation_is_acquired():
    store = InMemoryIdempotencyStore()
    reservation = store.reserve(key="k", fingerprint="fp", trace_id="t")

    assert reservation.acquired is True
    assert reservation.record.status is IdempotencyStatus.RESERVED
    assert reservation.record.attempt_count == 1


def test_a_second_reservation_of_a_live_key_is_not_acquired():
    store = InMemoryIdempotencyStore()
    store.reserve(key="k", fingerprint="fp", trace_id="t")
    second = store.reserve(key="k", fingerprint="fp", trace_id="t")

    assert second.acquired is False
    assert second.in_progress_elsewhere is True


def test_the_same_key_with_a_different_payload_is_a_conflict():
    store = InMemoryIdempotencyStore()
    store.reserve(key="k", fingerprint="fp-one", trace_id="t")

    with pytest.raises(SETTIdempotencyConflictError) as excinfo:
        store.reserve(key="k", fingerprint="fp-two", trace_id="t")

    error = excinfo.value
    assert error.key == "k"
    assert error.expected_fingerprint == "fp-one"
    assert error.actual_fingerprint == "fp-two"


def test_a_succeeded_key_reports_replay_not_acquisition():
    store = InMemoryIdempotencyStore()
    store.reserve(key="k", fingerprint="fp", trace_id="t")
    store.mark_succeeded(key="k", digest="digest")

    replay = store.reserve(key="k", fingerprint="fp", trace_id="t")
    assert replay.acquired is False
    assert replay.already_succeeded is True
    assert replay.record.result_digest == "digest"


def test_a_retryable_failure_can_be_reserved_again_with_a_new_attempt():
    store = InMemoryIdempotencyStore()
    store.reserve(key="k", fingerprint="fp", trace_id="t")
    store.mark_failed(key="k", retryable=True)

    again = store.reserve(key="k", fingerprint="fp", trace_id="t")
    assert again.acquired is True
    assert again.record.attempt_count == 2


def test_a_final_failure_cannot_be_reserved_again():
    store = InMemoryIdempotencyStore()
    store.reserve(key="k", fingerprint="fp", trace_id="t")
    store.mark_failed(key="k", retryable=False)

    again = store.reserve(key="k", fingerprint="fp", trace_id="t")
    assert again.acquired is False
    assert again.record.status is IdempotencyStatus.FAILED_FINAL


def test_exactly_one_of_two_racing_threads_acquires_the_key():
    store = InMemoryIdempotencyStore()
    results: list[bool] = []
    barrier = threading.Barrier(8)

    def claim() -> None:
        barrier.wait(timeout=5)
        results.append(
            store.reserve(key="k", fingerprint="fp", trace_id="t").acquired
        )

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert results.count(True) == 1
    assert results.count(False) == 7


def test_an_empty_key_is_rejected():
    store = InMemoryIdempotencyStore()
    with pytest.raises(SETTValidationError):
        store.reserve(key="   ", fingerprint="fp", trace_id="t")


def test_an_in_memory_store_forgets_everything_on_restart():
    """Documented as a passing test, not only as a caveat in prose."""
    first = InMemoryIdempotencyStore()
    first.reserve(key="k", fingerprint="fp", trace_id="t")
    first.mark_succeeded(key="k", digest="d")

    restarted = InMemoryIdempotencyStore()  # a fresh process
    assert restarted.get("k") is None
    assert restarted.reserve(
        key="k", fingerprint="fp", trace_id="t"
    ).acquired is True


# ── Retry policy and error classification ───────────────────────────────────


def test_the_default_policy_makes_exactly_one_attempt():
    policy = RetryPolicy()
    assert policy.max_attempts == 1
    assert policy.retry_on == ()
    assert policy.retries_enabled is False


def test_raising_max_attempts_alone_never_causes_a_retry():
    """Attempt count and "repeating is safe" are separate statements."""
    policy = RetryPolicy(max_attempts=5)
    assert policy.retries_enabled is False
    assert is_retryable(ConnectionError("down"), policy) is False


def test_an_error_listed_in_retry_on_is_retryable():
    policy = RetryPolicy(max_attempts=3, retry_on=(ConnectionError,))
    assert is_retryable(ConnectionError("down"), policy) is True
    assert classify_error(ConnectionError("down"), policy) is ErrorClass.TRANSIENT


def test_an_unknown_error_is_not_retried():
    policy = RetryPolicy(max_attempts=3, retry_on=(ConnectionError,))
    assert is_retryable(RuntimeError("who knows"), policy) is False


@pytest.mark.parametrize(
    "error",
    [
        SETTEthicalFilterRejectedError("blocked"),
        SETTCancelledError("stopped"),
        SETTTimeoutError("out of time"),
        SETTIdempotencyConflictError("different payload"),
        SETTConfigurationError("no handler"),
        SETTValidationError("bad value"),
    ],
)
def test_a_policy_cannot_make_the_never_retryable_errors_retryable(error):
    """Retrying a rejection would be asking the filter again until it says yes."""
    policy = RetryPolicy(max_attempts=5, retry_on=(type(error),))
    assert is_retryable(error, policy) is False


def test_a_deadline_is_classified_as_ambiguous_not_permanent():
    """The request may already have arrived on the other side."""
    assert classify_error(SETTTimeoutError("gone")) is ErrorClass.AMBIGUOUS


def test_backoff_grows_and_is_capped():
    policy = RetryPolicy(
        max_attempts=6,
        initial_backoff_seconds=1.0,
        backoff_multiplier=2.0,
        max_backoff_seconds=5.0,
        retry_on=(ConnectionError,),
    )
    assert policy.backoff_for(1) == 1.0
    assert policy.backoff_for(2) == 2.0
    assert policy.backoff_for(3) == 4.0
    assert policy.backoff_for(4) == 5.0  # capped


def test_a_policy_with_no_backoff_waits_not_at_all():
    assert RetryPolicy(max_attempts=3).backoff_for(1) == 0.0


def test_policy_validation():
    with pytest.raises(SETTValidationError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(SETTValidationError):
        RetryPolicy(initial_backoff_seconds=-1.0)
    with pytest.raises(SETTValidationError):
        RetryPolicy(retry_on=("ConnectionError",))
    with pytest.raises(SETTValidationError):
        RetryPolicy(initial_backoff_seconds=1.0, backoff_multiplier=0.5)


def test_max_attempts_must_be_an_int_not_a_float_or_a_bool():
    """M-02 regression (severe audit).

    max_attempts=2.5 used to pass validation (it is >= 1) and only fail
    later, inside the attempt loop's range(1, max_attempts + 1), with a
    bare TypeError that named neither RetryPolicy nor the value that
    caused it. bool is technically an int subclass but never a meaningful
    attempt count.
    """
    with pytest.raises(SETTValidationError):
        RetryPolicy(max_attempts=2.5)
    with pytest.raises(SETTValidationError):
        RetryPolicy(max_attempts=True)


def test_backoff_fields_reject_nan_and_infinity():
    """M-03 regression (severe audit)."""
    with pytest.raises(SETTValidationError):
        RetryPolicy(initial_backoff_seconds=float("nan"))
    with pytest.raises(SETTValidationError):
        RetryPolicy(max_backoff_seconds=float("inf"))
    with pytest.raises(SETTValidationError):
        RetryPolicy(
            initial_backoff_seconds=1.0, backoff_multiplier=float("nan")
        )


# ── submit_controlled(): the happy path and the guarantees ──────────────────


def test_a_controlled_submission_runs_the_handler_and_returns_its_value():
    orchestrator, executor = build()
    executor.register_handler("charge_payment", lambda payload: {"ok": True})

    result = executor.submit_controlled(charge())

    assert result.succeeded is True
    assert result.unwrap() == {"ok": True}
    assert result.state.attempt_count == 1


def test_submit_is_unchanged_and_still_returns_the_bare_value():
    orchestrator, executor = build()
    executor.register_handler("charge_payment", lambda payload: {"ok": True})

    assert executor.submit(charge()) == {"ok": True}


def test_a_replayed_key_never_runs_the_handler_twice():
    calls: list[int] = []
    orchestrator, executor = build()
    executor.register_handler(
        "charge_payment", lambda payload: calls.append(1) or {"ok": True}
    )

    first = executor.submit_controlled(charge())
    second = executor.submit_controlled(charge())

    assert first.succeeded is True
    assert second.succeeded is True
    assert second.reason_code == "idempotency.replayed"
    # The value is not invented: the store keeps a digest, not the result.
    assert second.value is None
    assert len(calls) == 1


def test_the_same_key_with_a_different_payload_fails_closed():
    orchestrator, executor = build()
    executor.register_handler("charge_payment", lambda payload: {"ok": True})
    executor.submit_controlled(charge(amount=1500))

    conflict = executor.submit_controlled(charge(amount=9999))

    assert conflict.status is ExecutionStatus.FAILED
    assert conflict.reason_code == "idempotency.conflict"
    with pytest.raises(SETTIdempotencyConflictError):
        conflict.unwrap()


def test_a_retry_with_an_equal_amount_serialized_differently_is_not_a_conflict():
    """The int/float regression, end to end through the executor."""
    orchestrator, executor = build()
    executor.register_handler("charge_payment", lambda payload: {"ok": True})
    executor.submit_controlled(charge(amount=1500))

    replay = executor.submit_controlled(charge(amount=1500.0))

    assert replay.succeeded is True
    assert replay.reason_code == "idempotency.replayed"


def test_an_action_without_a_key_is_never_replayed():
    calls: list[int] = []
    orchestrator, executor = build()
    executor.register_handler(
        "notify", lambda payload: calls.append(1) or {"sent": True}
    )
    action = Action("notify", {"to": "someone"})

    executor.submit_controlled(action)
    executor.submit_controlled(action)

    assert len(calls) == 2


def test_a_retry_policy_without_an_idempotency_key_fails_closed():
    """Retrying an action nobody can identify is how one request becomes two."""
    orchestrator, executor = build()
    executor.register_handler("notify", lambda payload: {"sent": True})

    result = executor.submit_controlled(
        Action("notify", {"to": "someone"}),
        retry_policy=RetryPolicy(max_attempts=3, retry_on=(ConnectionError,)),
    )

    assert result.status is ExecutionStatus.FAILED
    assert result.reason_code == "retry.key_required"
    with pytest.raises(SETTConfigurationError):
        result.unwrap()


# ── Attempts and retries ────────────────────────────────────────────────────


def test_a_transient_failure_is_retried_and_can_succeed():
    attempts: list[int] = []

    def flaky(payload):
        attempts.append(1)
        if len(attempts) < 2:
            raise ConnectionError("provider blipped")
        return {"ok": True}

    orchestrator, executor = build()
    executor.register_handler("charge_payment", flaky)

    result = executor.submit_controlled(
        charge(),
        retry_policy=RetryPolicy(max_attempts=3, retry_on=(ConnectionError,)),
    )

    assert result.succeeded is True
    assert result.state.attempt_count == 2
    assert len(attempts) == 2


def test_every_attempt_is_recorded_against_the_same_key():
    store = InMemoryIdempotencyStore()
    attempts: list[int] = []

    def flaky(payload):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("provider blipped")
        return {"ok": True}

    orchestrator, executor = build(store)
    executor.register_handler("charge_payment", flaky)
    executor.submit_controlled(
        charge(),
        retry_policy=RetryPolicy(max_attempts=3, retry_on=(ConnectionError,)),
    )

    logged = store.attempts_for("payment:invoice-2481")
    assert [attempt.attempt_number for attempt in logged] == [1, 2, 3]
    assert [attempt.status for attempt in logged] == [
        "failed",
        "failed",
        "completed",
    ]
    assert {attempt.attempt_id for attempt in logged}.__len__() == 3
    # No payload is kept anywhere in the attempt log.
    assert all(
        not hasattr(attempt, "payload") for attempt in logged
    )


def test_all_attempts_failing_ends_as_failed_with_the_real_error():
    def always_broken(payload):
        raise ConnectionError("provider is down")

    orchestrator, executor = build()
    executor.register_handler("charge_payment", always_broken)

    result = executor.submit_controlled(
        charge(),
        retry_policy=RetryPolicy(max_attempts=3, retry_on=(ConnectionError,)),
    )

    assert result.status is ExecutionStatus.FAILED
    assert result.state.attempt_count == 3
    with pytest.raises(ConnectionError, match="provider is down"):
        result.unwrap()


def test_an_ambiguous_outcome_is_never_retried():
    """A request that timed out after being sent may already have arrived."""
    calls: list[int] = []

    def times_out(payload):
        calls.append(1)
        raise SETTTimeoutError("the provider stopped answering")

    orchestrator, executor = build()
    executor.register_handler("charge_payment", times_out)

    result = executor.submit_controlled(
        charge(),
        retry_policy=RetryPolicy(max_attempts=3, retry_on=(SETTTimeoutError,)),
    )

    assert len(calls) == 1
    assert result.timed_out is True
    assert result.reason_code == "action.outcome_unknown"


def test_an_ambiguous_outcome_leaves_the_key_finally_failed():
    store = InMemoryIdempotencyStore()

    def times_out(payload):
        raise SETTTimeoutError("the provider stopped answering")

    orchestrator, executor = build(store)
    executor.register_handler("charge_payment", times_out)
    executor.submit_controlled(charge())

    record = store.get("payment:invoice-2481")
    assert record.status is IdempotencyStatus.FAILED_FINAL


def test_a_rejection_is_never_retried_and_is_not_a_failure():
    calls: list[int] = []
    orchestrator = SETTOrchestrator(
        ethical_filter=EthicalFilter(
            ruleset=EthicalRuleset(
                name="strict_test", reject_threshold=0.1, warn_threshold=0.05
            )
        )
    )
    executor = SETTExecutor()
    orchestrator.register_executor(executor)
    executor.register_handler(
        "charge_payment", lambda payload: calls.append(1) or {"ok": True}
    )

    result = executor.submit_controlled(
        charge(),
        emotional_state="crisis",
        retry_policy=RetryPolicy(
            max_attempts=3, retry_on=(SETTEthicalFilterRejectedError,)
        ),
    )

    assert calls == []  # the handler never ran
    assert result.status is ExecutionStatus.REJECTED
    assert result.status is not ExecutionStatus.FAILED
    with pytest.raises(SETTEthicalFilterRejectedError):
        result.unwrap()


def test_backoff_is_cancellable():
    orchestrator, executor = build()
    control_box: dict = {}

    def flaky(payload):
        raise ConnectionError("provider blipped")

    executor.register_handler("charge_payment", flaky)
    context = ExecutionContext.create()

    def cancel_soon():
        time.sleep(0.05)
        control_box["source"].cancel(reason="execution.user_stopped")

    from sett.core_ruler.lifecycle import ExecutionControl, lifecycle_scope

    control = ExecutionControl.create_root(context)
    with control, lifecycle_scope(control):
        control_box["source"] = control.source
        canceller = threading.Thread(target=cancel_soon)
        canceller.start()
        started = time.monotonic()
        result = executor.submit_controlled(
            charge(),
            retry_policy=RetryPolicy(
                max_attempts=5,
                initial_backoff_seconds=10.0,
                retry_on=(ConnectionError,),
            ),
        )
        canceller.join(timeout=5)

    assert result.cancelled is True
    assert time.monotonic() - started < 3.0


def test_a_backoff_that_would_outlast_the_deadline_is_not_taken():
    def flaky(payload):
        raise ConnectionError("provider blipped")

    orchestrator, executor = build()
    executor.register_handler("charge_payment", flaky)

    started = time.monotonic()
    result = executor.submit_controlled(
        charge(),
        retry_policy=RetryPolicy(
            max_attempts=5,
            initial_backoff_seconds=30.0,
            retry_on=(ConnectionError,),
        ),
        timeout_seconds=0.5,
    )

    assert result.reason_code == "retry.deadline_exhausted"
    assert time.monotonic() - started < 3.0


# ── HandlerContext ──────────────────────────────────────────────────────────


def test_a_controlled_handler_receives_its_attempt_context():
    seen: list[HandlerContext] = []

    def handler(payload, context):
        seen.append(context)
        return {"ok": True}

    orchestrator, executor = build()
    executor.register_controlled_handler("charge_payment", handler)

    result = executor.submit_controlled(charge(), timeout_seconds=30.0)

    assert result.succeeded is True
    context = seen[0]
    assert context.idempotency_key == "payment:invoice-2481"
    assert context.attempt_number == 1
    assert context.timeout_seconds is not None
    assert context.timeout_seconds <= 30.0
    assert context.cancellation_token is not None
    assert context.cancellation_token.is_cancelled is False


def test_the_attempt_number_increases_across_retries():
    numbers: list[int] = []

    def handler(payload, context):
        numbers.append(context.attempt_number)
        if context.attempt_number < 3:
            raise ConnectionError("provider blipped")
        return {"ok": True}

    orchestrator, executor = build()
    executor.register_controlled_handler("charge_payment", handler)
    executor.submit_controlled(
        charge(),
        retry_policy=RetryPolicy(max_attempts=3, retry_on=(ConnectionError,)),
    )

    assert numbers == [1, 2, 3]


def test_the_key_the_handler_receives_is_the_one_it_passes_to_the_provider():
    provider_calls: list[str] = []

    def handler(payload, context):
        provider_calls.append(context.idempotency_key)
        return {"ok": True}

    orchestrator, executor = build()
    executor.register_controlled_handler("charge_payment", handler)
    executor.submit_controlled(charge())

    assert provider_calls == ["payment:invoice-2481"]


def test_a_controlled_handler_reached_through_plain_submit_fails_closed():
    """Degrading the guarantees silently is worse than refusing."""
    orchestrator, executor = build()
    executor.register_controlled_handler(
        "charge_payment", lambda payload, context: {"ok": True}
    )

    with pytest.raises(SETTConfigurationError, match="submit_controlled"):
        executor.submit(charge())


def test_a_legacy_handler_still_works_under_submit_controlled():
    orchestrator, executor = build()
    executor.register_handler("charge_payment", lambda payload: {"ok": True})

    result = executor.submit_controlled(charge())

    assert result.unwrap() == {"ok": True}
    assert current_handler_context_is_clear()


def current_handler_context_is_clear() -> bool:
    from sett.core_ruler.executor import current_handler_context

    return current_handler_context() is None


def test_the_lifecycle_policy_supplies_a_per_handler_timeout():
    """H-03 regression: handler_timeouts must actually bound an attempt.

    Before this fix, SETTExecutor never received the orchestrator's
    LifecyclePolicy at all, so ``handler_timeouts={"charge_payment": ...}``
    had no effect: only an explicit ``timeout_seconds=`` argument to
    submit_controlled() (or the ambient control's own deadline) ever
    bounded a handler attempt.
    """
    from sett import LifecyclePolicy
    from sett.core_ruler.lifecycle import current_execution_control

    started = threading.Event()

    def slow_handler(payload, context):
        started.set()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            control = current_execution_control()
            if control is not None:
                control.checkpoint()
            time.sleep(0.01)
        return {"ok": True}

    orchestrator = SETTOrchestrator(
        lifecycle_policy=LifecyclePolicy(
            handler_timeouts={"charge_payment": 0.05}
        )
    )
    executor = SETTExecutor()
    orchestrator.register_executor(executor)
    executor.register_controlled_handler("charge_payment", slow_handler)

    result = executor.submit_controlled(charge())

    assert result.timed_out is True


def test_an_explicit_submit_controlled_timeout_overrides_the_policy():
    from sett import LifecyclePolicy

    orchestrator = SETTOrchestrator(
        lifecycle_policy=LifecyclePolicy(
            handler_timeouts={"charge_payment": 0.05}
        )
    )
    executor = SETTExecutor()
    orchestrator.register_executor(executor)
    executor.register_controlled_handler(
        "charge_payment", lambda payload, context: {"ok": True}
    )

    result = executor.submit_controlled(charge(), timeout_seconds=30.0)

    assert result.succeeded is True


def test_a_cleanup_failure_during_submit_controlled_is_not_dropped():
    """M-01 regression (severe audit): the state used to be built from
    inside `with control`, before control.__exit__() had run cleanup at
    all, so control.cleanup_errors was necessarily empty at that point
    no matter what actually happened during unwinding, and a hook that
    failed while cleaning up was silently missing from the result.
    """
    from sett.core_ruler.lifecycle import current_execution_control

    def handler(payload, context):
        control = current_execution_control()
        if control is not None:

            def _failing_cleanup():
                raise RuntimeError("cleanup blew up")

            control.register_cleanup(_failing_cleanup)
        return {"ok": True}

    orchestrator, executor = build()
    executor.register_controlled_handler("charge_payment", handler)

    result = executor.submit_controlled(charge())

    assert result.succeeded is True
    assert result.state.cleanup_failed is True
    assert result.state.cleanup_errors == ("RuntimeError",)


def test_a_handler_can_observe_cancellation_through_its_context():
    started = threading.Event()

    def slow_handler(payload, context):
        started.set()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            context.throw_if_cancelled()
            time.sleep(0.01)
        return {"ok": True}

    orchestrator, executor = build()
    executor.register_controlled_handler("charge_payment", slow_handler)
    context = ExecutionContext.create()

    from sett.core_ruler.lifecycle import ExecutionControl, lifecycle_scope

    control = ExecutionControl.create_root(context)
    with control, lifecycle_scope(control):
        canceller = threading.Thread(
            target=lambda: (
                started.wait(timeout=5),
                control.cancel(reason="execution.user_stopped"),
            )
        )
        canceller.start()
        result = executor.submit_controlled(charge())
        canceller.join(timeout=5)

    assert result.cancelled is True
    assert result.reason_code == "execution.user_stopped"


# ── Traces ──────────────────────────────────────────────────────────────────


def test_attempts_and_idempotency_appear_in_the_trace_and_it_verifies():
    orchestrator, executor = build()
    executor.register_handler("charge_payment", lambda payload: {"ok": True})
    root = ExecutionContext.create()
    orchestrator._trace_recorder.record(
        root,
        kind="trace.started",
        component_type="orchestrator",
        component_name="TestHarness",
        status="started",
    )
    executor.submit_controlled(charge(), execution_context=root)
    orchestrator._trace_recorder.record(
        root,
        kind="trace.completed",
        component_type="orchestrator",
        component_name="TestHarness",
        status="completed",
        cause_id=orchestrator._trace_recorder.last_event_id(root.run_id),
    )

    kinds = [
        event.kind for event in orchestrator.get_trace(root.trace_id)
    ]
    assert "idempotency.reserved" in kinds
    assert "handler.attempt_started" in kinds
    assert "handler.attempt_completed" in kinds
    assert orchestrator.verify_traces(root.trace_id) is True
