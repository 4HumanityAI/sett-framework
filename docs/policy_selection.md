# Policy selection value contracts

A `PolicySelection` is the application's declaration of which
policies apply to one exact action, and why. `PolicyRuler` reads it before
running any policy; the values decide nothing by themselves.

These public types validate and represent declarations. Constructing them does
not select or evaluate policies, verify applicability, issue or invalidate
receipts, or authorize an effect. `PolicyRuler` reads a declared selection
through the application's resolver and checks it before any policy runs; see
[PolicyRuler](policy_ruler.md#declared-selection). It cannot authorize an
effect yet; see [Current limitation](authorization_reason_codes.md#current-limitation).

The types are defined in `sett.policy_ruler.models` and exported by
`sett.policy_ruler`. Imports through `sett.authorization_ruler` and its
`models` module still work and return the same objects. They are not exported
from the top-level `sett` namespace.

## Applicability

For each policy in the selection, the application declares
whether it applies, does not apply, or cannot be determined, and on which
facts that rests.

`PolicyApplicabilityState` has three exact members: `APPLIES` (`"applies"`),
`DOES_NOT_APPLY` (`"does_not_apply"`) and `UNDETERMINED` (`"undetermined"`).
These are application declarations, not legal conclusions or authority verdicts.

```python
PolicyApplicability(
    *, policy_id: str, policy_version: str,
    state: PolicyApplicabilityState, justification: str,
    fact_names: Sequence[str], evidence_refs: Sequence[str],
)
```

Every field is required, including empty sequences. The justification is trimmed,
nonempty text of at most 1024 UTF-8 bytes. Identifier fields and references are
trimmed, nonempty strings of at most 128 UTF-8 bytes. ASCII controls and DEL are
rejected. References are unique, sorted and bounded at 256 entries per sequence.
Raw strings are not coerced to enum members. The model preserves the declared
state; it neither validates the explanation's legal correctness nor resolves
evidence references.

## Exact-action selection

One selection covers one exact action: its required and
informative policies, one applicability entry for each, and the facts those
entries refer to.

```python
PolicySelection(
    *, selection_id: str, selection_revision: str, contract_version: str,
    authority_ref: str, effective_at: datetime, action_id: str, action_type: str,
    required: Sequence[PolicyBinding], informative: Sequence[PolicyBinding],
    applicability: Sequence[PolicyApplicability],
    facts: Mapping[str, FieldValue | None],
)
```

All fields are explicit. Identifiers use the bounds above. `contract_version`
must be `"2.0"`. The authority reference records declared provenance without
authenticating identity. `effective_at` must be timezone-aware and is normalized
to UTC. Its canonical text uses six fractional digits and `Z`. It represents a
not-before time; this value model does not compare a clock, schedule work or
establish evidence freshness.

Required and informative inventories together contain at most 256 bindings.
A policy ID occurs at most once across both inventories, even if its versions
differ. Each binding has exactly one matching applicability entry by ID and
version. Entries are sorted by those two fields; opaque versions are not ranked
by age or precedence. Empty inventories are representable data, not permission.

Facts contain exactly the keys referenced by applicability entries, with at most
256 keys. Use `None` to represent a missing fact. An exact `FieldValue` whose
value is JSON null instead retains its provenance. Proposed and unknown facts
are representable without becoming authoritative. Jurisdiction, sector, purpose
and other application facts are not inferred or predefined by SETT.

Sequences must be finite `Sequence` objects; strings, bytes, mappings and
generators are not sequence inputs. Nested bindings, applicability records and
facts must have the exact model types, so subclass projection overrides are
not accepted. Collection counts and traversal are bounded. The complete canonical
value must fit 65,536 UTF-8 bytes, JSON depth 12 and 256 items per array/object.
Valid nested values can exceed these aggregate limits when combined.

## Immutability, projection and diagnostics

The values are frozen, their canonical view is full operational
data, and invalid input raises one of three codes in a fixed order.

The models are frozen and slotted. Input containers are detached, sequences
become tuples and the facts mapping is read-only. `canonical_view()` returns a
fresh full representation with all constructor fields. It includes fact values,
justifications and evidence references. It is operational data, **not a public-safe
trace, a persistence schema or an authorization token**. Mutating its returned
lists or dictionaries does not change the model.

Structural failures raise `SETTAuthorizationConfigurationError` without echoing
supplied values or retaining raw validation exceptions. Applicability defects use
`authorization.policy_applicability_invalid`. Selection defects, cross-reference
failures and aggregate bounds use `authorization.policy_selection_invalid`.
A well-formed but unsupported contract version instead uses
`authorization.policy_contract_unsupported`.

Scalar errors follow constructor field order. A valid unsupported version is
checked after the remaining scalar fields and before collection contents.
Collection type/count and nested-type validation precede duplicate and
cross-reference rules; aggregate canonical limits are checked last. The nested
metadata constructors keep their own diagnostics if they fail before a
selection is created.

## Integration boundary

`PolicyRuler` uses the selection to decide which policies run;
reading a selection is not yet enough to authorize an effect.

`PolicyRuler` takes the resolver, registers each policy with its binding,
checks applicability and composes the results; it validates the declared
selection before any policy callback. `AuthorizationPolicy.evaluate()` keeps
its signature and the verdict semantics of its `PolicyDecision`; the binding
carries version and provenance separately. There is no migration switch and no
fallback: the old two-argument registration call fails with
`authorization.policy_binding_required`.

Each `PolicyDecision` that `evaluate()` returns carries explicit `conditions`
and `review_obligations` declarations, `()` when there are none; see
[the API reference](api_reference.md#policydecision-condition-declaration).
Selection and composition do not read these declarations, and they do not
enter the aggregate decision. Condition declarations are checked separately
during evaluation; see [Policy condition declarations](policy_conditions.md).

Reading a selection is not enforcing one. The bound authorization path issues
a receipt only when every producer its verified-state record requires is
configured; see
[Current limitation](authorization_reason_codes.md#current-limitation).

Tests of these value models check structure only. The permit and block
examples in the handoff tests use synthetic state producers that exist only in
tests: they exercise the real validation, and establish no real verifier and
no deployment readiness.

See [PolicyRuler](policy_ruler.md), [the handoff contract](policy_handoff.md),
[policy identity](policy_identity.md), [terminology](terminology.md),
[policy reason codes](policy_reason_codes.md) and
[reason codes](authorization_reason_codes.md).
