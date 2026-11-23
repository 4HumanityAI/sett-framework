# Policy identity and provenance

`PolicyBinding` names a policy and its version, and records
where its requirements come from. It is declared metadata, not proof.

These metadata values describe a policy and its declared origins. They do not
evaluate policies, select applicable laws, verify evidence, activate a profile
or issue an authorization receipt. Their presence does not establish
satisfaction of an institutional requirement.

## Public values

Three frozen values, each with every field required: a source
reference, an origin and the binding that collects the origins.

Import `PolicySourceReference`, `PolicyOrigin` and `PolicyBinding` from
`sett.policy_ruler` or `sett.policy_ruler.models`. Imports through
`sett.authorization_ruler` and its `models` module still work and return the
same class objects. They are not exported from top-level `sett`. Constructors are keyword-only and
every field is required, including an explicit `None` for nullable fields.
Values are frozen and slotted. Construction accepts declared metadata, not
proof of its truth.

| Value | Fields | Meaning |
|---|---|---|
| `PolicySourceReference` | `source_id: str`, `source_version: str`, `locator: str` | An application registry identifier, exact edition label and provision locator. A locator is not a fetch instruction. |
| `PolicyOrigin` | `requirement_id: str`, `requirement_revision: str`, `layer_id: str`, `source: PolicySourceReference \| None`, `source_absence_reason: str \| None` | A requirement revision in a declared layer. Declare either the source or an explicit reason for its absence. Internal and owner-defined requirements are valid origins. |
| `PolicyBinding` | `policy_id: str`, `policy_version: str`, `contract_version: str`, `origins: Sequence[PolicyOrigin]`, `profile_id: str \| None`, `profile_revision: str \| None` | Policy identity, policy contract and complete declared provenance. Both profile fields must be absent or both present. |

Identifiers must be nonempty strings, trimmed, without ASCII control characters
or DEL, and at most 128 UTF-8 bytes. `locator` and `source_absence_reason` follow
the same text restrictions with a 1024-byte limit. Invalid UTF-8 is rejected.
Policy, source, requirement and profile versions are opaque identifiers; SETT
does not impose SemVer or rank them. `contract_version` must be exactly `"2.0"`.
Layer IDs express provenance without assigning legal priority or weight.

Nested sources and origins must have the exact declared types. Subclasses,
arbitrary mappings and objects with overridden projection methods are not
accepted as nested metadata. `origins` accepts finite sequences of 1 to 256
origins, excluding text, bytes and generators, and detaches them to a tuple.
An application-provided sequence still runs application code during traversal;
these value types are not a sandbox or a forced-interruption mechanism.

Within one `(layer_id, requirement_id)`, all origins must have the same
`requirement_revision`. Different source/version/locator links survive even
when one technical check will serve several requirements. Duplicate
`(layer_id, requirement_id, source_id, source_version, locator)` links are
rejected, using `None` for absent source fields. A different absence reason does
not make a duplicate internal origin distinct.

## Canonical representation

`canonical_view()` returns a fresh, complete and deterministic
form of each value, with a fixed size limit for the whole binding.

Each value provides `canonical_view() -> dict[str, object]`. It returns a fresh
complete mapping: nested values become objects, origins become an array, and
explicit absence becomes `null` in JSON. Mutating the input origins collection
or a returned projection does not change the stored value.

Origins are sorted by `(layer_id, requirement_id, requirement_revision,
source_present, source_id, source_version, locator)`. Empty strings are used only
as internal sort keys for absent source fields, never to replace recorded nulls.
The full binding is limited to 65536 UTF-8 bytes under SETT's existing canonical
JSON encoding: sorted object keys, no ASCII escaping, compact separators and no
NaN/infinity. Existing JSON limits of depth 12 and 256 items per container apply.
Bounds are inclusive and measure bytes, not characters.

The projection is operational metadata for comparison or recording. It is not
a public profile schema, journal format, decoder or safe public explanation.
Identifiers and text can still carry sensitive information; syntactic
validation does not sanitize them. Choosing what may be shown publicly is not
part of these values.

## Diagnostics

Invalid metadata raises `SETTAuthorizationConfigurationError`
with one of four codes.

Data validation raises `SETTAuthorizationConfigurationError`, with one of
these public `reason_code` values. Messages identify fields or categories
without echoing supplied values or chaining raw input errors. Ordinary Python
call errors, such as omitting a required keyword, remain `TypeError`.

| Code | Trigger |
|---|---|
| `authorization.policy_source_invalid` | Invalid source identifiers or locator |
| `authorization.policy_origin_invalid` | Invalid origin fields, nested source type or source/absence combination |
| `authorization.policy_binding_invalid` | Malformed contract field, invalid binding/profile fields, origins, conflicting revisions, duplicates or canonical bounds |
| `authorization.policy_contract_unsupported` | A well-formed contract identifier other than `"2.0"` |

Owning-model field types and bounds are validated before cross-field rules.
For example, `contract_version="1.0"` is unsupported, whereas
`contract_version=" 2.0"` is malformed and produces `policy_binding_invalid`.
The two cases report different codes and are never merged.

## Executable construction and rejection example

A binding with one owner-defined origin, and two contract
versions that are rejected for different reasons.

This example allows metadata construction and rejects an unsupported contract.
It demonstrates no permission to perform an effect.

```python
from sett.authorization_ruler import (
    PolicyBinding,
    PolicyOrigin,
    SETTAuthorizationConfigurationError,
)

origin = PolicyOrigin(
    requirement_id="owner-requirement",
    requirement_revision="r1",
    layer_id="internal",
    source=None,
    source_absence_reason="Owner-defined requirement",
)
fields = dict(
    policy_id="example-policy",
    policy_version="edition-blue",
    contract_version="2.0",
    origins=[origin],
    profile_id=None,
    profile_revision=None,
)
binding = PolicyBinding(**fields)
assert binding.canonical_view()["origins"][0]["source"] is None

try:
    PolicyBinding(**(fields | {"contract_version": "1.0"}))
except SETTAuthorizationConfigurationError as error:
    assert error.reason_code == "authorization.policy_contract_unsupported"
else:
    raise AssertionError("Unsupported contract accepted")

try:
    PolicyBinding(**(fields | {"contract_version": " 2.0"}))
except SETTAuthorizationConfigurationError as error:
    assert error.reason_code == "authorization.policy_binding_invalid"
else:
    raise AssertionError("Malformed contract accepted")
```

## Integration boundary

Registration and evaluation check results against the binding;
the metadata itself decides and enforces nothing.

Registering a policy requires its binding: omitting it fails with
`authorization.policy_binding_required`. Evaluation checks each policy result
against that registration: a result that names a different policy fails with
`authorization.policy_identity_mismatch`, and condition declarations that do
not match the binding fail with `authorization.policy_result_invalid`. The
metadata values themselves do not decide applicability, verify evidence or
enforce anything; see
[Current limitation](authorization_reason_codes.md#current-limitation).
The values download no sources and add no runtime dependency. Profile changes
are recorded as different values; creating them does not migrate a deployment or
rewrite a historical value. Source references alone cannot reproduce a past
execution without its code, configuration, declared facts and evidence.
