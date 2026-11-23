# Decision records

With a governance journal configured, every completed policy
evaluation leaves a durable decision record, and no receipt exists before its
record is confirmed. The records can be read back as values; they never
restore authority.

## How it is configured

One keyword on `PolicyRuler` installs the producer of the
durability state.

```python
policy_ruler = PolicyRuler(
    ...,
    governance_journal=journal,  # a GovernanceJournal; default None
)
```

- With a `GovernanceJournal`, the ruler installs the producer of the
  `durability` state slot.
- With `None`, the default, nothing is installed and the durability slot
  stays without a producer, as before.
- Any other value is `policy.ruler_configuration_invalid`. The choice is fixed
  for the ruler's life, and a second durability producer cannot be added.

The journal must be dedicated to these records and configured with the
durability the deployment requires; see
[Governance journal](governance_journal.md).

## What happens in an attempt

The record is written and confirmed while the evaluation
collects its state; a receipt can only follow.

1. The bound evaluation runs the policies and collects the verified state in
   its fixed order: approvals, conditions, consent, review, durability.
2. For durability, the producer writes one decision record to the journal
   and waits for the journal to confirm it at its required durability.
3. Only a confirmed record produces the durability slot. Its evidence is the
   record's write ID, and its value names the record's position and the
   durability achieved.
4. Before the receipt is issued and before it is used, the same retained slot
   is checked again; nothing more is written.

Every completed evaluation is recorded: a grant, a denial and an approval
requirement alike. When the record cannot be confirmed (the journal cannot be
read, the write is uncertain, or the achieved durability is lower than
required), the attempt blocks with `authorization.decision_record_unconfirmed`
(from `policy.decision_record_unconfirmed`) and no receipt is issued.

Writes through one producer are serialized, so concurrent attempts in one
process do not see each other as a stale journal head. A journal shared with
another writer can make an attempt block; it never makes it pass.

## What a record contains

Settled facts of the decision, without the request payload.

The record is canonical JSON with schema `sett.governance.decision`, version
4. Its operation ID is `decision.` followed by the attempt identifier.

| Field | Content |
|---|---|
| `authorization_attempt_id` | The attempt identifier |
| `action_id`, `action_type`, `trace_id`, `checkpoint_id` | The request and the checkpoint |
| `selection` | Selection ID and revision |
| `policies` | For each governing policy: ID, version, verdict, reason codes, and the `origins` and `profile` its binding declared |
| `decision` | Aggregate verdict, policy IDs, reason codes, receipt use, requirement IDs and constraint IDs |
| `conditions` | For each condition the condition producer verified: policy ID, version, condition ID, state, and the `requirements` it declared, each as a layer ID and a requirement ID |
| `approvals` | Requirement and verifier IDs of the verified approvals |
| `consent` | `owned_by_implementation`, or the producer that supplied consent |
| `request_fingerprint`, `fingerprint_policy_id` | SETT's StrictJSON request fingerprint and its policy ID |
| `recorded_at` | When the record was produced |
| `review_obligations` | Each review obligation the governing decisions declared: obligation ID, policy ID and version, binding, mode, verifier ID and version, action ID and type, and future action types |
| `review` | Each earlier restriction this action had to clear: the operation ID of the record that declared it, the obligation ID, the outcome and the evidence reference its verifier reported |
| `informative` | Each informative policy evaluated or diagnosed in the attempt: policy ID and version, the verdict and reason codes it returned, if any, and SETT's diagnostic codes for it |

- The record holds no request payload, no principal and no approval authority
  identity. The fingerprint binds it to the exact request without them.
- The fingerprint equals the receipt's `request_fingerprint` when the issuer
  uses the default StrictJSON fingerprint policy; with another policy, they
  differ, and the record names which policy it used.
- Version 2 adds `review_obligations` and `review`. The review producer reads
  the restrictions from `review_obligations`; see
  [Policy reviews](policy_reviews.md#the-review-producer).
- Version 3 adds each policy's declared provenance and each condition's
  requirements:
  - `origins` lists every origin of the policy's binding as declared: layer
    ID, requirement ID and revision, and either the source (ID, version and
    locator) or the reason it is absent. `profile` is the binding's profile
    ID and revision, or `None`. They are copied when the policy's result is
    admitted, so a later change of registration does not change the record.
  - Only the declared provenance is added: the row already names the policy
    and its version, and the contract version is fixed.
  - A condition's `requirements` name the requirements its declaration
    covers; their revisions and sources are in its policy's row.
  - Origins are declared metadata. SETT records them as declared and
    verifies nothing about them; an absent source stays an absence.
- Version 4 adds `informative`, the results of the attempt's informative
  policies, in the selection's order:
  - A row holds the policy ID and version; the verdict and reason codes the
    policy returned, or `None` and an empty list when it returned none; and
    SETT's diagnostic codes for it, such as
    `authorization.policy_applicability_undetermined`,
    `authorization.policy_missing` or `authorization.policy_failed`.
  - An informative entry that does not apply was neither evaluated nor
    diagnosed, and leaves no row.
  - Informative results never decide anything: they change no verdict,
    receipt or state, and no policy runs again to fill them. They are
    written with the record, so they add no new way for an attempt to fail.
  - A blocked attempt writes no record, so its informative results are not
    delivered.
- Records of versions 1 to 3, written by earlier development builds, stay
  readable. Version 1 review fields read as empty; versions 1 and 2 hold no
  origins or requirements; none of them holds informative results.
- Origins and informative rows count toward a record's size. A record larger
  than the journal's `max_frame_bytes` is not confirmed, and the attempt
  blocks with `authorization.decision_record_unconfirmed`. SETT sets no limit
  of its own: size the journal for the deployment's bindings and selections.

## Order instead of a transaction

A receipt never exists without its record; the other stores
stay separate.

- The record is confirmed before the receipt is issued, so a receipt without
  its decision record cannot occur.
- If the process stops after the record and before the receipt, a durable
  decision remains with no receipt: it carries no authority.
- Nothing joins the journal, the receipt store, the trace and the external
  effect in one transaction. Recording the receipt's issue and use, and the
  effect handoff, is outside these records.
- When `SETTExecutor` consumes the receipt, its trace event
  `authorization.receipt_verified` carries `decision_ref`, the attempt
  identifier in the record's operation ID, so the trace and the journal can
  be read together.

## Reading the records back

`read_decision_records()` returns the decisions as values, with
what the read can vouch for.

```python
from sett.policy_ruler.decision_record import read_decision_records

recovered = read_decision_records(journal)
for record in recovered.decisions:
    print(record.action_id, record.decision["verdict"])
```

It returns a `DecisionRecovery`:

| Field | Meaning |
|---|---|
| `decisions` | The `DecisionRecord` values read from the journal's stable prefix |
| `complete` | Whether the journal read was complete |
| `last_position` | The journal head the read observed |
| `other_records` | How many records of other schemas were found, such as review records; they are not interpreted |
| `diagnostics` | Unreadable decision records and the journal's read reason, if any |
| `requires_reconciliation` | True when the read was incomplete or a record could not be decoded |

- A record says that an evaluation completed with that outcome. It does not
  say that a receipt was issued or used, or that an effect happened.
- Reading restores no receipt, consumption, review or effect, and resumes
  nothing. A record written before a durability failure can still appear,
  for an attempt that was blocked.
- To read the records together with the restored trace, see
  [Governance recovery](governance_recovery.md).
- To show a record to operators or to the public, see
  [Explanations](explanations.md).
- `DecisionRecord`, `DecisionRecovery`, `read_decision_records`,
  `DECISION_SCHEMA_ID` and `DECISION_SCHEMA_VERSION` are imported from
  `sett.policy_ruler.decision_record`.

## When records are written

A governed attempt collects its state only when every state producer is
configured (see
[Current limitation](authorization_reason_codes.md#current-limitation)).
Without the condition, consent and review producers, it stops before its
state is collected, and no record is written.
