# Policy condition declarations

A `PolicyCondition` declares a check that must pass before an
action: which verifier performs it, and for how long a positive result may be
used. The declaration verifies nothing by itself.

`PolicyCondition` and `ConditionValidityMode` are defined in
`sett.policy_ruler.conditions`, and `sett.policy_ruler` exports the same
objects. `sett.authorization_ruler` and top-level `sett` do not export them.

A declaration is data. By itself it is not a verifier, a verification result,
an attestation, a registered checkpoint or a permission for an effect, and it
does not resolve dependencies or verify evidence. When condition verifiers are
configured, the policy layer is set up to verify the required declarations
during authorization. A governed attempt reaches that point only when every
state producer is configured; see
[Current limitation](authorization_reason_codes.md#current-limitation).

## Construction and meaning

Eleven mandatory keywords, all checked when the value is built.

```python
PolicyCondition(
    *,
    condition_id: str,
    binding: PolicyBinding,
    origins: Sequence[PolicyOrigin],
    verifier_id: str,
    verifier_version: str,
    required: bool,
    action_id: str,
    action_type: str,
    checkpoint_id: str,
    validity_mode: ConditionValidityMode,
    attestation_not_after: datetime | None,
)
```

- All eleven keywords are mandatory, including an explicit
  `attestation_not_after=None` when there is no limit. The value is frozen and
  slotted, and keeps its own copy of the origins.
- There is no field for a callback, predicate, payload, evidence content or
  arbitrary parameters.
- A condition is identified by its policy ID and version together with its
  condition ID. It also names an exact action ID and type and a checkpoint. A
  single value does not establish uniqueness within a collection, a link to a
  request, or reuse.

**Fields.**

- The six identifier fields (`condition_id`, `verifier_id`,
  `verifier_version`, `action_id`, `action_type`, `checkpoint_id`) follow the
  usual identifier rule: non-empty, trimmed, at most 128 UTF-8 bytes, without
  ASCII control characters or DEL. Versions are opaque: they imply no
  chronology, priority or legal applicability.
- `required` must be an exact `bool`. It cannot turn an informative policy into
  a governing one, and it grants no authority.
- The verifier ID and version and the checkpoint ID are names. They do not
  prove that a verifier is registered or available, or that the checkpoint is
  an executable boundary. During evaluation the checkpoint must be the entry
  checkpoint or one of the action type's
  [controlled checkpoints](controlled_checkpoints.md).

**Binding and origins.**

- `binding` must be an exact `PolicyBinding`.
- `origins` is a finite sequence of 1 to 256 exact `PolicyOrigin` values, and
  each must be a member of the binding in its full canonical form. Mappings,
  text, bytes, bytearrays, memoryviews, generators and subclasses of the nested
  values are rejected.
- Reading stops at most one entry past the limit. A sequence whose reported
  length is wrong, or that fails while being read, is rejected; this does not
  forcibly interrupt arbitrary Python callbacks.
- Duplicate origins are rejected, even when they are distinct objects with the
  same canonical form. The chosen subset is kept in the binding's canonical
  order, and the full provenance stays visible in the binding. Nothing is
  inferred, merged or dropped.

## Explicit validity mode

Each condition says whether it must be verified again on every
use (`RECHECK`) or may rely on a result until a fixed time (`ATTESTATION`).

`ConditionValidityMode(str, Enum)` has `RECHECK = "recheck"` and
`ATTESTATION = "attestation"`. The constructor accepts the members, not raw
strings, and no mode is chosen by default.

- `RECHECK` requires `attestation_not_after=None`. The policy layer verifies
  the condition afresh at the start of an evaluation, before a receipt is
  issued and before it is consumed; construction verifies nothing.
- `ATTESTATION` requires a timezone-aware absolute limit, stored in UTC. A
  satisfied result must expire no later than this limit, and the limit itself
  is exclusive: from that exact time on, no result can support an effect. The
  declaration is neither an attestation nor proof of validity.
- A past limit can be declared, for historical records. Construction uses no
  clock and has no validity query, universal lifetime, cache, fallback or
  recovery path. A limit that cannot be converted to UTC makes the declaration
  invalid.
- Verification results keep their own state and context. Unavailable evidence
  cannot extend a deadline or authorize part of an action.

## Canonical view and aggregate bounds

`canonical_view()` returns the declaration as plain data ready
for canonical JSON, and that data has a fixed size limit.

- `canonical_view() -> dict[str, object]` returns fresh nested data with
  exactly the eleven constructor field names. The binding and origins keep
  their complete accepted forms; sequences become lists, the mode becomes its
  value, `required` a JSON boolean, and a missing limit `null`. Times are in UTC
  with six fractional digits and `Z`.
- The whole view must fit 65,536 UTF-8 bytes, depth 12 and 256 entries per
  object or array. The root is depth zero, and each nested value adds a level.
- Canonical JSON sorts object keys, uses compact separators without ASCII
  escaping, rejects non-finite numbers and is encoded in UTF-8. Dictionary
  iteration order is not a byte-encoding promise. There is no configurable
  normalizer, and the declaration is not part of the request fingerprint.
- The limits apply to the whole: valid nested data, or an allowed number of
  origins, can still exceed the budget of the complete declaration. Nothing is
  truncated, and no provenance is dropped to make an oversized value fit.
- The view is full operational data. It is not a public-safe trace, a
  persistence or profile schema, or an authority token.

## Invalid declarations and precedence

Any invalid field raises one fixed error,
`policy.condition_invalid`, and the checks run in a fixed order.

Invalid field values raise `SETTPolicyConfigurationError` with
`reason_code="policy.condition_invalid"` and exactly the message
`The policy condition declaration is invalid.` The class is the same object in
`sett.exceptions`, `sett.policy_ruler.errors` and `sett.policy_ruler`. It never
becomes an authorization error, a decision or a receipt.

The checks run in this order:

1. Scalar fields in their relative constructor order: condition ID, verifier
   ID and version, exact bool, action ID and type, checkpoint, exact mode, then
   the optional aware timestamp and its conversion to UTC.
2. Exact binding, the origin container and count, and the exact type of each
   origin.
3. Duplicate origins, full canonical membership, then consistency between mode
   and time.
4. The canonical limits of the whole view.

- The error reports invalid structure, not a condition that failed at run time
  or institutional noncompliance.
- Its message, cause and context keep no raw validation exception and no
  submitted value. Ordinary validation exceptions are translated at the
  constructor; `KeyboardInterrupt`, `SystemExit` and `GeneratorExit` propagate.
  This is not a global exception handler or a sandbox.
- Missing or unexpected keywords keep Python's ordinary `TypeError`. Building an
  invalid enum member directly keeps ordinary `Enum` behavior. A nested
  constructor that fails before this one is called keeps its own error.
- The exception class itself does not sanitize arbitrary messages from callers.

## Attachment to a policy decision

A `PolicyDecision` carries its conditions in `conditions`, and
the policy layer checks them during evaluation.

`PolicyDecision` requires an explicit `conditions` declaration: a sequence of
these values, all bound to the decision's policy ID, or `()` when the policy
declares none. The empty sequence is the policy's explicit statement that the
decision carries no conditions; SETT keeps it, but does not verify that it is
complete or true. Leaving the declaration out is incomplete configuration:
construction fails with Python's ordinary `TypeError`, so no decision exists.
`None` is invalid, and neither case stands for the other.

The decision accepts exact `PolicyCondition` entries and validates each one
again through its canonical view and bounds; it does not rerun this
constructor. A value that fails its own construction keeps
`policy.condition_invalid`; a failure while the decision validates an entry
again reports `policy.condition_inventory_invalid`.

During evaluation, the policy layer checks each declaration against the
policy's registered binding, the request's action ID and type, and its entry
checkpoint. Conditions with `required=True` in required policies form the
inventory that the configured verifiers must verify; the others are kept as
information. A declared condition is not an `AuthorizationConstraint`, and
building a decision calls no verifier. The
[API reference](api_reference.md#policydecision-condition-declaration) lists
the complete rules.

## What else authorization needs

Conditions are one of five kinds of verified state. Each has a
producer only when it is configured, and a governed attempt stops before any
condition is verified unless all of them are.

The verified state of an authorization covers approvals, conditions, consent,
review and durability. Approvals have a producer, and conditions have one when
verifiers are configured. Consent has one when the implementation declares
that it owns consent, durability when a governance journal is configured,
and review when review verifiers are configured with that journal. Missing
required state blocks the attempt, and no empty default replaces it. Building
a value is not a run-time condition check.

The [policy handoff](policy_handoff.md) and [PolicyRuler](policy_ruler.md)
describe the rest of the boundary: local synchronization, mandatory injection, shared invalidation and
exact-object receipt semantics. See also the [API reference](api_reference.md),
[reason codes](policy_reason_codes.md) and [terminology](terminology.md).

## Condition verification: scope and limitations

The scope and limits of condition verification are described once, in
[Condition verification](condition_verification.md#condition-verification-scope-and-limitations).
