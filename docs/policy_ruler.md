# PolicyRuler

`PolicyRuler` owns policy governance: it registers policies,
reads the declared selection, evaluates and composes the policies, and keeps
their obligation state. Final authority and receipts stay with
`AuthorizationRuler`.

PolicyRuler is the single owner of policy governance: the authoritative
registry, the declared selection, evaluation, composition and policy
obligation state. AuthorizationRuler keeps final authority and receipts.
Execution keeps the effect boundary. There is no second registry, no
second policy engine and no legacy route to an effect.

`PolicyRuler` reads a declared selection and runs the registered callbacks.
An effect can be authorized only when the verified-state record required
before any receipt is complete, which needs every state producer configured;
see
[Current limitation](authorization_reason_codes.md#current-limitation) and
[the handoff contract](policy_handoff.md).

## Construction

Every setting is explicit. There are no defaults, no shared
instance and no background service.

```python
PolicyRuler(
    *,
    ruler_id: str,
    entry_checkpoint_id: str,
    evaluation_ttl_seconds: float,
    max_live_evaluations: int,
    clock: AuthorizationClock,
    evidence_sink: AuthorizationEvidenceSink,
    selection_resolver: PolicySelectionResolver | None = None,
    condition_verifiers: Sequence[ConditionVerifier] | None = None,
    implementation_owned_consent: bool = False,
    governance_journal: GovernanceJournal | None = None,
    review_verifiers: Sequence[ReviewVerifier] | None = None,
    controlled_checkpoints: Mapping[str, Sequence[str]] | None = None,
)
```

There is no default policy set, no shared singleton, no default evaluation
lifetime, no background cleanup service and no automatic eviction of
still-valid authority.

| Field | Contract |
|---|---|
| `ruler_id` | Trimmed, non-empty, at most 128 UTF-8 bytes. A label for diagnostics, never proof of instance identity |
| `entry_checkpoint_id` | The named authorization-entry boundary, where the receipt is issued and consumed. Declaring the name creates no handler hook; later checkpoints are declared with `controlled_checkpoints` |
| `evaluation_ttl_seconds` | An explicit finite positive number, excluding `bool`; `int` is accepted. A duration outside the representable datetime range is invalid |
| `max_live_evaluations` | An exact `int` in 1..256, no `bool` and no default. A resource limit, not a confidence or compliance score |
| `clock` | The `AuthorizationClock` protocol. It must be a read-only, non-blocking observation with no SETT reentry and no external call |
| `evidence_sink` | The `AuthorizationEvidenceSink` protocol |
| `selection_resolver` | Required in practice: `None` raises `authorization.policy_selection_resolver_missing` |
| `condition_verifiers` | Optional. `None` installs no condition producer, so a bound evaluation is blocked; a sequence of at most 256 verifiers installs it. See [Condition verification](condition_verification.md#explicit-construction-and-independent-proof) |
| `implementation_owned_consent` | Optional exact `bool`, default `False`. `True` declares that the implementation built on SETT owns consent: an internal producer records that declaration in the consent slot, and SETT verifies no consent. `False` installs nothing. Fixed for the ruler's life and readable through the property of the same name |
| `governance_journal` | Optional `GovernanceJournal`, default `None`. A journal installs the durability producer: every completed bound evaluation writes one decision record, and the durability slot exists only once the journal confirms it. `None` installs nothing. Any other value is `policy.ruler_configuration_invalid`. See [Decision records](decision_records.md) |
| `review_verifiers` | Optional sequence of at most 256 review verifiers, default `None`. It installs the review producer and requires `governance_journal`: a restriction that an earlier decision declared on this action type blocks until its verifier reports a favorable completed review. `None` installs nothing. Review verifiers without a journal, a malformed sequence or a repeated verifier ID and version are `policy.ruler_configuration_invalid`. See [Policy reviews](policy_reviews.md#the-review-producer) |
| `controlled_checkpoints` | Optional mapping, default `None`, from an action type to its ordered later checkpoint IDs: valid identifiers, distinct within the action type, not the entry checkpoint, at most 16 per action type and 256 action types. A condition may then name one of them; it is verified when a controlled handler reaches that checkpoint. Anything else is `policy.ruler_configuration_invalid`. See [Controlled checkpoints](controlled_checkpoints.md) |

Validation runs in this order: IDs, TTL, capacity, clock, sink, resolver, the
consent declaration, the governance journal, whether review verifiers have a
journal, the condition verifiers, the review verifiers, then the controlled
checkpoints. Malformed
constructor input, including an invalid verifier sequence, raises `SETTPolicyConfigurationError` with
`policy.ruler_configuration_invalid`. A missing resolver raises
`authorization.policy_selection_resolver_missing`, and an invalid one
`authorization.policy_selection_resolver_invalid`. No registry is usable after
a failed constructor.

Each instance creates a fresh private incarnation and a generation counter
starting at zero.

## Registration

Each policy is registered once, with its binding, for one action
type; nothing can be removed or replaced afterwards.

```python
PolicyRuler.register_policy(
    self,
    action_type: str,
    policy: AuthorizationPolicy,
    *,
    binding: PolicyBinding | None = None,
) -> None
```

- The keyword default exists only to diagnose the old two-argument call with
  `authorization.policy_binding_required`; it never registers a policy without
  metadata.
- Checks run in this order: `action_type`, the missing or wrong binding, the
  policy protocol and its identifier, the match between identifier and
  binding, then the duplicate key.
- A failed registration leaves the registry unchanged. A successful one
  advances the instance generation exactly once.
- `AuthorizationRuler.register_policy` has the same signature and delegates
  here once. It keeps no dictionary of its own.
- There is no removal, replacement or unregistration, and no setter for the
  resolver or the verifiers. Changing a registered implementation means
  configuring a new instance.

## Declared selection

The application's resolver declares which policies apply to each
request, and that declaration is checked completely before any policy runs.

The application owns the resolver:

```python
@runtime_checkable
class PolicySelectionResolver(Protocol):
    resolver_id: str

    def resolve(self, request: AuthorizationRequest) -> PolicySelection: ...
```

It receives the request, already detached, and returns the declaration for
that exact action. Validation runs before any policy callback:

1. Callback failure is `authorization.policy_selection_failed`; a wrong
   return type is `authorization.policy_selection_result_invalid`. The
   resolver's own exception is never retained or echoed.
2. A different action identity is
   `authorization.policy_selection_scope_mismatch`; use before
   `effective_at` is `authorization.policy_selection_not_effective`.
3. In sorted required order, an `UNDETERMINED` entry is
   `authorization.policy_applicability_undetermined`, and an entry whose
   referenced facts are missing or not authoritative is
   `authorization.policy_selection_facts_unverified`. No fact is inferred.
4. An attributable `DOES_NOT_APPLY` entry stays in the inventory, is not
   evaluated and needs no installed policy. If nothing required applies,
   `authorization.policy_required_empty` blocks: a permissive baseline
   must be an explicit required policy, not an empty selection.
5. Every required `APPLIES` binding must be registered. A missing
   identifier is `authorization.policy_missing`, a wrong version is
   `authorization.policy_version_mismatch`, and other canonical binding
   differences are `authorization.policy_binding_mismatch`. The complete
   required set is checked before any policy runs.

Informative entries never grant, deny or determine the receipt. Their
missing implementations, unresolved applicability, unverified facts and
evaluation failures are recorded as `(policy_id, reason_code)` pairs in
binding order. A wrong-version informative policy is never executed. An
invalid selection structure still fails as structure. For a completed bound
evaluation, the informative results are kept in its decision record; see
[Decision records](decision_records.md#what-a-record-contains).

## Requirements are never compared

Each requirement is checked on its own; SETT never compares,
merges or ranks two of them.

- Every governing policy is evaluated, and any denial decides the attempt;
  no grant or approval outweighs it.
- Every required condition of every governing policy goes to its own
  verifier, even when two conditions share an identifier and a verifier. One
  that fails blocks the action; another that passed does not cover it.
- Each policy keeps its own origins, and each condition the requirements it
  covers, in the decision record.
- Policy versions are opaque and never ranked; no policy takes precedence
  over another by age, origin or layer.

**Why SETT compares no time windows.** Whether two requirements are
comparable, equivalent or ordered, and how their time windows relate, depends
on the instruments, their interpretation and the deployment, which a general
framework cannot know. SETT therefore builds no window value and intersects
nothing. An application that needs a window expresses it as a condition its
own verifier checks: freshly at every site, or as an attestation with the
expiry the verifier reports. A comparison relation inside SETT would need a
concrete requirement and its own decision.

## Following up reviews

`follow_up_reviews()` checks every declared review obligation
when the application asks, and records what its verifier reports.

```python
PolicyRuler.follow_up_reviews(self) -> ReviewFollowUp
```

It needs a governance journal and review verifiers. SETT runs no scheduler
and decides no deadline; see
[Policy reviews](policy_reviews.md#following-up-obligations).

## Inspection

`evaluate` runs the same engine as authorization, for inspection
only; its result is never usable as authority.

```python
PolicyRuler.evaluate(
    self,
    request: AuthorizationRequest,
    *,
    verified_approvals: frozenset[VerifiedApproval],
    checkpoint_id: str,
) -> PolicyEvaluation
```

- The approval values it receives are declared inputs, not authority it
  verified. It uses the same engine as the bound authorization invocation,
  and it registers nothing: no receiver can recognize its result.
  `verification_ref` is always `None` here.
- The request must be the exact `AuthorizationRequest` class and the
  approvals an exact `frozenset` of exact `VerifiedApproval` values; anything
  else is `policy.evaluation_input_invalid`. A checkpoint that is malformed or
  not supported by the configured integration is
  `policy.evaluation_checkpoint_invalid`.
- `PolicyEvaluation` has no public constructor, no deserialization and no
  replacement helper. `PolicyEvaluation(...)` raises `TypeError` with the
  fixed message `PolicyEvaluation instances are created by PolicyRuler.` Its
  properties are read-only, and its `repr` shows only the opaque evaluation
  identifier.
- The result is operational data, not a public-safe trace: selection facts and
  policy reason codes may carry deployment detail.

| Property | Meaning |
|---|---|
| `evaluation_id` | Fresh opaque identifier; not a capability |
| `ruler_id` | Declared ruler label |
| `action_id`, `action_type`, `trace_id` | Exact evaluated identity |
| `selection` | The immutable selection snapshot that governed the run |
| `checkpoint_id` | The exact evaluated checkpoint |
| `generation` | The observed policy-instance generation |
| `evaluated_at` | Aware UTC completion time |
| `valid_until` | Exclusive upper bound; never an assertion of current validity |
| `decision` | The composed `AuthorizationDecision`; not a receipt |
| `policy_decisions` | Required results in binding order |
| `informative_decisions` | Successful informative results, which grant nothing |
| `informative_diagnostics` | Informative identifier and reason-code pairs |
| `verification_ref` | Opaque reference to the bound invocation's state; `None` for inspection |

There is no `authorized`, `complete`, `trusted` or token field. Identical
property values do not make an inspection result usable as authority.

## Sharing an instance

Several authorization rulers can share one policy ruler, at the
cost of invalidating each other's pending bindings when a policy is
registered.

Several AuthorizationRuler instances may share one PolicyRuler, with the
same configured clock and evidence-sink objects. They then share the
registry and the generation, so a registration invalidates every pending
binding of that ruler, including bindings belonging to the other issuers.
That is stale-binding rejection, not a policy denial, and it is a cost of
sharing an instance. Their receiver identities, verifier generations,
invocations and receipt bindings remain disjoint; equal declared
identifier strings never merge them.

See [the handoff contract](policy_handoff.md),
[policy reason codes](policy_reason_codes.md) and
[policy selection](policy_selection.md).

## Governing observations before a blocking failure

When a bound attempt fails partway, the error can list the
governing policies already admitted, without running the rest.

- The initial bound evaluation records each governing policy's registered ID
  and version and its admitted verdict, after the result checks and before the
  `authorization.policy_checked` evidence write. The order of admission and of
  callbacks is the same.
- A later failure can therefore carry the admitted part without building a
  completed `PolicyEvaluation` or running the remaining callbacks. The tuple
  adds no limit on the selection and truncates nothing; the existing admission
  limits still apply.
- The tuple is exposed through
  [`SETTAuthorizationError.policy_observations`](api_reference.md#admitted-policy-observations-on-authorization-failure),
  with the coverage and privacy limits stated there. Public inspection records
  no observations.
- Informative results and diagnostics are not part of the error. A blocked
  attempt writes no decision record, so its informative results are not
  delivered; a new inspection does not recover them.
- Neither a `grant` row nor an error changed by the caller can supply
  authority. This is not persistent audit storage or complete institutional
  reasoning.

## Condition verification: scope and limitations

The scope and limits of condition verification are described once, in
[Condition verification](condition_verification.md#condition-verification-scope-and-limitations).
