# Governance recovery

After a restart, `read_governance_recovery()` reads the
governance journal and the restored trace together and reports what the
durable evidence can vouch for about each decision. It restores nothing:
continuing is always a new submission.

## Reading the evidence

One read-only call, with the journal and, if you have it, the
restored trace.

```python
from sett.audit_ruler.trace import TraceRecorder
from sett.policy_ruler.governance_recovery import read_governance_recovery

trace = TraceRecorder.restore(
    evidence_stream=trace_stream,
    namespace_id=namespace_id,
    evidence_durability=evidence_durability,
)
recovery = read_governance_recovery(journal, trace=trace)
for decision in recovery.decisions:
    print(decision.action_id, decision.outcome, decision.effect)
```

`read_governance_recovery(journal, *, trace=None)` takes a
`GovernanceJournal` and, optionally, the `RecoveryReport` of
`TraceRecorder.restore()`. It returns a `GovernanceRecovery`:

| Field | Meaning |
|---|---|
| `decisions` | One `RecoveredDecision` per decision record, with its outcome |
| `obligations` | One `RecoveredObligation` per declared review obligation |
| `complete` | Whether the journal read was complete |
| `trace_given` | Whether a restored trace was given |
| `diagnostics` | Unreadable records, the journal's read reason, a trace whose chain integrity failed, or a given trace that cannot establish a granted decision's use (`trace_coverage_unestablished`) |
| `requires_reconciliation` | True when the read was incomplete, something was unreadable, or an uncertain effect is not documented |

`GovernanceRecovery`, `RecoveredDecision`, `RecoveredObligation`,
`ReviewRecord`, `RecoveredOutcome` and `read_governance_recovery` are
imported from `sett.policy_ruler.governance_recovery`.

## What each decision reports

A denial, an approval requirement, a known effect, an uncertain one, or a
grant whose use the given evidence cannot establish.

| `outcome` | Meaning |
|---|---|
| `denied` | A policy denied the action; nothing was authorized |
| `approval_required` | The attempt ended needing approvals; it holds no authority |
| `granted_not_used` | Retained as an enum value for compatibility; this reader does not infer non-use from missing events |
| `effect_known` | The effect ended, and `effect` says how: `completed`, `failed`, `cancelled`, `timed_out` or `blocked` |
| `effect_uncertain` | The effect may or may not have happened; `boundary` is the trace's open boundary, with its dispositions |
| `granted_trace_unknown` | A grant without sufficient intact trace evidence to establish its use or effect |

The decision is joined to the trace by `decision_ref` on
`authorization.receipt_verified`, and from there by the causal links the
executor records: `handler.authorized`, then `handler.started` or
`handler.blocked`, then the handler's terminal event.

A late entry rejection records `handler.entry_blocked` as a child of
`handler.started`. Recovery reports a known blocked effect in either case.

A missing event cannot establish non-use. Even an intact prefix of the
decision's own trace can stop before the receipt is used or the effect is
recorded. Such a grant is reported as `granted_trace_unknown`; when a trace
was supplied, `trace_coverage_unestablished` requests reconciliation.
Positive observations require an intact chain and matching trace and
authorization-attempt identities. A failed chain cannot establish a known
effect. No unknown outcome authorizes repetition.

Each `RecoveredObligation` carries the obligation's identity and mode, its
`review_records` written by the [review follow-up](policy_reviews.md#following-up-obligations),
and, for a restriction, `cleared_by`: the decision records of the actions
that cleared it.

## What it does not do

Nothing is restored, resumed or retried.

- No receipt becomes usable. The bindings of a receipt live only in the
  process that issued it, so a receipt from before a restart is refused with
  `authorization.policy_binding_unavailable`.
- No consumption, review or effect is resumed, and nothing is retried.
- A restriction still applies after a restart until its verifier reports a
  favorable review again; elapsed time, a recorded review or a restart lifts
  nothing.
- Continuing an action is a new submission: a new evaluation, a new decision
  record and a new receipt.
- An uncertain effect is documented with
  `sett.audit_ruler.recovery.record_boundary_disposition()`. A disposition
  documents the boundary without closing it, so the effect stays uncertain
  and no repetition is authorized; once documented, it no longer counts as
  requiring reconciliation.

## Why there is no pending request to restore

An approval-required outcome ends its attempt and keeps nothing alive, and
SETT gives it no expiry. How long a request, an approval or an authorization
stays valid depends on the application's own rules, calendars and
obligations, which a general framework cannot know. The report therefore
names approval-required decisions with the time they were recorded, and the
application decides whether and when to submit again with the approvals.

## Where a stop leaves the evidence

What each stopping point looks like when read after a restart.

| The process stopped | Reported as |
|---|---|
| After the decision record, before the receipt | `granted_trace_unknown`; absence of a use event does not prove non-use |
| After the receipt, before its use | `granted_trace_unknown`; the old receipt is refused after restart |
| After the use, before the effect barrier reached the trace | `granted_trace_unknown`; the reader cannot distinguish this stop from an older prefix |
| After the barrier, before the handler started | `effect_uncertain` |
| While the handler ran | `effect_uncertain` |
| After the handler, before its completion reached the trace | `effect_uncertain` |
| Before a review record was confirmed | The restriction still applies; the record may be read back or not |

## Limits

- Only durable evidence counts. A trace kept only in memory leaves nothing to
  restore after a restart; without a trace, grants are reported as
  `granted_trace_unknown`.
- A record whose write outcome was uncertain can still be read back, for a
  decision record as for a review record. It grants nothing either way.
- The report reflects one read of each store: the decision records and the
  review records come from the same journal read, and `complete` covers
  both. Nothing joins the journal and the trace in one transaction.
- The reader cannot distinguish an old intact prefix from the latest stream
  solely from its trace identifier. It reports observations present in the
  admitted evidence and leaves missing observations unknown.
