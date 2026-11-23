"""Governance journal observations; no real governance producer.

Failure callbacks below are labeled test injections at storage/integrity
boundaries. They do not replace the reference persistence implementations.
"""
from __future__ import annotations

import importlib
import inspect
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from sett.audit_ruler.integrity import NoDigestPolicy, Sha256IntegrityPolicy
from sett.exceptions import (
    SETTPersistenceContinuityError,
    SETTPersistenceError,
    SETTPersistenceFormatError,
    SETTPersistencePositionError,
)
from sett.persistence import (
    DurabilityLevel,
    InMemoryEvidenceStream,
    SQLitePersistenceBackend,
    StreamPosition,
)
from sett.persistence.formats import encode_stream_position
from sett.policy_ruler import (
    GovernanceEntry,
    GovernanceJournal,
    GovernanceReadResult,
    GovernanceReconciliation,
    GovernanceRecord,
    GovernanceSubmission,
    GovernanceWriteResult,
)

_NAMES = (
    "GovernanceEntry", "GovernanceJournal", "GovernanceReadResult",
    "GovernanceReconciliation", "GovernanceRecord", "GovernanceSubmission",
    "GovernanceWriteResult",
)
_PREFIX = "policy.journal_"
_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class ProbeStream:
    """Explicit boundary injection around an unchanged memory stream."""

    def __init__(self, stream=None):
        self.inner = stream or InMemoryEvidenceStream(
            backend_id="backend.journal", stream_id="governance"
        )
        self.before = {}
        self.after = {}
        self.calls = []

    def _invoke(self, name, action):
        self.calls.append(name)
        if name in self.before:
            self.before[name]()
        result = action()
        if name in self.after:
            result = self.after[name](result)
        return result

    def head(self):
        return self._invoke("head", self.inner.head)

    def read(self, *, after=None):
        return self._invoke("read", lambda: self.inner.read(after=after))

    def append(self, **kwargs):
        return self._invoke("append", lambda: self.inner.append(**kwargs))

    def flush(self, *, through):
        return self._invoke("flush", lambda: self.inner.flush(through=through))


class ProbePolicy:
    """Explicit failure/return injection around the existing SHA policy."""

    policy_id = "test.sha.v1"
    algorithm = "sha256"

    def __init__(self):
        self.delegate = Sha256IntegrityPolicy()
        self.digest_hook = None
        self.verify_hook = None

    def digest(self, payload):
        if self.digest_hook is not None:
            return self.digest_hook(payload)
        return self.delegate.digest(payload)

    def verify(self, payload, expected):
        if self.verify_hook is not None:
            return self.verify_hook(payload, expected)
        return self.delegate.verify(payload, expected)


def record(operation="operation.1", payload=b"opaque", **changes):
    values = dict(operation_id=operation, schema_id="uninterpreted.schema", schema_version=7, payload=payload)
    values.update(changes)
    return GovernanceRecord(**values)


def journal(stream, *, origin=None, **changes):
    values = dict(
        namespace_id="namespace.test", journal_id="journal.test",
        stream=stream, origin=stream.head() if origin is None else origin,
        required_durability=DurabilityLevel.IN_MEMORY,
        integrity_policy=Sha256IntegrityPolicy(),
        historical_integrity_policies=(),
        max_frame_bytes=65536, max_records=16, max_total_bytes=1048576,
    )
    values.update(changes)
    return GovernanceJournal(**values)


def prepared(**changes):
    stream = ProbeStream()
    origin = stream.head()
    value = journal(stream, origin=origin, **changes)
    submission = value.prepare(record(), expected_head=origin)
    return stream, value, submission


def throw(error):
    raise error


def invalid(action):
    with pytest.raises(SETTPersistenceFormatError) as caught:
        action()
    error = caught.value
    assert error.reason_code == _PREFIX + "invalid"
    assert error.backend_id is None
    assert error.__cause__ is None
    assert error.__context__ is None
    return error


def seed(stream, submission):
    return stream.inner.append(
        write_id=submission.write_id, payload=submission.frame,
        expected_head=submission.expected_head,
    )


def test_export_identity_and_no_root_authorization_exports():
    root = importlib.import_module("sett")
    authorization = importlib.import_module("sett.authorization_ruler")
    policy = importlib.import_module("sett.policy_ruler")
    module = importlib.import_module("sett.policy_ruler.persistence")
    assert set(module.__all__) == set(_NAMES)
    for name in _NAMES:
        assert getattr(policy, name) is getattr(module, name)
        assert getattr(module, name).__module__ == module.__name__
        assert not hasattr(root, name)
        assert not hasattr(authorization, name)


@pytest.mark.parametrize("first", ("sett.policy_ruler", "sett.authorization_ruler", "sett.persistence"))
def test_fresh_import_orders(first):
    code = (
        "import importlib; importlib.import_module(" + repr(first) + "); "
        "import sett.policy_ruler as p; import sett.policy_ruler.persistence as j; "
        "assert p.GovernanceJournal is j.GovernanceJournal"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=Path(__file__).parents[1],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


@pytest.mark.parametrize("changes", (
    {"operation_id": ""}, {"operation_id": "UPPER"}, {"operation_id": "a" * 257},
    {"schema_id": "\ud800"}, {"schema_version": True}, {"schema_version": 0},
    {"schema_version": 2**32}, {"payload": bytearray(b"x")},
    {"schema_id": type("Text", (str,), {})("schema")},
    {"payload": type("Bytes", (bytes,), {})(b"x")},
))
def test_local_admission(changes):
    invalid(lambda: record(**changes))


def test_exact_record_submission_and_position_types():
    stream, value, submission = prepared()
    derived_record = type("DerivedRecord", (GovernanceRecord,), {})(
        operation_id="operation.1", schema_id="schema", schema_version=1, payload=b"x"
    )
    invalid(lambda: value.prepare(derived_record, expected_head=submission.expected_head))
    derived_submission = type("DerivedSubmission", (GovernanceSubmission,), {})(
        record=submission.record, expected_head=submission.expected_head,
        write_id=submission.write_id, frame=submission.frame,
    )
    invalid(lambda: value.append(derived_submission))
    pos = submission.expected_head
    derived_position = type("DerivedPosition", (StreamPosition,), {})(pos.backend_id, pos.stream_id, pos.token)
    invalid(lambda: value.prepare(record(), expected_head=derived_position))
    assert "append" not in stream.calls


@pytest.mark.parametrize("changes", (
    {"required_durability": DurabilityLevel.UNKNOWN},
    {"max_frame_bytes": True}, {"max_records": 0}, {"max_total_bytes": 1},
    {"max_records": 2**32}, {"max_frame_bytes": 2**32}, {"max_total_bytes": 2**64},
    {"historical_integrity_policies": []},
    {"historical_integrity_policies": (Sha256IntegrityPolicy(),)},
))
def test_explicit_configuration(changes):
    invalid(lambda: journal(ProbeStream(), **changes))
    signature = inspect.signature(GovernanceJournal)
    assert all(p.default is inspect.Parameter.empty for p in signature.parameters.values())


def test_independent_golden_frame_and_identity():
    stream = ProbeStream()
    origin = stream.head()
    value = journal(stream, origin=origin)
    item = record(payload=b"")

    def text(value):
        encoded = value.encode("utf-8")
        return len(encoded).to_bytes(2, "big") + encoded

    def blob(value):
        return len(value).to_bytes(4, "big") + value

    body = b"".join((
        text("namespace.test"), text("journal.test"), text(item.operation_id),
        text(item.schema_id), (7).to_bytes(4, "big"),
        blob(encode_stream_position(origin)), blob(encode_stream_position(origin)),
        text("sett.sha256.v1"), text("sha256"), blob(b""),
    ))
    preimage = b"SETTGOV\0\x00\x01" + blob(body)
    expected = preimage + b"\x01" + blob(sha256(preimage).digest())
    submission = value.prepare(item, expected_head=origin)
    assert submission.frame == expected
    assert submission.write_id == "governance.v1." + sha256(
        b"SETT-GOVERNANCE-WRITE\x00v1\x00" + text("namespace.test")
        + text("journal.test") + text(item.operation_id)
    ).hexdigest()
    assert value.prepare(item, expected_head=origin) == submission
    changed = value.prepare(replace(item, payload=b"different"), expected_head=origin)
    assert changed.write_id == submission.write_id
    assert changed.frame != submission.frame
    assert "append" not in stream.calls


@pytest.mark.parametrize("mutation,reason", (
    (lambda b: b[:7], "format_invalid"),
    (lambda b: b + b"x", "format_invalid"),
    (lambda b: b[:8] + b"\x00\x02" + b[10:], "version_unsupported"),
    (lambda b: b[:10] + b"\xff" * 4 + b[14:], "format_invalid"),
    (lambda b: b[:-37] + b"\x02" + b[-36:], "format_invalid"),
))
def test_bad_frames_are_not_repaired(mutation, reason):
    stream, value, submission = prepared()
    altered = mutation(submission.frame)
    stream.inner.append(write_id=submission.write_id, payload=altered, expected_head=submission.expected_head)
    result = value.read()
    assert not result.complete
    assert result.reason_code == _PREFIX + reason
    assert result.entries == ()
    assert stream.inner.read()[0].payload == altered


@pytest.mark.parametrize("schema,version,payload", (("unknown.schema", 99, b""), ("decision", 1, b"not a decision")))
def test_domain_schemas_remain_uninterpreted(schema, version, payload):
    _stream, value, original = prepared()
    item = record(schema_id=schema, schema_version=version, payload=payload)
    submission = value.prepare(item, expected_head=original.expected_head)
    result = value.append(submission)
    assert result.state == "confirmed"
    assert result.entry.schema_status == "not_interpreted"
    assert result.entry.record.payload == payload


@pytest.mark.parametrize("changes", (
    {"namespace_id": "another"}, {"journal_id": "another"},
    {"origin": StreamPosition("another", "governance", "0")},
    {"origin": StreamPosition("backend.journal", "another", "0")},
    {"origin": StreamPosition("backend.journal", "governance", "wrong-origin")},
))
def test_identity_isolation(changes):
    stream, value, submission = prepared()
    assert value.append(submission).state == "confirmed"
    other = journal(stream, origin=changes.pop("origin", submission.expected_head), **changes)
    result = other.read()
    assert not result.complete
    assert result.reason_code == _PREFIX + "identity_mismatch"
    independent = ProbeStream()
    assert journal(independent).read().complete


@pytest.mark.parametrize("mode", ("predecessor", "duplicate", "origin_position", "terminal"))
def test_continuity_and_duplicate_observations(mode):
    stream, value, first = prepared()
    initial = value.append(first)
    second = value.prepare(record("operation.2"), expected_head=initial.entry.position)
    assert value.append(second).state == "confirmed"
    rows = tuple(stream.inner.read())
    if mode == "predecessor":
        wrong = value.prepare(record("operation.2"), expected_head=first.expected_head)
        stream.after["read"] = lambda _: (rows[0], replace(rows[1], payload=wrong.frame))
    elif mode == "duplicate":
        stream.after["read"] = lambda _: (rows[0], rows[0])
    elif mode == "origin_position":
        stream.after["read"] = lambda _: (replace(rows[0], position=first.expected_head),)
    else:
        stream.after["read"] = lambda _: rows[:1]
    result = value.read()
    assert not result.complete
    assert result.reason_code == _PREFIX + "continuity_invalid"


def test_opaque_positions_are_not_sorted():
    stream, value, first = prepared()
    seed(stream, first)
    actual = stream.inner.read()[0]
    opaque = replace(actual.position, token="not-a-number")
    stream.after["read"] = lambda _: (replace(actual, position=opaque),)
    stream.after["head"] = lambda _: opaque
    assert value.read().complete


def test_empty_origin_and_no_hidden_history():
    stream, value, submission = prepared()
    empty = value.read()
    assert empty.complete and empty.entries == () and empty.observed_head == submission.expected_head
    position = seed(stream, submission).position
    later_origin = journal(stream, origin=position)
    assert not later_origin.read().complete
    stream.after["read"] = lambda _: ()
    assert not value.read().complete


@pytest.mark.parametrize("mode", ("changed", "failed"))
def test_head_sampling_never_retries(mode):
    stream, value, submission = prepared()
    count = [0]

    def head(observed):
        count[0] += 1
        if count[0] == 2:
            if mode == "failed":
                raise RuntimeError("private failure")
            return replace(observed, token="changed")
        return observed

    stream.after["head"] = head
    result = value.read()
    assert not result.complete
    assert result.reason_code == _PREFIX + ("head_changed" if mode == "changed" else "storage_failed")
    assert result.observed_head == (submission.expected_head if mode == "failed" else replace(submission.expected_head, token="changed"))
    assert count == [2]


@pytest.mark.parametrize("limit", ("frame", "count", "aggregate"))
def test_independent_resource_boundaries(limit):
    stream, value, first = prepared()
    one = value.append(first)
    second = value.prepare(record("operation.2"), expected_head=one.entry.position)
    assert value.append(second).state == "confirmed"
    assert value.read().complete
    assert len(first.frame) == len(second.frame)
    frame_size = len(first.frame)
    config = dict(max_frame_bytes=frame_size, max_total_bytes=2 * frame_size, max_records=2)
    if limit == "frame":
        config["max_frame_bytes"] = frame_size - 1
    elif limit == "count":
        config["max_records"] = 1
    else:
        config["max_total_bytes"] = 2 * frame_size - 1
    limited = journal(stream, origin=first.expected_head, **config)
    result = limited.read()
    assert not result.complete
    assert result.reason_code == _PREFIX + "limit_exceeded"
    assert len(result.entries) == (0 if limit == "frame" else 1)
    if limit != "frame":
        assert all(len(item.payload) <= config["max_frame_bytes"] for item in stream.inner.read())
    assert stream.inner.read()[1].payload == second.frame


def test_append_projected_limit_does_not_write():
    stream, value, first = prepared(max_records=1)
    accepted = value.append(first)
    second = value.prepare(record("operation.2"), expected_head=accepted.entry.position)
    calls = stream.calls.count("append")
    result = value.append(second)
    assert result.state == "not_submitted" and result.reason_code == _PREFIX + "limit_exceeded"
    assert stream.calls.count("append") == calls


@pytest.mark.parametrize("mode", ("sha", "disabled", "corrupt", "missing", "changed", "nonbool", "bad_digest"))
def test_integrity_dispositions(mode):
    policy = ProbePolicy()
    if mode == "disabled":
        policy = NoDigestPolicy()
    stream, value, submission = prepared(integrity_policy=policy)
    if mode == "bad_digest":
        policy.digest_hook = lambda _: b""
        invalid(lambda: value.prepare(record(), expected_head=submission.expected_head))
        return
    seed(stream, submission)
    if mode == "corrupt":
        stream.after["read"] = lambda rows: tuple(
            replace(row, payload=row.payload[:-1] + bytes([row.payload[-1] ^ 1])) for row in rows
        )
    elif mode == "missing":
        value = journal(stream, origin=submission.expected_head)
    elif mode == "changed":
        policy.algorithm = "changed"
    elif mode == "nonbool":
        policy.verify_hook = lambda *_: 1
    result = value.read()
    if mode in ("sha", "disabled"):
        assert result.complete
        assert result.entries[0].integrity_status == ("disabled" if mode == "disabled" else "verified")
    else:
        assert not result.complete
        assert result.reason_code == _PREFIX + ("integrity_unavailable" if mode in ("missing", "changed") else "integrity_failed")


@pytest.mark.parametrize("field,value", (
    ("write_id", "forged"), ("frame", b"bad"),
    ("record", record("different")),
    ("expected_head", StreamPosition("backend.journal", "governance", "other")),
))
def test_forged_submission_before_storage(field, value):
    stream, instance, submission = prepared()
    calls = tuple(stream.calls)
    invalid(lambda: instance.append(replace(submission, **{field: value})))
    assert tuple(stream.calls) == calls


def test_memory_write_requires_flush_and_exact_membership():
    stream, value, submission = prepared()
    result = value.append(submission)
    assert result.state == "confirmed"
    assert result.append_receipt.write_id == submission.write_id
    assert result.entry.frame == submission.frame
    assert result.durability_receipt.through == result.entry.position
    assert result.durability_receipt.achieved_durability is DurabilityLevel.IN_MEMORY
    assert stream.calls.count("append") == 1
    assert stream.calls.count("flush") == 1
    assert stream.calls.index("append") < stream.calls.index("flush")
    assert isinstance(result, GovernanceWriteResult)


def test_replay_and_conflict_compare_full_original_bytes():
    stream, value, first = prepared()
    initial = value.append(first)
    second = value.prepare(record("operation.2"), expected_head=initial.entry.position)
    assert value.append(second).state == "confirmed"
    calls = stream.calls.count("append")
    replay = value.append(first)
    assert replay.state == "confirmed" and replay.append_receipt is None
    assert replay.entry.frame == first.frame
    conflict = value.prepare(replace(first.record, payload=b"changed"), expected_head=first.expected_head)
    result = value.append(conflict)
    assert result.reason_code == _PREFIX + "identity_conflict"
    assert result.state == "not_submitted"
    assert stream.calls.count("append") == calls


def test_stale_and_injected_race_no_rebase():
    stream, value, first = prepared()
    other = value.prepare(record("operation.2"), expected_head=first.expected_head)
    assert value.append(first).state == "confirmed"
    stale = value.append(other)
    assert stale.state == "not_submitted" and stale.reason_code == _PREFIX + "stale_head"
    fresh = value.prepare(record("operation.3"), expected_head=stream.head())
    original_head = fresh.expected_head
    rival = value.prepare(record("operation.4"), expected_head=fresh.expected_head)
    stream.before["append"] = lambda: seed(stream, rival)
    raced = value.append(fresh)
    assert raced.state == "append_uncertain"
    assert raced.reason_code == _PREFIX + "continuity_invalid"
    assert len(stream.inner.read()) == 2
    assert fresh.expected_head == original_head
    assert fresh.expected_head != stream.head()


@pytest.mark.parametrize("mode", ("before", "after", "malformed"))
def test_append_uncertainty_then_explicit_reconciliation(mode):
    stream, value, submission = prepared()
    error = RuntimeError("private append failure")
    if mode == "before":
        stream.before["append"] = lambda: throw(error)
    elif mode == "after":
        stream.after["append"] = lambda _: throw(error)
    else:
        stream.after["append"] = lambda _: object()
    result = value.append(submission)
    assert result.state == "append_uncertain"
    assert result.reason_code == _PREFIX + ("receipt_invalid" if mode == "malformed" else "storage_failed")
    outcome = value.reconcile(submission)
    assert outcome.state == ("not_found_in_prefix" if mode == "before" else "found_confirmed")
    assert stream.calls.count("append") == 1
    assert isinstance(outcome, GovernanceReconciliation)


@pytest.mark.parametrize("change", (
    {"backend_id": "other"}, {"acquired": 1}, {"accepted_at": None},
    {"position": StreamPosition("other", "governance", "x")},
    {"write_id": "wrong"},
))
def test_malformed_receipts_stay_uncertain(change):
    stream, value, submission = prepared()
    stream.after["append"] = lambda receipt: replace(receipt, **change)
    result = value.append(submission)
    assert result.state == "append_uncertain"
    assert result.reason_code == _PREFIX + "receipt_invalid"
    assert result.append_receipt is None
    assert "flush" not in stream.calls


def test_apparent_receipt_without_bytes_cannot_confirm():
    stream, value, submission = prepared()

    def hide(receipt):
        stream.after["read"] = lambda _: ()
        return receipt

    stream.after["append"] = hide
    result = value.append(submission)
    assert result.state == "append_uncertain"
    assert result.entry is None and result.durability_receipt is None
    assert "flush" not in stream.calls


@pytest.mark.parametrize("mode", ("exception", "shape", "position", "level", "passive"))
def test_flush_is_separate_from_append_and_capability(mode):
    stream, value, submission = prepared(
        required_durability=DurabilityLevel.POWER_LOSS if mode in ("level", "passive") else DurabilityLevel.IN_MEMORY
    )
    if mode == "exception":
        stream.before["flush"] = lambda: throw(RuntimeError("private flush"))
    elif mode == "shape":
        stream.after["flush"] = lambda _: object()
    elif mode == "position":
        stream.after["flush"] = lambda receipt: replace(receipt, through=submission.expected_head)
    elif mode == "passive":
        stream.durability = lambda: throw(AssertionError("passive declaration must not be consulted"))
    result = value.append(submission)
    assert result.state == "appended_unconfirmed"
    assert result.entry.frame == submission.frame
    assert result.reason_code == _PREFIX + (
        "storage_failed" if mode == "exception" else
        "durability_unconfirmed" if mode in ("level", "passive") else "receipt_invalid"
    )
    assert (result.durability_receipt is not None) == (mode in ("level", "passive"))


def test_original_submission_preserved_through_reconciliation():
    stream, value, submission = prepared()
    before = (submission.frame, submission.write_id, submission.expected_head)
    assert value.append(submission).state == "confirmed"
    assert value.reconcile(submission).state == "found_confirmed"
    assert (submission.frame, submission.write_id, submission.expected_head) == before
    assert stream.calls.count("append") == 1


@pytest.mark.parametrize("operation", ("head", "read", "next", "append", "flush"))
@pytest.mark.parametrize("error_type,reason", (
    (SETTPersistenceFormatError, "format_invalid"),
    (SETTPersistencePositionError, "identity_mismatch"),
    (SETTPersistenceContinuityError, "continuity_invalid"),
    (SETTPersistenceError, "storage_failed"),
    (RuntimeError, "storage_failed"),
))
def test_structured_storage_errors_keep_precedence(operation, error_type, reason):
    stream, value, submission = prepared()
    error = (
        RuntimeError("private error") if error_type is RuntimeError
        else error_type("private error", reason_code="original.structured")
    )
    if operation == "next":
        def failing_iterator():
            raise error
            yield  # Explicit lazy-iteration boundary injection.
        stream.after["read"] = lambda _: failing_iterator()
    else:
        stream.before[operation] = lambda: throw(error)
    result = value.append(submission) if operation in ("append", "flush") else value.read()
    assert result.reason_code == _PREFIX + reason
    if operation == "append":
        assert result.state == "append_uncertain"
    if operation == "flush":
        assert result.state == "appended_unconfirmed"


@pytest.mark.parametrize("operation", ("digest", "verify"))
def test_ordinary_integrity_errors_are_private(operation):
    policy = ProbePolicy()
    stream, value, submission = prepared(integrity_policy=policy)
    error = RuntimeError("unexposed detail")
    if operation == "digest":
        policy.digest_hook = lambda _: throw(error)
        invalid(lambda: value.prepare(record(), expected_head=submission.expected_head))
    else:
        seed(stream, submission)
        policy.verify_hook = lambda *_: throw(error)
        result = value.read()
        assert result.reason_code == _PREFIX + "integrity_failed"
        assert "unexposed" not in repr(result)


@pytest.mark.parametrize("signal_type", (KeyboardInterrupt, SystemExit, GeneratorExit))
@pytest.mark.parametrize("boundary", ("head", "read", "next", "append", "flush", "digest", "verify"))
def test_termination_signals_propagate_same_object(signal_type, boundary):
    policy = ProbePolicy()
    stream, value, submission = prepared(integrity_policy=policy)
    signal = signal_type("termination")
    if boundary == "digest":
        policy.digest_hook = lambda _: throw(signal)
        def action():
            return value.prepare(record(), expected_head=submission.expected_head)
    elif boundary == "verify":
        seed(stream, submission)
        policy.verify_hook = lambda *_: throw(signal)
        action = value.read
    elif boundary == "next":
        def iterator():
            raise signal
            yield  # Explicit lazy-iteration boundary injection.
        stream.after["read"] = lambda _: iterator()
        action = value.read
    else:
        stream.before[boundary] = lambda: throw(signal)
        def action():
            return value.append(submission)
    with pytest.raises(signal_type) as caught:
        action()
    assert caught.value is signal


def test_fixed_diagnostics_inside_caller_exception_and_hostile_repr():
    class Hostile:
        def __repr__(self):
            raise AssertionError("repr must not be evaluated")

    try:
        raise RuntimeError("caller-private")
    except RuntimeError:
        error = invalid(lambda: record(payload=Hostile()))
    assert "caller-private" not in str(error)
    _stream, value, submission = prepared()
    invalid(lambda: value.append(replace(submission, frame=b"\xff")))
    assert "opaque" not in repr(submission)
    assert "opaque" not in repr(submission.record)


def test_sqlite_close_reopen_exact_bytes_and_confirmation(tmp_path):
    database = tmp_path / "journal.sqlite"
    options = dict(namespace_id="namespace.test", backend_id="backend.sqlite", journal_mode="WAL", synchronous="FULL")
    with SQLitePersistenceBackend.open(database, **options) as backend:
        origin = backend.evidence.head()
        value = journal(backend.evidence, origin=origin, required_durability=DurabilityLevel.POWER_LOSS)
        submission = value.prepare(record(), expected_head=origin)
        assert value.append(submission).state == "confirmed"
    with SQLitePersistenceBackend.open(database, **options) as reopened:
        value = journal(reopened.evidence, origin=origin, required_durability=DurabilityLevel.POWER_LOSS)
        read = value.read()
        assert read.complete and read.entries[0].frame == submission.frame
        assert value.reconcile(submission).state == "found_confirmed"
    # Reopening measures persisted bytes, not a simulated physical power failure.


@pytest.mark.parametrize("tail", (b"corrupt", b"SETTGOV\0\x00\x02"))
def test_corrupt_tail_retains_only_admitted_prefix(tail):
    stream, value, first = prepared()
    accepted = value.append(first)
    stream.inner.append(write_id="bad.tail", payload=tail, expected_head=accepted.entry.position)
    before = tuple(stream.inner.read())
    result = value.read()
    assert not result.complete and len(result.entries) == 1
    assert result.entries[0].frame == first.frame
    assert result.failure_index == 1
    assert tuple(stream.inner.read()) == before


def test_reconciliation_absence_conflict_and_partial_never_append():
    stream, value, first = prepared()
    assert value.reconcile(first).state == "not_found_in_prefix"
    different = value.prepare(replace(first.record, payload=b"different"), expected_head=first.expected_head)
    receipt = seed(stream, different)
    assert value.reconcile(first).state == "conflicting"
    stream.inner.append(write_id="bad.tail", payload=b"bad", expected_head=receipt.position)
    assert value.reconcile(first).state == "inconclusive"
    assert "append" not in stream.calls and "flush" not in stream.calls


def test_policy_rotation_retains_historical_reconciliation():
    stream, value, submission = prepared()
    assert value.append(submission).state == "confirmed"
    rotated = journal(
        stream, origin=submission.expected_head,
        integrity_policy=Sha256IntegrityPolicy(policy_id="sha.new"),
        historical_integrity_policies=(Sha256IntegrityPolicy(),),
    )
    invalid(lambda: rotated.append(submission))
    assert rotated.reconcile(submission).state == "found_confirmed"


def test_results_are_not_authority_or_real_producers():
    stream, value, submission = prepared()
    result = value.append(submission)
    scan = value.read()
    assert isinstance(scan, GovernanceReadResult)
    assert isinstance(scan.entries[0], GovernanceEntry)
    for item in (result, scan.entries[0], value.reconcile(submission)):
        assert not hasattr(item, "receipt_id")
        assert not hasattr(item, "verdict")
        assert not hasattr(item, "permits_effect")
    assert set(stream.calls) <= {"head", "read", "append", "flush"}


def test_public_limits_are_present_without_claiming_semantic_approval():
    root = Path(__file__).parents[1]
    dedicated = (root / "docs/governance_journal.md").read_text(encoding="utf-8")
    for phrase in (
        "not interpreted", "Empty", "writer", "transaction",
        "real durability producer", "governed recovery", "not part of this component",
    ):
        assert phrase in dedicated
    assert "opaque" not in repr(record())
    # Phrase checks are regression guards; owner reading accepts public meaning.
