# Condition verification values and callback protocol

A `ConditionVerifier` is the application's callback that checks
one declared condition and reports the result as a `ConditionVerification`.
The policy layer calls it and checks every result against what it expected.

`ConditionVerificationState` and `ConditionVerification` are defined in
`sett.policy_ruler.condition_results`, and the `ConditionVerifier` protocol in
`sett.policy_ruler.contracts`. `sett.policy_ruler` exports the same three
objects; `sett.authorization_ruler` and top-level `sett` do not.

A structurally valid result is not current evidence and grants no authority.
Verifiers are configured explicitly on `PolicyRuler`, and the policy layer
calls them during authorization. A governed attempt reaches that point only
when every state producer is configured; see
[Current limitation](authorization_reason_codes.md#current-limitation).

## Callback protocol

Two methods. `verify` reports one observation; `confirm` says
whether an earlier observation still holds.

```python
@runtime_checkable
class ConditionVerifier(Protocol):
    verifier_id: str
    verifier_version: str

    def verify(
        self, request: AuthorizationRequest, *, condition: PolicyCondition
    ) -> ConditionVerification: ...

    def confirm(
        self, verification: ConditionVerification, *, request: AuthorizationRequest
    ) -> bool: ...
```

- `ConditionVerifier` names an application callback, not an Expert, Agent,
  Adapter, Ruler or producer.
- `verify` reports one observation for the declared condition.
- `confirm` answers whether that exact observation is still valid for the
  supplied request. It does not create a replacement, renew a deadline,
  authorize an action or perform a required recheck. Only exact `True` and
  `False` conform to the protocol, and anything other than `True` counts as
  not confirmed.
- Verifiers are registered only through `PolicyRuler(condition_verifiers=...)`,
  which checks their identities when the ruler is built; see
  [Explicit construction and independent proof](#explicit-construction-and-independent-proof).
  A runtime `isinstance` check tests only that the attributes exist; it
  validates neither signatures, returned values nor truthfulness. There is no
  default verifier, inferred alias, network discovery or fallback.

## Inputs supplied and withheld

The callback sees the whole request, but not the policy
selection or the actual checkpoint, and nothing it sees can replace the
policy layer's own checks.

### What the callback receives

- `verify` receives the request and the declared condition; `confirm` receives
  an earlier result and the request. Neither receives the policy selection or
  the actual execution checkpoint, and `confirm` does not receive the condition
  again: the declared checkpoint can be read from the condition.
- Both receive the complete `AuthorizationRequest`, including its payload
  through `payload_copy()`. That is broad access, not a reduced or public-safe
  view. Hidden `repr` fields, frozen values and detached views are not access
  control, redaction or protection from hostile Python, and a field's declared
  trust does not prove identity or consent.

### What the callback must not do

- Recover the missing inputs, the selection or the actual checkpoint, through
  rewritten requests, overloaded identifiers, ambient configuration or a
  preconfigured mutable selection. A real need for one of them would be a
  change to this interface, designed explicitly; the interface does not rule
  that out.
- Take reading the declared checkpoint as proof of having reached it. The
  actual and the declared checkpoint are checked separately inside the policy
  layer, and a fabricated matching declaration is not accepted.

### What stays independent of the callback

- The policy layer's comparison of the complete request and the authoritative
  approvals does not depend on what the callback sees: no filtered view, digest
  or fingerprint replaces it, and no approval input is left out.
- Canonical views produced by a callback, including views with empty
  approvals, do not prove which approvals the real invocation kept.
- The policy layer compares each result with the declaration it expected,
  taken from its own retained inventory, not from the result. Values copied
  from the result, or an equal declaration from another invocation, do not
  prove membership. A fresh recheck and the retained proof stay separate.
- An application that relies on correlation or execution fields owns that
  dependency; narrowing this interface later would be a public contract change
  with a migration cost.

## Result value

A `ConditionVerification` records one observation: its state,
when it was made, how long a positive result may be used, and the references
and reasons behind it.

```python
ConditionVerification(
    *,
    condition: PolicyCondition,
    state: ConditionVerificationState,
    checked_at: datetime,
    valid_until: datetime | None,
    evidence_refs: Sequence[str],
    reason_codes: Sequence[str],
)
ConditionVerification.canonical_view(self) -> dict[str, object]
```

All six keywords are mandatory, including an explicit `None`. The value is
frozen, slotted and keyword-only. It has no field for a raw payload, an
exception, a free-text explanation, a percentage, a score, a stored projection
or authority.

| Enum member | Serialized value | Reported meaning |
|---|---|---|
| SATISFIED | `satisfied` | The verifier reports the declared criterion satisfied within the observation scope |
| UNSATISFIED | `unsatisfied` | An evaluated criterion was not met |
| EVIDENCE_NOT_PRESENT | `evidence_not_present` | Required evidence was not supplied for this evaluation |
| EVIDENCE_INVALID | `evidence_invalid` | Supplied evidence failed its verification criterion |
| EVIDENCE_UNAVAILABLE | `evidence_unavailable` | The required evidence check could not obtain or consult its dependency |

These are observation states only, separate from applicability, validity
mode and final authorization.

- `SATISFIED` requires at least one opaque evidence reference and may carry no
  reasons. Even an entirely local check needs an identifiable observation
  record supplied by the verifier or the application; the reference alone
  proves nothing.
- Every other state requires at least one reason identifier and
  `valid_until=None`. `EVIDENCE_NOT_PRESENT` has no evidence references; the
  other negative states may keep bounded opaque references.
- A positive observation of a `RECHECK` condition has `valid_until=None`: no
  reusable attestation is claimed, which is not the same as timeless validity.
- A positive observation of an `ATTESTATION` condition requires
  `checked_at < valid_until <= condition.attestation_not_after`. Being equal to
  the declared limit is allowed, because that limit is an exclusive expiry;
  being equal to `checked_at` is invalid.
- `None` never means unlimited validity. Construction consults no clock, so
  historical values can be represented; construction establishes neither
  freshness, revocation nor source availability.

## Admission, canonical view and bounds

Every field is checked in a fixed order, and the whole result
must fit the usual canonical limits.

- `condition` must be an exact `PolicyCondition` and `state` an exact
  `ConditionVerificationState` member; no property of the nested declaration
  is read before that check. `checked_at`, and `valid_until` when present, must
  be timezone-aware and are stored in UTC.
- Each evidence reference and reason code follows the usual identifier rule:
  non-empty, trimmed, at most 128 UTF-8 bytes, without ASCII control characters
  or DEL. Each collection is a finite sequence of at most 256 entries; text,
  bytes, bytearrays, memoryviews and mappings are rejected. Reading stops at
  most one entry past the limit, and a wrong reported length is rejected.
  Duplicates are rejected, not dropped, and the values are kept as sorted
  tuples.
- `canonical_view()` returns fresh data with exactly the six field names. The
  nested condition uses its complete canonical form, times use six fractional
  digits and `Z`, and the sequences become lists.
- The whole view must fit 65,536 UTF-8 bytes, depth 12 and 256 entries per
  object or array. A result can exceed that limit even when its condition alone
  fits; it is then rejected, with no truncation and no larger limit. The
  condition state that collects all the results of an evaluation has its own
  limits; see [Explicit construction and independent proof](#explicit-construction-and-independent-proof).
- The view is full operational data, not a public-safe trace or an authority
  token.

The checks run in this order:

1. The exact state, then `checked_at` and `valid_until`.
2. The exact nested declaration.
3. Evidence references and reason codes: container, count and each identifier.
4. The relations between state, validity mode and time described above.
5. The canonical limits of the whole view.

## Invalid results

Any invalid field raises one fixed error,
`policy.condition_verification_invalid`, with no exception context.

Invalid field values raise `SETTPolicyConfigurationError` with
`reason_code="policy.condition_verification_invalid"` and exactly the message
`The condition verification result is invalid.` Its `verdict` is `None` and
it carries no receipt identity.

- Its message, cause and context keep no raw validation exception and no
  submitted value. Its `__cause__` and `__context__` are `None`, even when the
  value is built while the caller is handling another exception.
- Ordinary validation exceptions are translated at the constructor;
  `KeyboardInterrupt`, `SystemExit` and `GeneratorExit` propagate. This is not
  a global exception handler or a sandbox.
- Missing or unexpected keywords keep Python's ordinary `TypeError`, and
  building an invalid enum member directly keeps ordinary `Enum` behavior.
  Errors of the condition declaration itself stay `policy.condition_invalid`.
- No result at all, an unregistered verifier and malformed callback output are
  failures of the call, reported with their own codes, such as
  `policy.condition_result_invalid` or `policy.condition_verifier_missing`.
  They are not extra enum states and not an `evidence_not_present` observation.

## Declared conditions on a policy decision

A decision declares conditions; it does not observe them.

A `PolicyDecision` carries an explicit declaration of its policy's conditions.
That declaration is not an observation. A missing declaration is a
construction error, not `evidence_not_present` or any other
`ConditionVerificationState`, and an empty declaration produces no
`ConditionVerification`. Building a decision calls no verifier.

During evaluation, the policy layer builds its retained inventory from the
conditions marked `required=True` in required policies. Results are compared
with that inventory, not with the decision.

## How the policy layer uses results

The policy layer, not the callback, decides whether a result
counts.

- Anyone can build and serialize an observation. Only the policy layer binds a
  verified invocation to its retained state; submitting a rebuilt observation
  cannot establish that binding.
- The policy layer compares each result with the expected condition from its
  retained inventory, even if a callback accepts any input. Values taken from
  the result alone do not prove membership, and an equal declaration from
  another invocation is not enough.
- A fresh recheck stays separate from the retained proof and its confirmation.
  A newer observation does not replace the retained proof or relax the
  comparison of the complete retained state.
- A missing callback, inventory or result is a structural failure.
- Verifier registration and identity, the order of failures, retention and
  confirmation are described in
  [Explicit construction and independent proof](#explicit-construction-and-independent-proof).
  A condition of a controlled checkpoint is verified when the handler
  reaches it; see [Controlled checkpoints](controlled_checkpoints.md).
- Callbacks run outside the policy layer's internal lock, without Python
  sandboxing or forced interruption.

See [condition declarations](policy_conditions.md),
[the handoff contract](policy_handoff.md), [API reference](api_reference.md),
[reason codes](policy_reason_codes.md) and [terminology](terminology.md).

## Condition verification: scope and limitations

What configuring condition verifiers gives you today, and what
it does not.

Configuring condition verifiers installs the condition producer. It does not
verify consent, review or durability, and it provides no later execution
checkpoints, recovery or final delivery. Unless the consent, review and
durability producers are configured too, a governed attempt stops before any
condition is verified; see
[Current limitation](authorization_reason_codes.md#current-limitation).
Producers registered inside tests are synthetic and establish no real effect
or integration.

- Only required declarations from admitted, required and applicable policies
  govern. Informative declarations produce no observation: they are kept with
  their origins and never grant, block, replace or use up the capacity of
  governing requirements.
- Public inspection returns informational diagnostic pairs; authorization does
  not expose them. A later inspection is a new evaluation: it may call
  callbacks again and see a different context, so it does not recover the
  authorization attempt. The informational diagnostics of an attempt are not
  delivered, and no event, sink write, reporting callback, diagnostic store or
  persistence exists for them.
- The check for missing state producers runs after the resolver and the policy
  callbacks, and before any state producer is called. It is not a check made
  before policy evaluation.
- A failed condition blocks; it never becomes a DENY verdict. An observation
  cannot supply authority or override a denial. When a policy denies and a
  required condition fails in the same attempt, the condition failure is the
  result, and the denial is reported in
  [`SETTAuthorizationError.policy_observations`](api_reference.md#admitted-policy-observations-on-authorization-failure).
  The failing condition is reported in `failed_condition`; see
  [authorization failure details](authorization_failure_details.md).

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
[Inputs supplied and withheld](#inputs-supplied-and-withheld).
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
