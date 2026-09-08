# Authorization Ruler reason codes

These stable identifiers describe failures without carrying payloads, identity
values, provider messages, or free text into evidence.

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
| `authorization.evidence_failed` | Sanitized authorization evidence could not be recorded |

## Receipt state

| Reason code | Meaning |
|---|---|
| `authorization.receipt_store_failed` | The issuer-owned store could not answer safely |
| `authorization.receipt_duplicate` | A receipt identifier already exists |
| `authorization.receipt_unknown` | The issuer-owned store has no such receipt |
| `authorization.receipt_mismatch` | Public receipt fields differ from issuer-owned state |
| `authorization.request_mismatch` | The current request fingerprint differs from the authorized request |
| `authorization.receipt_expired` | The receipt lifetime ended before verification |
| `authorization.receipt_revoked` | The issuer revoked the receipt |
| `authorization.receipt_consumed` | A single-use receipt was already consumed |
| `authorization.receipt_invalid` | A grant did not carry a valid receipt |
| `authorization.denied` | Applicable policy composition denied the exact request |
| `authorization.approval_required` | Additional predeclared authority remains unsatisfied |
| `authorization.system_failure` | A failure without a more specific authorization reason was normalized at the execution boundary |

Policy-provided reason codes use consumer-owned namespaces. SETT records them as
bounded identifiers but does not assign their business meaning.
