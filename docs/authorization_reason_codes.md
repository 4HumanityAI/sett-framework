# Authorization Ruler reason codes

These stable identifiers describe failures without carrying payloads, identity
values, provider messages, or free text into evidence.

## Current limitation

Authorization issues a receipt only when every verified state it requires
has a producer, and each producer comes only from explicit configuration on
`PolicyRuler`: conditions from condition verifiers (`condition_verifiers=`,
which may be empty), consent from the implementation's declaration
(`implementation_owned_consent=True`; SETT records that declaration and
verifies no consent), durability from a decision record written to a
governance journal (`governance_journal=`), and review from the
application's review verifiers (`review_verifiers=`, which need that
journal). Without all four, every `authorize()` call that reaches state
verification fails with `authorization.policy_evaluation_incomplete`, even
when every policy would grant, and no receipt is issued, so handlers that
require an authorization receipt cannot run. This is deliberate: the
framework blocks rather than act on unverified state.

If a policy denied the action during such an attempt, the denial is visible
only in `SETTAuthorizationError.policy_observations`, with its reason codes
in `policy_observation_reason_codes`. Attempts that fail earlier keep their
own reason codes, for example when the request is invalid, an approval
cannot be verified or a policy fails.

## Configuration and policy

| Reason code | Meaning |
|---|---|
| `authorization.policy_missing` | No policy is registered for the exact action type |
| `authorization.policy_invalid` | The supplied policy does not implement the contract |
| `authorization.policy_duplicate` | A policy identifier is already registered for that action type |
| `authorization.policy_failed` | A policy raised while evaluating |
| `authorization.policy_result_invalid` | A policy returned an unknown result type |
| `authorization.policy_identity_mismatch` | A result claims a different policy identifier |
| `authorization.provenance_not_authoritative` | A populated authority field is proposed or unknown |
| `authorization.constraint_duplicate` | Two policy results reuse one constraint identifier |
| `authorization.approval_requirement_conflict` | One requirement identifier names incompatible verifiers |
| `authorization.not_configured` | Approval inputs were supplied to an executor without Authorization Ruler |
| `authorization.resolver_missing` | Configured authorization has no application request resolver |
| `authorization.request_resolution_invalid` | The resolver returned something other than an AuthorizationRequest |
| `authorization.request_resolution_failed` | The application resolver raised before producing a request |

## Selection value validation

These codes come from the constructors of the selection values when a value is
invalid.

| Reason code | Meaning |
|---|---|
| `authorization.policy_applicability_invalid` | An applicability value has invalid fields, references or canonical bounds |
| `authorization.policy_selection_invalid` | A selection has invalid fields, malformed contract version, inconsistent inventory/facts or aggregate bounds |
| `authorization.policy_contract_unsupported` | A structurally valid contract-version token is unsupported; also used by existing policy bindings |

All three raise `SETTAuthorizationConfigurationError`. A malformed version and
an unsupported one have different codes. The metadata constructors keep their
own errors; see [policy identity](policy_identity.md).

## Approval verification

| Reason code | Meaning |
|---|---|
| `authorization.verifier_missing` | Proposed evidence names no registered verifier |
| `authorization.verifier_invalid` | The verifier does not implement the contract |
| `authorization.verifier_duplicate` | A verifier identifier is already registered |
| `authorization.verifier_failed` | A verifier raised while checking evidence |
| `authorization.verifier_result_invalid` | A verifier returned an unknown result type |
| `authorization.verifier_identity_mismatch` | Verified authority does not match its declared requirement or verifier |
| `authorization.approval_conflict` | One requirement and verifier pair produced conflicting authorities |

## Fingerprint, time, and evidence

| Reason code | Meaning |
|---|---|
| `authorization.fingerprint_failed` | The injected fingerprint policy could not bind the request |
| `authorization.clock_invalid` | The clock did not return a timezone-aware datetime |
| `authorization.evidence_failed` | Sanitized authorization evidence could not be recorded. When the failed write was reporting another failure, `original_reason_code` holds that failure's code |
| `authorization.decision_record_unconfirmed` | The decision record of the evaluation could not be written and confirmed at the governance journal's required durability; no receipt is issued. See [Decision records](decision_records.md) |

## Receipt state

| Reason code | Meaning |
|---|---|
| `authorization.receipt_store_failed` | The issuer-owned store could not answer safely |
| `authorization.receipt_duplicate` | A receipt identifier already exists |
| `authorization.receipt_unknown` | The issuer-owned store has no such receipt |
| `authorization.receipt_mismatch` | Public receipt fields differ from issuer-owned state |
| `authorization.request_mismatch` | The current request fingerprint differs from the authorized request |
| `authorization.receipt_expired` | The receipt lifetime ended before verification, or before the handler ran |
| `authorization.receipt_revoked` | The issuer revoked the receipt |
| `authorization.receipt_consumed` | A single-use receipt was already consumed |
| `authorization.receipt_invalid` | A grant did not carry a valid receipt |
| `authorization.denied` | Applicable policy composition denied the exact request |
| `authorization.approval_required` | Additional predeclared authority remains unsatisfied |
| `authorization.system_failure` | A failure without a more specific authorization reason was normalized at the execution boundary |

Policy-provided reason codes use consumer-owned namespaces. SETT records them as
bounded identifiers but does not assign their business meaning.

## Policy domain diagnostics

Moving the policy values and the policy callback protocol into the policy
layer kept every existing diagnostic and error class.

The policy layer adds two families of codes, both listed in full in
[policy reason codes](policy_reason_codes.md):

- `policy.*` codes, raised by the policy layer itself as
  `SETTPolicyConfigurationError`. That class inherits `SETTConfigurationError`
  on purpose, so catching an authorization error never catches it.
- `authorization.policy_*` codes, raised here when a policy failure reaches
  authorization.

Selection and registration diagnostics keep their classes: configuration
failures raise `SETTAuthorizationConfigurationError`, and runtime selection
failures raise `SETTAuthorizationError`. The
[handoff contract](policy_handoff.md) describes the guarantees that hold
today, and the [current limitation](#current-limitation) says what a
receipt requires.


## Condition verification: scope and limitations

The scope and limits of condition verification are described once, in
[Condition verification](condition_verification.md#condition-verification-scope-and-limitations).


### Condition diagnostics

| Policy code | Authorization translation | Authorization class |
|---|---|---|
| `policy.condition_verifier_missing` | `authorization.condition_verifier_missing` | configuration |
| `policy.condition_verifier_failed` | `authorization.condition_verifier_failed` | configuration |
| `policy.condition_result_invalid` | `authorization.condition_result_invalid` | configuration |
| `policy.condition_unsatisfied` | `authorization.condition_unsatisfied` | blocking authorization |
| `policy.condition_evidence_not_present` | `authorization.condition_evidence_not_present` | blocking authorization |
| `policy.condition_evidence_invalid` | `authorization.condition_evidence_invalid` | blocking authorization |
| `policy.condition_evidence_unavailable` | `authorization.condition_evidence_unavailable` | blocking authorization |

In the policy layer, all seven raise `SETTPolicyConfigurationError`. Whether a
verifier is missing is checked before any verifier is called. A result of the
wrong type, for a different declaration, or with a check time or validity
window that does not fit, is `condition_result_invalid`. Missing, invalid and
unavailable evidence stay distinct from an unsatisfied condition, and a
structural failure is never reported as missing evidence.

An invalid condition inventory, `policy.condition_inventory_invalid`, maps to
`authorization.policy_result_invalid`; an invalid declaration raised inside a
policy callback is handled as a callback failure. Structural state failures
report incomplete state. If verified state no longer matches when it is
confirmed before a receipt is issued or used, the code is
`authorization.policy_evidence_changed`; an evaluation used at or after its
expiry time reports `authorization.policy_evaluation_expired`. The final gate
before a governed handler reports a stale grant with
`authorization.policy_generation_changed`, `authorization.clock_invalid`,
`authorization.policy_evaluation_expired` or `authorization.receipt_expired`;
see the [API reference](api_reference.md#settexecutor). No existing code
changes meaning.

Callback exceptions are sanitized. If `authorize()` is called while the caller
is handling another exception, the authorization error keeps that exception as
its context; value-construction errors carry none.

### Review restriction diagnostics

| Policy code | Authorization translation | Authorization class |
|---|---|---|
| `policy.review_restriction_active` | `authorization.review_restriction_active` | blocking authorization |
| `policy.review_verifier_missing` | `authorization.review_verifier_missing` | configuration |
| `policy.review_verifier_failed` | `authorization.review_verifier_failed` | configuration |
| `policy.review_result_invalid` | `authorization.review_result_invalid` | configuration |
| `policy.review_state_unavailable` | `authorization.review_state_unavailable` | configuration |

In the policy layer, all five raise `SETTPolicyConfigurationError`. A missing
verifier is checked before any verifier is called. A restriction that is lifted
at issue but not at use blocks the receipt with
`authorization.review_restriction_active`; a review state that differs from the
one the receipt was bound to is `authorization.policy_evidence_changed`. See
[Policy reviews](policy_reviews.md#the-review-producer).

### Controlled checkpoint diagnostics

| Reason code | Meaning |
|---|---|
| `authorization.checkpoint_invalid` | The checkpoint is not declared for the action type, is out of order, or was already passed |
| `authorization.checkpoint_blocked` | An earlier checkpoint of the same handler failed; every later call fails too |
| `authorization.checkpoint_unavailable` | No governed controlled handler is running for this context |

All three raise `SETTAuthorizationError`. A checkpoint can also fail with a
condition code or `authorization.policy_generation_changed`. See
[Controlled checkpoints](controlled_checkpoints.md).


### Policy observations are distinct from the blocking diagnostic

`SETTAuthorizationError.policy_observations` can show which governing policies
had already returned a result in the same call, as rows of policy ID, version
and verdict. It adds no reason code and changes nothing else about the error:
its class, message, reason code, verdict and receipt behavior stay the same,
including when an evidence-write failure replaces another failure report. A
denial seen in these rows and a blocked authorization are different facts, and
a grant seen in them cannot lift a block. The rows may cover only the policies
evaluated before the failure.

What `None`, an empty tuple, coverage and privacy mean exactly is in
[the API reference](api_reference.md#admitted-policy-observations-on-authorization-failure).
This field is not a per-condition explanation, a record of institutional
compliance, a delivery of informational diagnostics or a durable audit
history.

The reason codes of each observed policy, the condition on which
verification stopped, the attempt identifier and the original code of a
failure report that could not be written are separate fields; see
[authorization failure details](authorization_failure_details.md).
