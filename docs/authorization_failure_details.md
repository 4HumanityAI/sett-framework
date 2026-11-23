# Authorization failure details

The errors and outcomes of `authorize()` and
`verify_and_consume()` tell a caller why an attempt failed and let it match
the attempt with its evidence events.

These fields are part of the public contract of
`AuthorizationRuler.authorize()` and `AuthorizationRuler.verify_and_consume()`.
The decisions they build on are listed at the end.

## Why these fields exist

Without them, a failed attempt did not say why a policy denied,
which condition failed, which events belong to it, or what failed when the
failure report could not be written.

When `authorize()` cannot finish, it raises `SETTAuthorizationError`, or its
subclass `SETTAuthorizationConfigurationError`. Before these fields, the error
told the caller two things:

- `reason_code`: why the attempt stopped, for example
  `authorization.condition_unsatisfied`;
- `policy_observations`: which policies had already returned a result in that
  same call, as `(policy_id, policy_version, verdict)` rows.

Two facts can happen in the same attempt: a policy denies the action, and a
required verification fails. The caller could learn that a policy denied, but
not:

1. why it denied;
2. which required condition failed, and which conditions were checked before
   it;
3. which evidence events belong to this attempt. Events shared only the action
   identifier, and the same action identifier can appear in several attempts;
4. what the original failure was when writing the failure report also failed.
   The error then said only `authorization.evidence_failed`.

The four additions below close those gaps without changing the meaning of any
existing field.

## Which result comes first

When a required verification fails, that failure is the result;
a denial in the same attempt is reported alongside it.

When a required verification fails, that failure is the result of the
attempt: `authorize()` raises with the failure's reason code. A policy denial
observed in the same attempt is reported alongside it, not instead of it.
Neither fact can produce a permission. A completed denial, where every
required verification succeeded and a policy denied, is still returned as an
outcome rather than raised.

Why the failure comes first rather than the denial:

- Both refuse the action, so neither order is safer than the other.
- A returned denial means that the evaluation completed. Returning one when a
  required verification failed would make a clean denial and a denial on
  unverified state look the same.
- Failures such as an invalid clock or a broken verifier need someone to fix
  them. They should not be hidden behind a policy answer.

A decision record keeps every completed evaluation, a denial included; see
[Decision records](decision_records.md). A blocked attempt writes no
record, so when a required verification fails, both facts stay only on the
error.

## The additions

Four additions, each `None` where it does not apply; nothing
that existed before changes.

All four are new attributes. Each one is `None` where it does not apply. No
existing attribute, reason code, error class, message, return type or order
of evaluation changes.

### 1. Reasons for observed policy decisions

The reason codes each observed policy returned, in a separate
attribute aligned with `policy_observations`.

```python
SETTAuthorizationError.policy_observation_reason_codes: tuple[tuple[str, ...], ...] | None
```

- It has the same length and order as `policy_observations`. Element *i*
  holds the reason codes that the policy in row *i* returned with its
  verdict. A policy that returned no reason codes has `()`.
- It is `None` exactly when `policy_observations` is `None`.
- It is captured at the same moment as the row, from the accepted policy
  result. The codes are the identifiers the policy supplied. They already
  pass the framework's identifier rule and already appear in the
  `authorization.policy_checked` evidence event.
- The existing three-element rows stay as they are, so code that unpacks or
  compares them keeps working. That is why this is a separate attribute
  rather than a wider row.

### 2. Which condition failed

The condition on which verification stopped, and the conditions
whose verifier was called before it.

```python
SETTAuthorizationError.failed_condition: tuple[str, str, str] | None
SETTAuthorizationError.attempted_conditions: tuple[tuple[str, str, str], ...] | None
```

Each condition is identified as `(policy_id, policy_version, condition_id)`,
the identity its `PolicyCondition` declaration already defines.

Both are set when condition verification during `authorize()` stops on a
specific condition. That covers these reason codes:
`authorization.condition_verifier_missing`,
`authorization.condition_verifier_failed`,
`authorization.condition_result_invalid`,
`authorization.condition_unsatisfied`,
`authorization.condition_evidence_not_present`,
`authorization.condition_evidence_invalid`,
`authorization.condition_evidence_unavailable`, and
`authorization.clock_invalid` when the clock fails while a condition is being
verified.

- `failed_condition` is the condition on which verification stopped.
- `attempted_conditions` lists the conditions whose verifier was called, in
  verification order. Every condition before the failing one returned
  *satisfied* in this attempt.
- It ends with `failed_condition` when the failing condition's verifier was
  called. It does not include it when verification stopped before that call,
  as when the clock fails first.
- When a required verifier is not configured: the framework checks that every
  verifier exists before calling any of them. `failed_condition` is then the
  first condition, in verification order, whose verifier is missing, and
  `attempted_conditions` is `()` because no verifier was called.
- When the report of such a failure cannot be written, the error carries
  `authorization.evidence_failed` with the condition code in
  `original_reason_code`, and both fields are still set.
- Both are `None` for every other failure. That includes a structurally
  invalid condition inventory, which has no single failing condition.

Only declared identifiers are reported, never evidence content, verifier
output or the request. A condition whose verifier was not called is never
listed as attempted.

**These fields are set only when condition verification runs.** It runs in a
governed attempt only when every verified-state producer is configured. See
[Current limitation](#current-limitation).

### 3. Authorization attempt identifier

A random identifier shared by an attempt's error or outcome and
by all its evidence events.

```python
SETTAuthorizationError.authorization_attempt_id: str | None
AuthorizationOutcome.authorization_attempt_id: str | None
```

The same value also appears under the key `"authorization_attempt_id"` in the
`attributes` of every evidence event the attempt writes.

An attempt is one call to `authorize()` or one call to `verify_and_consume()`.

- It is a new random UUID (version 4) string for each attempt. It is
  generated before the attempt's first evidence event. It is not derived from
  the request, its fingerprint, the receipt, the time or a counter.
- Every `SETTAuthorizationError` raised during the attempt carries it,
  including failures that happen before policy evaluation starts. When an
  evidence sink or a receipt store raises its own `SETTAuthorizationError`,
  the attempt sets its identifier on that error, replacing any value already
  there.
- Every `AuthorizationOutcome` returned by `authorize()` carries it: a
  completed denial, an approval requirement and a grant alike. An outcome
  that application code constructs directly has `None`: the identifier is not
  a constructor parameter. It takes no part in equality, hashing or `repr()`,
  so two outcomes with the same decision still compare equal.
- Events written by the policy evaluation during the call, such as
  `authorization.policy_checked`, carry the same value. Events written outside
  an attempt, such as those of `PolicyRuler.evaluate()`, carry none.
- Attempts running at the same time in different threads keep their own
  identifiers.
- A call to `verify_and_consume()` gets its own identifier, not the one of
  the attempt that issued the receipt. When `SETTExecutor` consumes a
  receipt, its trace event `authorization.receipt_verified` carries
  `decision_ref`, the issuing attempt's identifier; a direct call to
  `verify_and_consume()` is not linked.
- The executor's own `authorization.denied` error, raised when an outcome
  carries no receipt, is created outside the attempt and has `None`. The
  outcome it comes from carries the identifier.
- It identifies an attempt so that its error or outcome and its events can be
  matched. It is not authority, not a secret, not a receipt identifier and
  not proof of anything. Presenting it grants nothing.
- The name keeps it distinct from the executor's `attempt_id`, which
  identifies one execution attempt of a handler.
- Decision records use it in their operation ID, `decision.` followed by
  the identifier, so a record, its error or outcome and its events can be
  matched without a second identifier.

It goes in `attributes` rather than in a new event field so that custom
evidence sinks that already store `kind`, `reason_codes` and `attributes`
keep it without any change.

### 4. Original failure code when the failure report cannot be written

When the failure report cannot be written, the error still says
what the original failure was.

```python
SETTAuthorizationError.original_reason_code: str | None
```

Before raising a failure, the framework writes an evidence event that reports
it: `authorization.failed`, or `authorization.receipt_rejected` when a receipt
is rejected. If that write fails, the caller receives
`SETTAuthorizationConfigurationError` with `authorization.evidence_failed`.
The original reason code is not in the evidence, because the write that would
have recorded it is the one that failed.

- `authorization.evidence_failed` stays the primary reason code. The caller
  must still learn that evidence is not being recorded.
- `original_reason_code` holds the reason code of the failure that was being
  reported.
- It is `None` when `authorization.evidence_failed` comes from any other
  write, such as the request event or a policy-check event, and on every
  other error.

The reasons for this choice are in
[Decision: keep the original failure code](decision_keep_original_failure_code.md).

## Rules shared by all four additions

Additive, diagnostic only, and never read back by the framework.

- **Additive only.** Existing attributes, reason codes, error classes,
  messages, return types, callback order and precedence are unchanged.
- **Diagnostic data, not authority.** The framework never reads these values
  back. Changing them on a caught exception has no effect.
- **Detached values.** Strings and tuples of strings.
- **Not added to text output.** They are not included automatically in
  exception messages, `str()`, `repr()` or trace export.
- **Not a durable audit record.** They exist only on the error or outcome
  object the caller received.
- **Careful disclosure.** As with `policy_observations`, policy and condition
  identifiers, versions and reason codes are operational data. They are not
  automatically suitable for showing to end users.
- **No extra evaluation.** No policy or verifier runs just to fill these
  fields.

## Current limitation

Unless every state producer is configured, no governed attempt
reaches condition verification, and a denial shows only in the observation
fields.

The README, the
[authorization reason codes](authorization_reason_codes.md#current-limitation)
and the API reference state it: unless the condition, consent, durability and
review producers are all configured, every `authorize()` call that reaches
state verification fails with `authorization.policy_evaluation_incomplete`,
even when every policy would grant; no receipt is issued; and a denial in such
an attempt is visible only in `SETTAuthorizationError.policy_observations`,
with its reason codes in `policy_observation_reason_codes`.

## Example

How a caller can log the new fields.

```python
try:
    outcome = ruler.authorize(request)
except SETTAuthorizationError as error:
    logger.warning(
        "authorization failed",
        extra={
            "reason_code": error.reason_code,
            "original_reason_code": error.original_reason_code,
            "authorization_attempt_id": error.authorization_attempt_id,
            "failed_condition": error.failed_condition,
            "observed": list(
                zip(
                    error.policy_observations or (),
                    error.policy_observation_reason_codes or (),
                )
            ),
        },
    )
    raise
```

## How it is verified

Every earlier test passes unchanged; new tests cover five
situations and the attempt identifier.

- Every test that existed before these fields passes unchanged.
- `tests/test_authorization_failure_details.py` covers five situations,
  checking every new field, including `None` where a field does not apply:
  1. a denial with all required state verified;
  2. a denial together with a failed required condition;
  3. a failure before any policy returned a result;
  4. a denial followed by a missing state verifier, an invalid clock or a
     malformed result;
  5. a failure of the failure report itself.
- Further tests check that the attempt identifier is the same on the error
  or outcome and on every event of the attempt, differs between attempts, and
  is not derived from the request fingerprint. They also check that every
  outcome returned by `authorize()` carries it, and that each
  `verify_and_consume()` call has its own, different from the identifier of
  the attempt that issued the receipt.

## Not covered

A durable record of a blocked attempt, the link for a direct
receipt use, and explanations for end users.

- A durable record of a blocked attempt. Completed evaluations are kept in
  [decision records](decision_records.md) and read back after a restart by
  [governance recovery](governance_recovery.md); a blocked attempt keeps
  its facts only on its error.
- Linking a direct call to `verify_and_consume()` to the attempt that
  issued the receipt. Through `SETTExecutor`, the link is the trace's
  `decision_ref`.
- Explanations written for end users. [Explanations](explanations.md)
  turns an error into views with stable codes; the words are the
  application's.

## Decisions behind these fields

The decisions behind this contract, with links to their reasons.

The project maintainer took these decisions. The options considered and the
reasons for each are in
[Design decisions](authorization_failure_details_decisions.md).

- **Which result comes first** stays as it was: the failure comes first and
  the denial is reported separately. Decision records keep completed
  evaluations; a blocked attempt keeps both facts on its error. Making the
  denial the primary result was considered and not adopted.
- **The four additions** above are part of the contract. An explicit flag
  saying whether the observation rows are complete was considered and not
  included.
- **Every attempt is identified**: errors, returned outcomes and evidence
  events carry the attempt identifier, and each receipt use is an attempt of
  its own.
- **The original failure code** is kept when the failure report cannot be
  written. See
  [Decision: keep the original failure code](decision_keep_original_failure_code.md).
- **The current limitation** is stated plainly in the user documentation.
