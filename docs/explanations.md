# Explanations

`explain()` turns one decision record, or the error of a
blocked attempt, into two fixed views: one for the deployment's operators
and one for anyone else. The views carry codes and values, never sentences;
the application turns them into its own words.

## Building an explanation

One call builds the explanation; each view is one more call.

```python
from sett.policy_ruler.decision_record import read_decision_records
from sett.policy_ruler.explanation import PublicValues, explain

record = read_decision_records(journal).decisions[-1]
explanation = explain(record)
for_operators = explanation.operational_view()
for_the_public = explanation.public_view(
    PublicValues(policy_ids={"example.retention_policy"}),
    next_steps=["example.contact_support"],
)
```

A blocked attempt writes no decision record, so its error is the source:

```python
try:
    outcome = ruler.authorize(request)
except SETTAuthorizationError as error:
    for_the_public = explain(error).public_view()
```

`explain(source, *, recovered=None, ethics_verdict=None)` takes:

| Argument | Meaning |
|---|---|
| `source` | A `DecisionRecord`, for an evaluation that completed, or a `SETTAuthorizationError`, for an attempt that was blocked, for example by a failed required condition |
| `recovered` | The decision's `RecoveredDecision` from [governance recovery](governance_recovery.md), which adds the effect's state; only with a decision record, and only for that record |
| `ethics_verdict` | The application's `FilterVerdict`, if it has one, kept as a field of its own |

Both views take `next_steps`: identifiers the application declares, kept in
order. Without them the field is empty; SETT suggests none.

`Explanation`, `PublicValues` and `explain` are imported from
`sett.policy_ruler.explanation`.

## The operational view

Every recorded identity, for the deployment's own operators,
and still minimized.

| Field | Content |
|---|---|
| `view`, `source` | `operational`, and `decision_record` or `authorization_error` |
| `reference` | The attempt identifier: a random value SETT generated, which carries nothing from the request |
| `action_type`, `verdict` | From a decision record: the action type and the aggregate verdict |
| `reason_code`, `original_reason_code` | From an error: why the attempt stopped, and the failure being reported when its report could not be written |
| `policies` | For each policy: ID, version, its own verdict, its reason codes, and its `origins` and `profile` |
| `policy_rows_may_be_incomplete` | True for an error: its policy rows may not cover every policy, because the failure details carry no completeness flag |
| `conditions` | The conditions a decision record holds, by identity, state and `requirements` |
| `failed_required_condition`, `attempted_conditions` | From an error: the required condition on which verification stopped, and the conditions whose verifier was called |
| `pending_approvals` | The requirement IDs of an approval requirement |
| `review_obligations` | Each declared obligation: ID, policy ID and version, and mode |
| `restrictions_cleared` | Each earlier restriction the action cleared: obligation ID, the operation ID of the record that declared it, and the outcome |
| `informative` | From a decision record of version 4: each informative policy's ID, version, verdict, reason codes and SETT's diagnostic codes; they decide nothing |
| `recovery_outcome`, `effect_end` | With `recovered`: what the evidence vouches for, and how a known effect ended |
| `ethics_verdict` | The ethics verdict, if given |
| `next_steps` | As declared |

Origins, profiles and requirements come from decision records of version 3,
and informative results from version 4; for an earlier record or an error
they are `None`, meaning not recorded. Each
origin gives its layer ID, requirement ID and revision, and either its source
or the reason the source is absent.

It never holds the request payload, the principal, the request fingerprint,
an evidence reference, an approval authority's identity or an exception
message. Evidence references stay in the record.

## The public view

SETT's own vocabulary, and the application's values only when
the application approves them.

The public view has the same fields, with these differences:

- SETT's own reason codes and enumerations appear: verdicts, condition
  states, review modes, recovery outcomes, effect ends and ethics verdicts.
  They are a fixed vocabulary without application data. A value outside it,
  such as a code raised by an application's own component, is treated as
  the application's.
- A value the application or its policies supplied appears only when the
  application approves it in the call, through `PublicValues`: policy IDs,
  reason codes, action types, condition IDs, obligation IDs, and the layer,
  requirement and source IDs of origins. A policy,
  condition or obligation whose ID is not approved is left out; a failed
  required condition stays visible as failed, and its ID is `None` unless
  approved.
- Rows carry only the approved ID with its verdict, state or mode, and a
  policy's approved reason codes and origins. Policy versions and attempted
  conditions are left out, and pending approvals and restrictions cleared
  are counts.
- Each origin of a policy row shown appears as its approved layer ID,
  requirement ID and source ID, with `source_declared`, true or false.
  Revisions, source versions, locators and absence reasons never appear,
  and neither do profiles or condition requirements.
- An informative row appears only when its policy ID is approved, with its
  verdict, its approved reason codes and SETT's diagnostic codes, without
  its version.
- The reference appears only in the form SETT generates.
- `withheld` counts the values the view left out. A row left out counts
  once.

`PublicValues(policy_ids=..., reason_codes=..., action_types=...,
condition_ids=..., obligation_ids=..., layer_ids=..., requirement_ids=...,
source_ids=...)` takes collections of identifiers. It
approves values, not fields: an approved value is shown wherever it appears.

## What SETT leaves to the application

The words, the audiences and what a reader should do next.

**Why SETT writes no sentence.** What a reader is told, in which words and
language, which identifiers a deployment may show to the public, and what a
reader should do next depend on the application, its audiences and its
obligations, which a general framework cannot know. SETT therefore provides
the mechanism: views built from the record, closed field lists, withholding
by default and checked values. The application maps the codes to its own
text, approves what may be public and declares the next steps.

Who may see which view is the application's to control; an explanation does
no access control.

## What an explanation never says

No joint verdict, no score and no claim beyond the evidence.

- The authorization verdict, the effect's state and the ethics verdict stay
  separate fields. Nothing combines them into one judgement or aggregates
  them into a score or a percentage.
- A grant whose effect is uncertain after a restart says `grant` and
  `effect_uncertain`; nothing says it was done.
- An explanation never states that an external requirement is satisfied or
  that data was deleted.
- It is a value to read. It grants nothing, and presenting its reference
  grants nothing.

## Not covered

- Translations and wording for readers.
- Deciding which audience may see what, and access control to the views.
- Deletion and retention policies.
- Reports on external frameworks or requirements.
