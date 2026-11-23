# Policy Ruler reason codes

Every code the policy layer can produce, the class of error that
carries it, and the authorization code it becomes on the bound path.

Two families appear here. Codes in the `policy.` namespace are raised by
the policy boundary as `SETTPolicyConfigurationError`, which inherits
`SETTConfigurationError` and is deliberately **not** an authorization
error: it carries no authority verdict and no receipt identity. Codes in
the `authorization.` namespace are the ones authorization raises once a
policy failure crosses into the bound path, plus the selection and
registration diagnostics that the policy layer emits on authorization's
behalf, with the same classes they have always had.

Every message listed here is fixed and input-free. These diagnostics never
echo a submitted value, a canonical snapshot, a raw callback exception or
received evidence text, and they keep no internal cause or context. An
authorization error raised while the caller is handling another exception
keeps that exception as its `__context__`, as usual in Python.

## Policy boundary

Failures inside the policy layer itself, all with one fixed
message.

All of these raise `SETTPolicyConfigurationError` with the message
`Policy evaluation could not establish a valid binding.`

| Code | Raised when |
|---|---|
| `policy.ruler_configuration_invalid` | A PolicyRuler constructor field, or a producer registration, is malformed |
| `policy.evaluation_input_invalid` | The request or approval container is not the exact expected type, or the request cannot be canonically encoded |
| `policy.evaluation_checkpoint_invalid` | The checkpoint identifier is malformed or not supported by the configured integration |
| `policy.evaluation_capacity_exceeded` | Every configured live-record slot belongs to a pending or receipt-bound invocation |
| `policy.clock_invalid` | The configured clock failed, returned a naive value, or a computed deadline is unrepresentable |
| `policy.evaluation_state_changed` | The ruler generation changed between the snapshot and the completed evaluation |
| `policy.evaluation_incomplete` | A required verified-state producer is missing, failed, or returned a record that does not match its registration and scope |
| `policy.transition_reentrant` | A forbidden mutation was attempted from inside the guarded transition on the same thread |
| `policy.decision_record_unconfirmed` | The durability producer could not write the decision record or have it confirmed at the governance journal's required durability |

## Translation at the bound authorization path

How each policy code is reported when it reaches authorization.

`authorization.clock_invalid` is also raised directly by the bound path,
without a policy-boundary origin, when the observation taken after a
backend operation is unusable or earlier than the observation taken
immediately before it. Any write or consumption the backend already
performed remains an observed fact: it is not undone, reinterpreted or
retried.

| Policy code | Authorization code |
|---|---|
| `policy.evaluation_input_invalid` | `authorization.policy_evaluation_invalid` |
| `policy.evaluation_checkpoint_invalid` | `authorization.policy_checkpoint_mismatch` |
| `policy.evaluation_capacity_exceeded` | `authorization.policy_evaluation_capacity_exceeded` |
| `policy.clock_invalid` | `authorization.clock_invalid` |
| `policy.evaluation_state_changed` | `authorization.policy_generation_changed` |
| `policy.evaluation_incomplete` | `authorization.policy_evaluation_incomplete` |
| `policy.transition_reentrant` | `authorization.policy_handoff_reentrant` |
| `policy.decision_record_unconfirmed` | `authorization.decision_record_unconfirmed` |

An unexpected failure in the policy handoff that has no code of its own is
reported as `authorization.policy_evaluation_failed`.

## Binding diagnostics at authorization

The private binding no longer matches; the attempt is blocked,
which is not a policy denial.

These raise `SETTAuthorizationError` with the message `Authorization
could not validate the policy binding.` They block; none of them is a
policy denial.

| Code | Raised when |
|---|---|
| `authorization.policy_evaluation_invalid` | The result is not the exact `PolicyEvaluation` type, or the bound input was rejected at the policy boundary |
| `authorization.policy_evaluation_unrecognized` | The object is not the one this incarnation registered: a copy, a manufactured instance, an inspection result or a foreign ruler's result |
| `authorization.policy_invocation_mismatch` | The registered result belongs to another receiver or another invocation |
| `authorization.policy_evaluation_used` | The record was already claimed or bound to a receipt |
| `authorization.policy_selection_changed` | The current full canonical selection differs, including changed content under an unchanged revision label |
| `authorization.policy_generation_changed` | The policy generation or the receiver's verifier generation differs, including when it changed after consumption and before the handler. Stale-binding rejection, not denial |
| `authorization.policy_checkpoint_mismatch` | The checkpoint differs from the invocation witness or the single configured entry checkpoint; checked after selection and generations |
| `authorization.policy_evaluation_incomplete` | A closed verified-state slot is missing or its producer reported incomplete state |
| `authorization.policy_evidence_changed` | Well-formed current state differs in canonical value, epoch or scope, or confirmation of retained state is negative, unavailable or fails before issue or consume |
| `authorization.policy_evaluation_expired` | The binding deadline was reached, including equality, expiry observed after the backend call, and expiry reached before the handler |
| `authorization.policy_handoff_reentrant` | A forbidden reentry poisoned the transition, including when a backend caught the rejection |
| `authorization.policy_binding_unavailable` | No usable private binding exists for the presented receipt object and receiver, including an equal but distinct copy, a restart, an orphan write or a terminated single use |
| `authorization.policy_evaluation_capacity_exceeded` | The bound path hit the configured live-record limit. Capacity exhaustion, not denial |
| `authorization.policy_evaluation_failed` | An unexpected failure in the policy handoff, reported without an internal cause or context |

An equal but distinct receipt produces `authorization.policy_binding_unavailable`
even when lookup finds the original object's live record. The code concerns the
association required for the presented object, not whether any record with those
public fields exists. This is not a policy denial or person-authentication result.
A found record with changed receipt fields still produces
`authorization.receipt_mismatch`.

`authorization.receipt_expired` also rejects a receipt whose expiry is reached
during issue or consumption, even if its policy binding has a later deadline.
This is a `SETTAuthorizationError`, not a configuration failure or a policy
denial. If both bounds have been reached, the binding deadline is reported
first. Completed backend writes or consumptions remain
observed facts, without rollback or automatic retry.

## Configuration diagnostics at authorization

The authorization ruler was built without a usable policy ruler.

These raise `SETTAuthorizationConfigurationError` at construction, before
any instance is attached or either object is changed.

| Code | Raised when |
|---|---|
| `authorization.policy_ruler_missing` | No `policy_ruler` was injected. There is no default ruler and no legacy engine |
| `authorization.policy_ruler_invalid` | The injected value is not the exact `PolicyRuler` class |
| `authorization.policy_ruler_configuration_mismatch` | The supplied clock or evidence sink is not the same configured object the policy ruler holds |
| `authorization.policy_selection_resolver_conflict` | The facade resolver argument is a different object from the injected ruler's resolver |

## Selection and registration diagnostics

A policy could not be registered, or the declared selection
could not be used.

Registration failures are `SETTAuthorizationConfigurationError`; runtime
selection failures are `SETTAuthorizationError`.

| Code | Stage |
|---|---|
| `authorization.policy_selection_resolver_missing` | PolicyRuler construction without a resolver |
| `authorization.policy_selection_resolver_invalid` | A non-conforming resolver or resolver identifier |
| `authorization.policy_binding_required` | The old two-argument registration call |
| `authorization.policy_binding_invalid` | A registration binding that is not an exact `PolicyBinding` |
| `authorization.policy_invalid` | A policy that does not implement the callback protocol |
| `authorization.policy_duplicate` | A repeated `(action_type, policy_id)` key |
| `authorization.policy_binding_mismatch` | Registration: the policy identifier does not match its binding. Runtime: the selected and registered canonical bindings differ |
| `authorization.policy_selection_failed` | The resolver callback raised |
| `authorization.policy_selection_result_invalid` | The resolver returned something other than an exact `PolicySelection` |
| `authorization.policy_selection_scope_mismatch` | The selection names a different action identity |
| `authorization.policy_selection_not_effective` | The selection is not yet effective |
| `authorization.policy_applicability_undetermined` | A required entry declares `UNDETERMINED` |
| `authorization.policy_selection_facts_unverified` | A required entry depends on a missing or non-authoritative fact |
| `authorization.policy_required_empty` | No required entry applies |
| `authorization.policy_missing` | A required applicable policy has no registration for this action type |
| `authorization.policy_version_mismatch` | The registered version differs from the selected one |

Informative entries carry the same codes as `(policy_id, reason_code)`
pairs on the evaluation. They are diagnostics, never authority-blocking
exceptions, and they never downgrade a required control.

See [PolicyRuler](policy_ruler.md), [the handoff contract](policy_handoff.md)
and [authorization reason codes](authorization_reason_codes.md).

## Condition declaration diagnostic

A `PolicyCondition` was built with invalid fields.

| Code | Boundary and meaning | Result |
|---|---|---|
| `policy.condition_invalid` | Structurally invalid supplied condition declaration | `SETTPolicyConfigurationError`; fixed message `The policy condition declaration is invalid.` |

- This code reports invalid structure. It does not report a failed verifier,
  a denial by an institutional policy or institutional compliance, and it
  replaces no authorization code.
- The checks run in the order described in
  [the condition contract](policy_conditions.md).
- The diagnostic keeps no raw validation exception and no submitted value. When
  the declaration is built while the caller is handling another exception, that
  exception becomes its `__context__`, as usual in Python.
- Ordinary call-argument errors, direct `Enum` errors and failures of nested
  values before the call keep their own behavior.

A declaration verifies nothing by itself. When condition verifiers are
configured, the policy layer is set up to verify the required declarations
during authorization. A governed attempt reaches that point only when every
state producer is configured; see
[Current limitation](authorization_reason_codes.md#current-limitation).

## Condition verification result diagnostic

A `ConditionVerification` was built with invalid fields.

| Code | Boundary and meaning | Result |
|---|---|---|
| `policy.condition_verification_invalid` | Structurally invalid supplied condition verification result | `SETTPolicyConfigurationError`; fixed message `The condition verification result is invalid.` |

- This code is raised only when a `ConditionVerification` value is built with
  invalid fields. It does not report a denial by an institutional policy or
  institutional compliance, and it replaces no other code, including
  `policy.condition_invalid`.
- Failures of a verifier call have their own codes; see
  [Condition diagnostics](#condition-diagnostics). If a verifier raises this
  error while building its result, the call is reported as
  `policy.condition_verifier_failed`.
- The checks run in the order described in
  [condition verification](condition_verification.md).
- The diagnostic keeps no raw validation exception and no submitted value, and
  an exception the caller was handling is not chained to it either. Its
  `verdict` is `None` and it carries no receipt identity.
- Ordinary call-argument errors and direct `Enum` errors keep their own
  behavior.

A structurally valid result is not current evidence and grants no authority.

## Condition inventory diagnostic

The conditions declared on a `PolicyDecision`, or the inventory
built from all the decisions of one evaluation, are invalid.

| Code | Boundary and meaning | Result |
|---|---|---|
| `policy.condition_inventory_invalid` | Structurally invalid supplied condition declaration on a `PolicyDecision` | `SETTPolicyConfigurationError`; fixed message `The policy condition inventory is invalid.` |

- On a `PolicyDecision`, this code is raised when the decision is built,
  directly or through a factory, with an invalid `conditions` declaration,
  including an explicit `None`. An omitted declaration raises Python's ordinary
  `TypeError` instead.
- Raised inside a policy callback, it is handled like any other callback
  failure, as `authorization.policy_failed`. Raised by the policy layer for the
  inventory of a whole evaluation, it is reported as
  `authorization.policy_result_invalid`.
- It does not report a failed verifier, a denial by an institutional policy or
  institutional compliance, and it replaces no other code, including
  `policy.condition_invalid`.

Invalid earlier fields of the decision raise `SETTValidationError`, a
`ValueError`, first. A handler that catches `ValueError` or
`SETTValidationError` around construction does not catch this diagnostic;
their common base is `SETTError`. Its `verdict` is `None` and it carries no
receipt identity. It keeps no raw validation exception and no submitted value,
and an exception the caller was handling is not chained to it either.
`KeyboardInterrupt`, `SystemExit` and `GeneratorExit` propagate. A
`PolicyCondition` that fails its own construction before the decision is
built keeps `policy.condition_invalid`. See the
[API reference](api_reference.md#policydecision-condition-declaration).

## Condition verification: scope and limitations

The scope and limits of condition verification are described once, in
[Condition verification](condition_verification.md#condition-verification-scope-and-limitations).

### Condition diagnostics

Each way a condition check can fail has its own policy code, and
authorization reports it under a matching code of its own.

| Policy code | Authorization translation | Authorization class |
|---|---|---|
| `policy.condition_verifier_missing` | `authorization.condition_verifier_missing` | configuration |
| `policy.condition_verifier_failed` | `authorization.condition_verifier_failed` | configuration |
| `policy.condition_result_invalid` | `authorization.condition_result_invalid` | configuration |
| `policy.condition_unsatisfied` | `authorization.condition_unsatisfied` | blocking authorization |
| `policy.condition_evidence_not_present` | `authorization.condition_evidence_not_present` | blocking authorization |
| `policy.condition_evidence_invalid` | `authorization.condition_evidence_invalid` | blocking authorization |
| `policy.condition_evidence_unavailable` | `authorization.condition_evidence_unavailable` | blocking authorization |

In the last column, "configuration" means the error is a
`SETTAuthorizationConfigurationError`, and "blocking authorization" means a
plain `SETTAuthorizationError`.

- At the policy boundary, all seven codes are raised as
  `SETTPolicyConfigurationError` with the same fixed message.
- A result of the wrong type, with a declaration different from the expected
  one, or outside its time window is `condition_result_invalid`.
- A missing verifier is detected before any verification call.
- Missing, invalid and unavailable evidence stay distinct from an unsatisfied
  condition.
- A missing structural part is never reported as `evidence_not_present`.

Related codes:

- `policy.condition_inventory_invalid`, raised for the inventory of a whole
  evaluation, is reported as `authorization.policy_result_invalid`.
- An invalid declaration raised inside a policy callback is handled like any
  other callback failure: `authorization.policy_failed` for a required policy,
  and a diagnostic pair for an informative policy.
- Structural failures of the condition state keep the incomplete-state code,
  `policy.evaluation_incomplete`.
- A later confirmation failure is reported as
  `authorization.policy_evidence_changed`, and reaching the exclusive expiry as
  `authorization.policy_evaluation_expired`.
- No existing reason code changes meaning.

Exceptions raised by callbacks, and the exception context of these errors, are
described in
[Condition verification](condition_verification.md#explicit-construction-and-independent-proof).

## Review restriction diagnostics

Each way the review producer can block has its own policy code,
and authorization reports it under a matching code of its own.

| Policy code | Authorization translation | Authorization class |
|---|---|---|
| `policy.review_restriction_active` | `authorization.review_restriction_active` | blocking authorization |
| `policy.review_verifier_missing` | `authorization.review_verifier_missing` | configuration |
| `policy.review_verifier_failed` | `authorization.review_verifier_failed` | configuration |
| `policy.review_result_invalid` | `authorization.review_result_invalid` | configuration |
| `policy.review_state_unavailable` | `authorization.review_state_unavailable` | configuration |

In the last column, "configuration" means the error is a
`SETTAuthorizationConfigurationError`, and "blocking authorization" means a
plain `SETTAuthorizationError`. At the policy boundary, all five codes are
raised as `SETTPolicyConfigurationError` with the same fixed message.

- `review_restriction_active`: a restriction an earlier decision declared on
  this action type has no favorable completed review.
- `review_verifier_missing`: a recorded restriction names a verifier that is
  not configured. It is checked before any verifier is called.
- `review_verifier_failed`: the verifier raised.
- `review_result_invalid`: the result is not a `ReviewVerification` for the
  same obligation, binding and action.
- `review_state_unavailable`: the governance journal could not be read
  completely, or the binding of a recorded obligation is no longer
  registered.

`policy.review_record_unconfirmed` is raised only by
`PolicyRuler.follow_up_reviews()`, when a review record cannot be written and
confirmed at the journal's required durability. It is a
`SETTPolicyConfigurationError` and has no authorization translation, because
the follow-up is not on the authorization path. During the follow-up, the
other review codes describe one obligation and do not stop the others, except
`policy.review_state_unavailable` for an unreadable journal.

The producer is described in
[Policy reviews](policy_reviews.md#the-review-producer).

## Governance journal diagnostics

What the standalone journal reports when it cannot read, write or
confirm a record.

These diagnostics belong to the standalone byte journal. They are not policy
denials, authorization receipts or a durability producer. Payloads are opaque
and not interpreted; empty bytes do not mean a decision, a review or the absence
of obligations. Configured integrity checks do not authenticate a writer. No
transaction joins storage, journal, trace, consumption and effect.

| Code | Fixed meaning |
|---|---|
| policy.journal_invalid | Invalid local configuration, record or submission. |
| policy.journal_format_invalid | Malformed, truncated or trailing frame data. |
| policy.journal_version_unsupported | Unsupported envelope version, not an opaque domain schema. |
| policy.journal_identity_mismatch | Journal, stream, origin or recomputed write identity differs. |
| policy.journal_continuity_invalid | Predecessor, duplicate identity/position or terminal-head inconsistency. |
| policy.journal_limit_exceeded | Frame, count or aggregate limit exceeded. |
| policy.journal_integrity_unavailable | Historical policy missing or its identity/algorithm changed. |
| policy.journal_integrity_failed | Integrity invocation/return or digest verification failed. |
| policy.journal_head_changed | Head changed during the bounded scan. |
| policy.journal_stale_head | Original expected head no longer matches before a new append. |
| policy.journal_identity_conflict | Same operation has different full frame bytes. |
| policy.journal_storage_failed | Ordinary storage invocation or iteration failed. |
| policy.journal_receipt_invalid | Append/flush response violates the receipt contract. |
| policy.journal_durability_unconfirmed | Valid flush receipt has insufficient/unknown achieved durability. |

- Scans stop at the first failure, checked in this order: type and bounds,
  format and version, identity, continuity, integrity, then the terminal head
  and its stability. Bounds stop before any further interpretation.
- For a known operation, an identity conflict is reported before a stale head.
- After an append call, uncertainty takes precedence over any claim that
  nothing was submitted.
- The persistence exceptions for format, position and continuity map to the
  corresponding journal diagnostics; other storage failures use
  `policy.journal_storage_failed`.
- Translated journal diagnostics keep no raw exception object, cause, context
  or text derived from the payload. Termination signals propagate.

The [journal contract](governance_journal.md) says which results can keep a
valid receipt, and why none implies rollback, authority or delivery of an
effect.

## Policy review declaration diagnostic

A review obligation or a review fact was built with invalid
fields.

| Policy code | Meaning |
|---|---|
| `policy.review_obligation_invalid` | Invalid local review obligation or review fact value. It is a policy configuration failure, not an authorization verdict. |

The fixed message is `The policy review obligation declaration is invalid.` No submitted identifier or raw exception detail is included. This diagnostic validates local value construction only; it does not report a verifier failure, missing producer, unverified fact or authorization denial. Those producer and integration outcomes are not covered here.