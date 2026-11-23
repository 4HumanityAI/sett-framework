# Consent model

`templates/consent_model.py` is a conceptual model of how an
application can check that it holds a consent record before an action. It
is a starting point to modify or replace, not a framework requirement and not
a compliance tool.

## Whose consent this is

Consent belongs to the application built on SETT, not to the
framework.

SETT is a toolbox for building applications. Who the subject is, which
purposes and scopes exist, which legal basis applies and where consent
records are kept are decisions of the application. The model shows one
shape for those decisions so that an application does not start from a
blank page. Modifying it, or building your own, is the recommended way to
use it.

## How it works

A policy declares a consent check as a condition, and the
model's verifier looks for a current record that covers the request.

The model reuses SETT's condition verification; it adds no new mechanism.

1. A policy declares a required `PolicyCondition` whose verifier is the
   model's `ConsentVerifier`. `consent_condition()` builds one for a request.
2. When the condition is verified, `ConsentVerifier` reads the subject from
   the request's `principal`, the purpose from its `purpose` and the scopes
   from its `scope`, and looks in a `ConsentRegistry` for records of that
   subject and purpose that cover every requested scope.
3. It reports the result as a `ConditionVerification`:

| What it finds | State | Reason code |
|---|---|---|
| A current record | `SATISFIED`, with the record identifier as evidence | none |
| Matching records, none current (revoked, expired or not yet granted) | `UNSATISFIED` | `consent.not_current` |
| No matching record | `EVIDENCE_NOT_PRESENT` | `consent.not_found` |
| No usable subject or purpose in the request | `EVIDENCE_INVALID` | `consent.request_incomplete` |

A required condition that is not satisfied blocks the attempt, with the
usual authorization codes, for example
`authorization.condition_evidence_not_present`, and the failed condition is
reported in `SETTAuthorizationError.failed_condition`.

```python
registry = ConsentRegistry()
registry.record(ConsentRecord(
    record_id="consent.0001",
    subject="user-42",
    purpose="appointment-reminders",
    scopes=frozenset({"sms"}),
))

policy_ruler = PolicyRuler(
    ...,
    condition_verifiers=[ConsentVerifier(registry, clock)],
    implementation_owned_consent=True,
)


class ReminderPolicy:
    policy_id = "policy.reminders"

    def evaluate(self, request, verified_approvals):
        return PolicyDecision.grant(
            self.policy_id,
            conditions=(consent_condition(binding, request, checkpoint_id=entry),),
            review_obligations=(),
        )
```

The condition is declared with `RECHECK`, so the record is looked up again
before the receipt is issued and before it is used. With `ATTESTATION`, a
verified consent may be reused until the record expires or the declaration's
limit ends, and `confirm()` checks that its records are still current.

## What it implies

A record is what the application wrote down; the model checks
it exists and is current, nothing more.

- **Not proof of agreement.** SETT does not verify that a person actually
  agreed. A record says what the application recorded.
- **Not legal advice.** Nothing in the model decides what valid consent
  means in any jurisdiction.
- **In memory.** `ConsentRegistry` keeps records only while the process runs
  and has no access control. Replace it with your own storage.
- **One clock.** The verifier's clock must agree with the `PolicyRuler`
  clock. SETT rejects an observation outside the call window, but it cannot
  check the verifier's clock.
- **The consent state.** Authorization also requires a consent state,
  apart from this model. An implementation that owns consent declares it
  with `PolicyRuler(implementation_owned_consent=True)`, and SETT records
  that declaration; the model does not need to supply that state.
- **The other states.** A governed attempt reaches condition verification
  only when every state producer is configured, including a governance
  journal and review verifiers (see
  [Current limitation](authorization_reason_codes.md#current-limitation)).
  Without them, the model's check runs only where those states are supplied,
  as in the tests with synthetic states.

## Changing it

The parts marked TODO are the ones most applications replace.

- **Storage:** replace `ConsentRegistry` with your database or service.
- **Keys:** change `consent_key()` if consent is keyed by something other
  than principal, purpose and scope.
- **Record fields:** add what your legal basis and audit need, such as how
  the consent was collected or the version of the text agreed to.
- **Reuse:** choose `ATTESTATION` with a limit if a verified consent may be
  reused for a while.

The file lives in `templates/`, next to the other templates. It is not part
of the installed package, and `import sett` never exposes it.
`tests/test_consent_model_template.py` keeps it working against the
condition contract.
