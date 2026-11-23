# Design decisions: authorization failure details

The questions behind the authorization failure details
contract, the options considered, what was decided and why.

**Status:** decided by the project maintainer and implemented. The resulting
contract is [Authorization failure details](authorization_failure_details.md).

Each section states the question, the options considered, the decision and
the reasons for it.

## 1. Which result comes first

The verification failure stays the result; the denial is
reported separately.

**Question.** In the same attempt, a policy denies the action and a required
verification fails. Which of the two is the result of the attempt?

**Options considered.**

1. The verification failure is the result, and the denial is reported
   separately. This is how the framework already behaved.
2. The denial is the result, and the verification failure is reported
   separately.
3. Both facts are first defined in a shared decision record, and only then is
   one of them chosen as the result.

**Decision.** Option 1, kept as it was implemented. Option 3 is recorded as
the direction for a future design of durable decision records: it
complements option 1 rather than replacing it, because option 1 says what a
call returns and option 3 says what a record keeps. Option 2 is not adopted.

**Later.** Decision records now keep every completed evaluation; a blocked
attempt writes none and keeps both facts on its error. See
[Decision records](decision_records.md).

**Reasons.**

- Both options refuse the action, so neither is safer than the other.
- A returned denial means that the evaluation completed. Returning one when a
  required verification failed would make a clean denial and a denial on
  unverified state look the same.
- Failures such as an invalid clock or a broken verifier need someone to fix
  them. They should not be hidden behind a policy answer.
- Option 2 would change behavior that callers and tests already rely on, and
  nothing requires that change: both facts can already be obtained from the
  same call.

## 2. What a failed attempt should tell the caller

Four additions to the error; the completeness flag is left out.

**Question.** What could a caller not learn from a failed attempt, and which
additions should close those gaps?

**Options considered.** Five additions to the error:

1. the reasons given by each policy that had already decided;
2. which required condition failed, and which conditions were checked before
   it;
3. an identifier for the attempt;
4. the original failure code when the failure report cannot be written;
5. an explicit flag saying whether the list of policy decisions is complete.

**Decision.** The first four. The completeness flag is left out.

**Reasons.**

- *Reasons for policy decisions.* Before, a denial in a failed attempt showed
  only as a row that said which policy denied, not why. The reason codes
  already appear in evidence events and in completed denials, so this exposes
  no new kind of data. They go in a separate attribute so that code using the
  existing rows keeps working.
- *Which condition failed.* It is the first thing a caller needs to
  understand or fix the failure. Only declared identifiers are reported. A
  condition counts as attempted only if its verifier was actually called, so
  the error never suggests that something ran when it did not.
- *Attempt identifier* and *original failure code*: see sections 3 and 4.
- *Completeness flag.* Whether the list is complete can already be told from
  the reason code of the error.

**Consequence.** While the other required state verifiers do not exist,
condition verification is never reached in a governed attempt, so the fields
about failed conditions stay empty in practice. The contract says so.

## 3. How attempts are identified

A random identifier per attempt, on its errors, outcomes and
events; each receipt use is an attempt of its own.

**Question.** Evidence events used to share only the action identifier, and
the same action identifier can appear in several attempts. How can an error
or a result be matched with the events of its own attempt?

**Decision.** Each attempt gets a random identifier. It appears on every
error the attempt raises, on every outcome `authorize()` returns, and on
every evidence event the attempt writes. One call to `authorize()` is an
attempt, and so is one call to `verify_and_consume()`.

**Choices within the decision, and their reasons.**

- *A random UUID, version 4.* It is not derived from the request, the time or
  a counter, so it reveals nothing about the request or the order of
  attempts. The executor already uses this format for its own attempts.
- *The name `authorization_attempt_id`.* The executor already exposes
  `attempt_id` for an execution attempt of a handler. A different name keeps
  the two from being confused.
- *Inside the event's attributes.* Custom evidence sinks that already store
  an event's attributes keep the identifier without any change.
- *Also on returned outcomes.* Once the required state verifiers exist, a
  completed denial will be the normal way an attempt is refused. Without the
  identifier on the outcome, it could not be matched with its events.
- *Receipt use as an attempt of its own.* Every call that writes evidence
  becomes identifiable. Linking a receipt use to the attempt that issued the
  receipt is left to the future design of durable decision records.
- *Not authority.* The identifier is for matching only. Presenting it grants
  nothing. It is designed so that a future durable record can reuse it
  instead of introducing a second identifier.

**Later.** Decision records use the identifier in their operation ID. When
`SETTExecutor` consumes a receipt, the trace event
`authorization.receipt_verified` carries `decision_ref`, the issuing
attempt's identifier.

## 4. Original failure code when the failure report cannot be written

`authorization.evidence_failed` stays first, and the original
code is kept in a separate attribute.

**Decision.** When writing a failure report to the evidence sink fails,
`authorization.evidence_failed` stays the primary reason code and the error
also keeps the original failure code, in a separate attribute.

The context, options and reasons are in their own document:
[Decision: keep the original failure code](decision_keep_original_failure_code.md).

## 5. Stating the current limitation plainly

The documentation says plainly when a governed attempt stops,
and why.

**Question.** When this was decided, the framework did not yet include
verifiers for consent, review and durability. What did users need to be told
while that was so?

**Decision.** The user documentation says plainly that every `authorize()`
call reaching state verification fails with
`authorization.policy_evaluation_incomplete`, even when every policy would
grant. It also says that no receipt is issued, that handlers requiring a
receipt cannot run yet, and that a denial in such an attempt is visible only
in `policy_observations`.

**Reasons.**

- The project is open source and aims to be fully honest about what works
  today. A user who reads that `AuthorizationRuler` requires a policy grant
  and a receipt should not have to discover by experiment that no receipt
  can be issued yet.
- The behavior is deliberate: the framework blocks rather than act on
  unverified state. Saying so explains it as a safety property instead of
  leaving it to look like a defect.

**Since then.** The consent, durability and review producers now exist, each
installed only by explicit configuration. The decision still holds: the
limitation statement now says that a governed attempt stops unless all of
them are configured.

## 6. Details settled with the implementation

How the outcome carries the identifier, what the executor
does, and four smaller rules.

- *The outcome's identifier.* It is not a constructor parameter, so an
  outcome built by application code always has `None`. It takes no part in
  equality, hashing or `repr()`, so two outcomes with the same decision still
  compare equal and the text output does not change.
- *The executor's denial error.* The executor raises its own
  `authorization.denied` error when an outcome carries no receipt. It is
  created outside the attempt, so it has no identifier; carrying the
  outcome's identifier onto it would be a separate extension.
- *A failed condition whose report also fails.* The error keeps
  `failed_condition` and `attempted_conditions` next to
  `original_reason_code`, so that information is not lost.
- *Values set by the attempt replace any others.* When an evidence sink or a
  receipt store raises its own `SETTAuthorizationError`, the attempt sets its
  identifier on it, and `original_reason_code` when it applies, replacing any
  value already there, as the framework already does with
  `policy_observations`.
- *Events outside an attempt* carry no identifier.
- *Concurrent attempts* in different threads keep their own identifiers.
