# SETT terminology

What each SETT term means, why it has that name, and where its
boundary is.

These terms describe SETT's roles and its naming rules; they introduce no API.

| Term | What it designates | Naming reason and boundary |
|---|---|---|
| Agent | Owner of a domain, coordinating its experts and publishing results | A domain participant, not an interchangeable service client |
| Expert | One task or responsibility within an owning agent | Focused expertise; it does not own a separate agent domain |
| Ruler | A structural governance pillar or its defining boundary | It governs; it is not a synonym for an important agent or a singleton promise |
| Adapter | An implementation of an external capability interface | It adapts replaceable infrastructure, not domain authority |
| Orchestrator | The coordinator of registered agents and governed paths | Coordination does not grant access-control enforcement over arbitrary Python |
| Action | Data describing an effect submitted for governed execution | Representing an effect is distinct from performing it |
| PolicyDecision | The structured authorization-policy result, carrying its policy's explicit condition and review declarations | It keeps ethics and authority separate. An empty declaration states that there are none; an absent one is a construction error. A declaration is not a constraint, a verification or authority |
| PrivateMemory | An agent's private workspace by architecture and API convention | It is not a sandbox or a runtime identity-access boundary |
| UniversalMemory | The governed shared publication layer | Intended for published results; updates follow the configured filter |

Established names such as `EthicalFilter` keep their meaning and compatibility.
A more conventional synonym is not a reason to rename an element. Adding a
framework term, changing its meaning or renaming a public element requires an
explicit decision by the author.

## Policy metadata terms

The names that describe a policy and where its requirements come
from.

These values belong to the policy domain, and the authorization package
re-exports the same objects. The established terms above keep their meanings.

| Term | What it designates | Naming reason and boundary |
|---|---|---|
| PolicySourceReference | A versioned reference to an external instrument provision | It identifies provenance; it is neither an Adapter that retrieves content nor a Ruler that determines legal authority |
| PolicyOrigin | One requirement revision and declared layer with explicit source presence or absence | It preserves origin without establishing an Agent domain, evaluating a task or ranking jurisdictions |
| PolicyBinding | A policy identity and version associated with its contract and provenance | It binds metadata; it is distinct from a PolicyDecision result and does not grant authority or introduce another Ruler |

See [Policy identity and provenance](policy_identity.md) for the contract and
its limits.

## Selection value terms

The names that declare which policies apply to one action.

These values belong to the policy domain. They rename no existing role and add
no other authority mechanism.

| Term | What it designates | Naming reason and boundary |
|---|---|---|
| PolicyApplicabilityState | The three declared applicability states | Distinguishes application declarations from authorization verdicts and evaluated satisfaction |
| PolicyApplicability | One policy's declared applicability, justification and references | Records a declaration; it is not a Ruler deciding law or an Expert interpreting a source |
| PolicySelection | An exact-action inventory with its declared applicability and facts | Names the selected inventory, distinct from a PolicyBinding identity or PolicyDecision result |

Constructing these values grants no permission. `PolicyRuler` reads a declared
selection and checks it before any policy runs; see
[policy selection value contracts](policy_selection.md).

## PolicyRuler boundary terms

The names of the policy governance boundary and of what it
produces.

`PolicyRuler` designates the policy governance boundary, not an Agent, Expert,
value object or second authorization issuer. None of these names renames an
established Agent, Expert, Ruler or Adapter term.

| Term | What it designates | Naming reason and boundary |
|---|---|---|
| PolicyRuler | The owner of policy registration, selection, evaluation, composition and obligation state | A ruler governs; policy governance is its own boundary. Final authorization and receipts remain with AuthorizationRuler |
| PolicyEvaluation | One immutable producer-created observation of a completed evaluation | Names an observation, not a permit, a receipt or a verdict value. It cannot be constructed, transferred or submitted as authority |
| PolicySelectionResolver | The application-owned source of one exact-action selection | Names the resolution of a declaration, distinct from AuthorizationRequestResolver, which builds the request itself |

`AuthorizationRuler` requires an injected `PolicyRuler`. A governed attempt
completes only when every verified-state producer that the handoff
requires is configured; see
[Current limitation](authorization_reason_codes.md#current-limitation),
[PolicyRuler](policy_ruler.md), [the handoff contract](policy_handoff.md) and
[policy reason codes](policy_reason_codes.md).

## Condition declaration value terms

The names of a declared condition and of its validity mode.

These names do not rename a Ruler, Agent, Expert or Adapter.

| Term | What it designates | Naming reason and boundary |
|---|---|---|
| PolicyCondition | An exact-action declaration of a policy's required or informative check, with explicit provenance and named dependencies | A declaration to be checked, distinct from an AuthorizationConstraint describing a restriction already verified; not a Ruler or verification result |
| ConditionValidityMode | The explicit RECHECK or ATTESTATION mode of that declaration | Names declared validity handling, distinct from applicability, execution mode and proof of present validity |

A declaration verifies nothing by itself. When condition verifiers are
configured, the policy layer is set up to verify the required declarations
during authorization. A governed attempt reaches that point only when every
state producer is configured. See
[condition declarations](policy_conditions.md) for the complete value contract.

## Condition verification terms

The names of a condition observation and of the callback that
reports it.

These names do not rename a Ruler, Agent, Expert or Adapter.

| Term | What it designates | Naming reason and boundary |
|---|---|---|
| ConditionVerificationState | The five reported states of one condition observation | Names an observation state, distinct from PolicyApplicabilityState, ConditionValidityMode and an authorization verdict |
| ConditionVerification | One reported observation for a PolicyCondition, with its times, opaque evidence references and reasons | Names an observation, not a receipt, PolicyDecision, PolicyEvaluation or institutional conclusion; constructing it conveys no authority |
| ConditionVerifier | The application callback protocol that reports and confirms condition observations | Names an application callback, not an Expert, Agent, Adapter, Ruler or the private verified-state producer; distinct from the authorization ApprovalVerifier protocol |

A structurally valid result is not current evidence and grants no authority.
See [condition verification](condition_verification.md) for the complete value
and protocol contract, including its
[scope and limitations](condition_verification.md#condition-verification-scope-and-limitations),
and [policy reason codes](policy_reason_codes.md#condition-diagnostics) for the
condition diagnostics.

## Standalone governance journal terms

The names of the journal and of the values it reads and writes.

- **GovernanceRecord**: immutable operation identity, uninterpreted domain schema
  label/version and opaque payload bytes. Empty bytes do not declare a decision,
  review or absence of obligations.
- **GovernanceSubmission**: the exact original frame, record, write ID and expected
  head prepared for explicit submission; not authority.
- **GovernanceEntry**: admitted frame bytes and stream position, with separate
  integrity and non-interpretation labels.
- **GovernanceReadResult**: bounded observed prefix and completeness information.
  Completeness does not establish domain validity or durable recovery.
- **GovernanceWriteResult**: observed append/confirmation state, including explicit
  uncertainty; a failure does not claim rollback or absence of effect.
- **GovernanceReconciliation**: explicit lookup and optional flush of original
  bytes; never an append, renewed authority or effect retry.
- **GovernanceJournal**: the standalone domain envelope over the
  EvidenceStream, integrity and durability contracts.

A positive integrity check reports only the configured policy's check of bytes,
not writer authenticity. No transaction joins storage, journal, trace, authority
consumption and effect. The journal itself installs no producer; configured
on `PolicyRuler`, it receives the decision records of the durability
producer ([Decision records](decision_records.md)). See
[Governance journal](governance_journal.md) for signatures, bounds and limits.
These terms do not define decision or review schemas.

## Policy review terms

The names of a review obligation, of the facts reported about it,
and of the callback that reports them.

- **PolicyReviewObligation**: a policy-owned declaration of one review modality and its exact policy/action binding. It does not run review or grant authority.
- **ReviewVerificationRequest**: an application callback input identifying the declared obligation, action and opaque evidence reference. It does not contain a deadline or a framework clock.
- **ReviewVerification**: a bounded value carrying distinct typed facts for one obligation, binding and action. A value alone does not prove membership in the retained inventory or satisfy an obligation.
- **ReviewFact**: one reported review completion/outcome, notification delivery or application-determined deadline status. These kinds are not interchangeable.
- **ReviewVerifier**: the application callback protocol for reporting a review value. Declaring the protocol installs nothing; verifiers passed to `PolicyRuler(review_verifiers=...)` install the review producer.

The application owns reviewer authority, notification delivery, deadlines,
clock and calendar. The review producer checks restrictions on declared
future actions before an effect; there is no durable review lifecycle,
restart recovery or effect integration.
