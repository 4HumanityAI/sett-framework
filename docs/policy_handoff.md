# The policy-to-authorization handoff

Authorization never accepts an evaluation result from outside.
It opens its own private invocation on the policy ruler, which binds the exact
result to that invocation, and the binding is checked again before a receipt
is issued and before authority is used.

An evaluation result is not a bearer permit. `AuthorizationRuler.authorize()`
has no parameter that accepts one, and there is no public API that
promotes an observation into authority. Copying fields, rebuilding an
equal object, learning an identifier or obtaining a favourable result
from another invocation supplies nothing.

Authorization opens its own private invocation on the injected
PolicyRuler. The ruler registers the exact object it returned together
with a complete detached binding, and authorization revalidates that
binding before it issues a receipt and again before authority is
consumed.

A governed attempt completes only when all five closed verified-state
slots have a producer: `conditions` when condition verifiers are
configured, `consent` when the implementation declares that it owns
consent, `durability` when a governance journal is configured, and
`review` when review verifiers are configured with that journal.
Otherwise a bound evaluation stops and no receipt is issued; see
[Current limitation](authorization_reason_codes.md#current-limitation) and
[Verified state](#verified-state-completeness-before-authority) below.

## Private identities

Four private objects tie one evaluation to one ruler, one
receiver and one call; none can be built, exported or recovered from outside.

| Record | What it is |
|---|---|
| Ruler incarnation | One private object per constructed PolicyRuler |
| Receiver link | One private object per attached AuthorizationRuler, bound to that exact receiver |
| Invocation witness | One fresh private object per `authorize()` call, even for identical request bytes |
| Binding record | The complete detached state that makes one evaluation usable once |

None has a public constructor, export, serialization or recovery path.
Equality is object identity, never equality of declared ruler, issuer or
request labels. Public inspection creates no invocation.

This is structural validation inside cooperating Python. It is not a
cryptographic cross-process token, it is not protection against code that
deliberately rewrites private memory, and it promises neither network
atomicity nor forced interruption of a running Python call.

## What one binding retains

Everything needed to check later that nothing changed: the full
request and selection bytes, the generations, the checkpoint, the verified
state, the deadlines and, once issued, the exact receipt.

The binding record holds the ruler incarnation and receiver link, the
invocation witness, the exact returned evaluation object and its
identifier, the full canonical request bytes, the full canonical
selection bytes, the policy generation, the receiver's
approval-verifier generation, the checkpoint identifier, the complete
verified-state record, the evaluation and binding deadlines, the
lifecycle state and, once issued, the exact receipt and its receiver.

Canonical request and selection bytes use SETT's existing strict local
JSON rules: sorted keys, compact separators, `ensure_ascii=False`, finite
values, UTF-8, and the existing depth, item and byte bounds. They
preserve distinctions that an application fingerprint may normalize away,
such as `True` versus `1`, `1` versus `1.0` and signed zero. Bytes are
compared in full. The two components are bounded separately rather than
aggregated, and the request fingerprint is computed separately.

The application-selected receipt fingerprint keeps its own receipt
contract. It is never the proof of this cross-ruler binding, and the two
mechanisms are not unified.

## Verified state: completeness before authority

Five kinds of verified state must each be complete before any
receipt; a missing one is never treated as empty.

The closed slot names are `approvals`, `conditions`, `consent`, `review`
and `durability`. Each slot is either a verified explicit empty
requirement set or a complete producer-created record. **Absence is never
equivalent to empty.**

Each record carries its producer identity and schema revision, a local
epoch, the exact request and checkpoint scope, the required key set, the
observed key set, complete canonical value bytes, opaque evidence
references and an exclusive validity bound where applicable. Key sets are
sorted unique bounded identifiers, at most 256 per slot, and value bytes
use the existing 65,536-byte canonical limit. The receiver checks the
closed inventory, the producer and schema identity, the scope, the
equality of required and observed keys, the epoch and the validity. A
populated slot is never accepted because it carries a `complete` flag, a
digest, a resolvable reference or a claimed timestamp.

Only registered internal verification paths can install a slot, one for
each slot:

- `approvals` is produced by the attached authorization receiver, which
  owns approval verification. Its keys are the requirement identifiers
  whose authority a registered verifier confirmed for this exact request,
  and its value bytes carry the verified triples together with the
  receiver's verifier generation. It binds what was verified so that a
  later change is detected. It is **not** a claim that every declared
  requirement was satisfied: that is still the aggregate
  `REQUIRE_APPROVAL` semantics.
- `conditions` is produced by the condition producer, which `PolicyRuler`
  installs only when condition verifiers are configured; see
  [Condition verification](condition_verification.md#explicit-construction-and-independent-proof).
- `consent` is produced only when `PolicyRuler` is built with
  `implementation_owned_consent=True`, by an internal producer that
  records that the implementation owns consent; SETT verifies no consent.
- `durability` is produced only when `PolicyRuler` is built with a
  `governance_journal`: the producer writes the evaluation's decision
  record and returns a slot only once the journal confirms it; see
  [Decision records](decision_records.md).
- `review` is produced only when `PolicyRuler` is built with
  `review_verifiers` and a `governance_journal`: the producer checks
  that every restriction earlier decision records declared on this action
  type has a favorable completed review; see
  [Policy reviews](policy_reviews.md#the-review-producer).

No producer exists for `review` without review verifiers, `consent`
without that declaration, `durability` without a governance journal, or
`conditions` when no verifiers are configured. In each of those cases a
bound evaluation blocks with
`policy.evaluation_incomplete`, translated at authorization to
`authorization.policy_evaluation_incomplete`; see
[Current limitation](authorization_reason_codes.md#current-limitation). No public slot writer,
fixture loader, bypass or synthetic default exists in the product, and a
missing adapter never manufactures an empty slot. A synthetic producer
registered inside a test exercises this validation; it establishes no
production verifier and no deployment readiness.

Before issuance and before consumption, each producer confirms whether
its already verified state is still current. Confirmation never replaces
evidence, refreshes its checked time or extends a deadline. Any changed
canonical value, epoch or scope requires a new explicit evaluation;
callbacks are not retried automatically.

Pre-issue confirmation receives the original slot retained by the binding,
not a newly collected equal value. A negative, unavailable or failed confirmation
blocks issuance. Its result is checked at the verified-state stage of the fixed
receiver order; it does not mask an earlier selection or generation mismatch.
Confirmation callbacks run outside the guard, and subsequent generation checks
still apply. This is confirmation under the producer's contract, not continuous
monitoring of external state or an additional verification adapter.

## Lifecycle

Open, register, issue, consume, terminate; every step after the
first checks the binding again, and a failed check stops before the receipt
store is called.

1. **Open.** One invocation for the exact receiver, request and
   checkpoint. Under the guard the ruler snapshots the generation and
   registry and reserves a live-record slot. Outside the guard it
   resolves the selection, validates its complete inventory and evaluates
   through the single composer.
2. **Register.** After the callbacks the guard is reacquired. A changed
   generation, an invalid clock or an incomplete state rejects the run.
   Otherwise the exact immutable result and its binding are registered.
   Inspection never registers a claimable result. A failed stage releases
   the reservation and grants nothing.
3. **Issue.** Outside the guard, authorization obtains the current
   selection and the current verified-state confirmations, then computes
   the application fingerprint. Under the guard it runs the receiver
   checks below, claims the result once, reads the clock, and calls
   `receipt_store.issue`. Only then is the exact receipt bound. A
   non-grant outcome issues no receipt and terminates the invocation. No
   additional policy evaluation is hidden here.
4. **Consume.** Authorization obtains current declarations and
   confirmations, then under the same guard compares the private receipt
   binding, the exact request, the selection, both generations, the
   checkpoint, the state and the deadlines before calling the store's
   atomic verification once. The store keeps its own mismatch,
   revocation, expiry and consumption checks.
5. **Terminate.** A successful single-use consumption terminates the
   binding before control can reach an effect. Because the binding checks
   run first, a repeat presentation is rejected with
   `authorization.policy_binding_unavailable` rather than reaching the
   store's `authorization.receipt_consumed`; the store's own semantics stay
   as they are. Explicitly authorized controlled replay keeps its reuse
   semantics, and every use still satisfies the current binding checks.
   Restart cannot recreate a binding.

When `SETTExecutor` consumes the receipt, it keeps what the consumption
checked: both generations, the evaluation deadline, the receipt's expiry
and the time of consumption. After the effect barrier `handler.authorized`
and its trace exporters, SETT checks the grant before recording handler start.
It checks again after the start exporters and payload copy, immediately
before calling the handler. Each check samples the configured clock before
comparing the generations. A changed generation, a clock that went back or a
deadline reached meanwhile raises `authorization.policy_generation_changed`, `authorization.clock_invalid`,
`authorization.policy_evaluation_expired` or `authorization.receipt_expired`;
the receipt stays consumed. The rejection closes the authorized boundary with
`handler.blocked` or the start boundary with `handler.entry_blocked`. The
handler does not run. The gate cannot see an outside fact that an exporter
changes.

Receiver validation order before issue is fixed: exact result type;
registered result object from this incarnation; expected receiver and
invocation; unclaimed lifecycle; full request; full selection; both
generations; checkpoint; complete verified-state structure, full equality and
retained-state confirmation; clock
and deadline; grant invariants. Every failure has its own case, and no
store operation is attempted after a failed check.

There is one configured entry checkpoint, where the receipt is issued and
consumed. The record must match both the
invocation witness and that configured entry. Tests that corrupt witnesses on
purpose do not create a public way to use another checkpoint for a receipt.
Controlled checkpoints come after consumption, inside a controlled handler,
and verify conditions without a receipt; see
[Controlled checkpoints](controlled_checkpoints.md).

For consumption, the input type checks run first. Absence of the private
binding blocks even when the backend recognizes the receipt. A changed public
receipt field is reported as `authorization.receipt_mismatch`, and a changed
request as `authorization.request_mismatch`.

The receipt must be the exact object originally issued to this receiver.
A distinct object with identical fields is rejected with
`authorization.policy_binding_unavailable`, even while the issuer retains the
original live binding. Lookup by public receipt ID can locate a record but does
not make its binding available to a reconstructed object. Changed fields are
reported as `authorization.receipt_mismatch` when that record is found.

Equal copies managed by the application are not accepted, including shallow
and deep copies, dataclass reconstruction and deserialization. Passing the
original reference, including through a local queue that keeps that
reference, works with the binding checks. A backend may retain a
value copy without replacing the original receipt object held by the caller.
SETT supplies no receipt-transport or private-binding recovery API.

Object identity closes reconstruction only. It does not authenticate a person,
prevent another component holding the original reference from using it, or
isolate hostile Python code inside the process. It is not an access-control
mechanism. Evaluation-object identity and every other request, selection,
generation, checkpoint, evidence, deadline and store check remain mandatory.
Passing the reference through a queue does not make delivery after final
consumption safe; that delivery boundary is not defined.

`valid_until` is the minimum of the completion time plus the explicit
evaluation lifetime and every required verified-state validity limit. It
never extends a state proof. An observed time earlier than the retained
evaluation time, or an unusable clock reading, blocks. Receipt expiry
keeps its own bound; usable authority ends at the earlier of the two
deadlines. Time equality means expired. Reading the clock schedules no
work.

Live capacity counts pending invocations and receipt-bound evaluations.
Expired or terminal records may be purged when a later evaluation starts;
still-valid records are never evicted to make room. A full store fails
closed with `policy.evaluation_capacity_exceeded`, translated to
`authorization.policy_evaluation_capacity_exceeded`. A purged result is
unrecognized while a retained expired record produces the expiry
diagnostic; both block. Private canonical payload and state retention is
ephemeral and operational, not an evidence archive.

## One guard, callback phases and faults

One local lock serializes every change of authority or
configuration; no application callback runs while it is held, and a failure
after the backend call yields no usable authority.

PolicyRuler owns one local serialization guard. Registry writes, producer
registration, the attached receivers' authority-configuration changes and
the final receipt transition all serialize there. Each successful
mutation advances the generation once; a failed mutation changes nothing.

A receiver's configuration change holds that guard across the mutation
**and** its generation increment, not merely before them. Acquiring and
releasing the guard first and then mutating under the receiver's own lock
would leave a window in which a transition already in progress could read
a half-applied configuration or a stale generation.

For the same reason, both generations are read inside the guarded
transition, after every application callback has run. A value captured
before the callbacks is not the value the authority check uses: a
resolver, a policy, a verification callback or an application fingerprint
callback may register a verifier or a policy while it runs, and that
change must reject the pending binding with
`authorization.policy_generation_changed` rather than pass an out-of-date
comparison.

Resolvers, policy callbacks, verification callbacks, application
fingerprint callbacks, evidence sinks and trace exporters are never
invoked while the guard is held. The configured clock is read inside the
final section, immediately before and after the backend operation, so
time spent waiting cannot extend validity. Besides that clock reading,
only the receipt backend's own operations run inside the final guarded
transition. Lock order is the policy guard and then the backend's own
lock; no callback may take them in reverse, and a backend must not call
SETT recursively, invoke user effects or wait on another thread that
needs the policy guard. These are explicit integration constraints, not a
sandbox.

A same-thread attempt to enter a forbidden authority or configuration
mutation during the critical phase is rejected before any mutation with
`policy.transition_reentrant` and poisons that outer transition. Catching
the rejection inside a backend does not restore the outer call's
authority: it still fails with
`authorization.policy_handoff_reentrant`. Other threads serialize
normally and may invalidate a snapshot before the next guarded check. No
optimistic retry re-evaluates policies.

After a backend call the poison flag and the clock are rechecked before
any authority is returned. The clock recheck rejects three things, not
one: an unusable reading, a reading earlier than the observation taken
immediately before the backend call, and a reading at or past either the binding
deadline or the receipt expiry. The backward case uses that pre-backend observation as its
reference, which is itself never earlier than the retained evaluation
time, and it fails with `authorization.clock_invalid`. A clock that moves
backwards cannot be allowed to make an expired or out-of-order operation
look timely.

The binding deadline is checked first and reported as
`authorization.policy_evaluation_expired`. If it is still valid but the receipt
has expired, `authorization.receipt_expired` blocks the return.
This applies after both issue and consume, including controlled replay. Neither
deadline is extended or made equal to the other to avoid rejection.

An operation that crosses expiry, or that observes time moving backwards,
may already have written or consumed backend state; it yields no usable
receipt, binding or handoff, and the write is not undone, the deadline is
not renewed, and the framework does not claim the write did not happen. If
issue fails before or after an uncertain write, the invocation is
terminal and no live binding is returned; a backend-only orphan receipt
cannot later pass the binding lookup. If consumption fails after a
possible consume, authority is unusable and is not retried or restored.
A best-effort backend revoke is not the safety mechanism.

Successful guarded consumption consumes the receipt; it does not establish
that the handler ran. SETTExecutor checks retained authority again after
pre-effect exporters and immediately before calling the handler. A changed
generation, an invalid clock or an expired deadline can still block that
call, without restoring the consumed receipt. Direct callers of
verify_and_consume() do not receive these executor entry checks automatically.
Revoking a receipt after successful consumption does not cancel the pending
effect. Receipt revocation is not a post-consumption cancellation mechanism;
the executor's final checks cover the retained authority conditions described
above. Applications must not treat a later revoke call as proof that an effect
was prevented or reversed.
A copy of a receipt, including a deep copy, is refused with
`authorization.policy_binding_unavailable`. Present the object that
`authorize()` returned.
The journal and recovery records distinguish authorization, dispatch and
observed completion, including effects whose outcome is unknown. No local
check makes a remote effect atomic with storage or configuration changes.

## Condition verification: scope and limitations

The scope and limits of condition verification are described once, in
[Condition verification](condition_verification.md#condition-verification-scope-and-limitations).

### Explicit construction and independent proof

Condition verification is switched on explicitly through
`PolicyRuler`, and the policy layer checks every result against its own record
of what it expected.

**Configuration.** `PolicyRuler` takes the optional keyword
`condition_verifiers: Sequence[ConditionVerifier] | None = None`; its other
constructor keywords are unchanged.

- `None` installs no condition producer, so a bound evaluation is blocked.
- An empty sequence installs the condition producer, which then accepts only an
  inventory with no governing conditions. A governing condition without a
  matching verifier fails as `policy.condition_verifier_missing`. An empty
  sequence is never assumed when nothing is passed.
- At most 256 verifiers are accepted, and admission reads no more than 257
  entries. A value that is not a plain sequence (for example text, bytes or a
  mapping), an invalid identifier or version, and a duplicate
  `(verifier_id, verifier_version)` pair are rejected with
  `policy.ruler_configuration_invalid`.
- The set of verifiers is fixed after construction. The runtime protocol check
  only tests that the attributes exist; it validates neither signatures nor
  whether a verifier reports the truth.

**What the policy layer keeps.** For each evaluation, the policy layer keeps the
complete coverage of the selected policies and the canonical inventory of
governing conditions, both built without looking at any result.

- It checks each declaration against the policy's binding, the action ID and
  type, and the configured entry checkpoint or a controlled checkpoint of the
  action type. A mismatch in a required policy is
  invalid policy output; it is never filtered out to leave an empty inventory
  that passes.
- Inventory keys are ordinal (`condition.000001`, `condition.000002`, ...) and
  repeat across evaluations, so a key alone proves nothing.
- It compares its expected keys and the full canonical inventory bytes with the
  condition state it receives, besides checking that the required and observed
  keys are equal. A digest, or two sets supplied by the producer, cannot replace
  that comparison.
- A missing inventory, producer, coverage, callback or result is a structural
  failure, never an `evidence_not_present` observation.

**Limits.**

- The governing inventory is limited to 65,536 bytes, depth 12 and 256 entries
  per object or array. Informational conditions have their own capacity.
- If the informational part overflows, all of it is reported as unavailable,
  with a diagnostic for each policy involved; it is never truncated. The
  admitted decisions keep their declarations.
- The condition state has its own 65,536-byte limit and at most 256 evidence
  references.
- Requirements are never merged by verifier name or by evidence reference.

**Verification.**

- Before any callback runs, a first pass checks that every governing condition
  has a verifier with the exact ID and version.
- Each condition is then verified with one call, in key order, stopping at the
  first failure. Callbacks run outside the policy layer's internal lock.
- A result must be an exact `ConditionVerification` that carries the full
  expected declaration.
- With `RECHECK`, `checked_at` must fall between the clock readings taken just
  before and just after the call: `t0 <= checked_at <= t1`. A satisfied
  `ATTESTATION` result needs `checked_at <= t1 < valid_until`.
- The verifier must report time consistently with the policy ruler's injected
  clock. Inconsistent results are rejected, with no configurable tolerance.
  SETT does not check that an external clock is correct.

**Proof over time.**

- The first condition state and its observations are kept as they are.
- `RECHECK` conditions are verified afresh at the start, before a receipt is
  issued and before it is consumed. Later observations are added to a private
  history; they never replace the kept bytes, refresh `checked_at` or extend
  authority.
- `ATTESTATION` conditions need `confirm` to return exactly `True`, and keep
  their exclusive expiry. The earliest attestation deadline is added to the
  existing receipt and binding limits, including the checks made after the
  storage backend runs. Storage events already observed are not undone.

**Changes and interruption.**

- SETT does not detect changes in a verifier, inside or outside the process,
  and does not claim to. Fresh verification and confirmation follow their own
  contracts.
- A change of the shared policy generation invalidates every issuer that
  depends on it; a change local to a receiver affects only the bindings that
  depend on it.
- Python callbacks cannot be forcibly interrupted, and local serialization is
  not network atomicity.

**The callback.** What a callback receives, what it must not do and what stays
independent of it are described in
[Inputs supplied and withheld](condition_verification.md#inputs-supplied-and-withheld).
Later checkpoints are declared with `PolicyRuler(controlled_checkpoints=...)`;
see [Controlled checkpoints](controlled_checkpoints.md).

**Exceptions.**

- Exceptions raised by callbacks are sanitized: their raw cause, context and
  private data are not kept. `KeyboardInterrupt`, `SystemExit` and
  `GeneratorExit` propagate.
- If `authorize()` is called while the caller is handling another exception,
  the authorization error keeps that exception as its `__context__`. Among the
  condition values, `ConditionVerification` and the declaration errors of
  `PolicyDecision` go further: their validation errors carry no exception
  context, even inside a caller's handler. `PolicyCondition` keeps no raw
  validation exception either, but when it is built inside a caller's handler,
  the caller's exception becomes its `__context__`, as usual in Python.

### Invocation-local diagnostic snapshot

The private invocation keeps the admitted governing verdicts so
that a failure can report them; the snapshot is never an input to authority.

- During the initial bound evaluation, the invocation witness keeps a detached
  tuple of rows with the admitted governing policy's ID, version and verdict.
- Its private translation boundary attaches that tuple to the final
  `SETTAuthorizationError` for the same call, including a structured failure of
  the evidence sink. The invocation is closed as usual.
- The snapshot is not a binding field, a substitute for canonical state, a
  producer, a claim, a receipt or an input to authority. It adds no retry, no
  other engine and no permission to continue after a failure.

The [public attribute contract](api_reference.md#admitted-policy-observations-on-authorization-failure)
defines the possibly partial report and the difference between unavailable and
empty. The snapshot keeps no request, selection, binding object or raw
exception. The exception that carries it stays mutable and can be forged; it is
not a durable record or isolation from hostile code. Later failures of issue
or consumption, recovery and checkpoint scheduling are outside this report.
