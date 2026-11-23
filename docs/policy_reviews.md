# Policy review declarations and reported facts

A policy can declare review obligations on its decision: a notification after the action, a human review with a deadline, or a restriction on declared future actions. SETT carries the declarations and the facts an application reports; it runs no review.

Policy review declarations belong to `sett.policy_ruler`. `AuthorizationRuler` remains the final authority compositor. The values in this document declare and carry review information; they do not install a producer, perform a review, deliver a notification, calculate a deadline or govern an effect. The [review producer](#the-review-producer), installed by `PolicyRuler(review_verifiers=...)`, checks restrictions before an effect.

## Explicit policy declaration

Every decision states its review obligations explicitly, even when there are none.

`PolicyDecision` and its `grant`, `deny` and `require_approval` factories require `review_obligations`. An explicit empty sequence means that the decision declares no review obligation. Omitting the argument is a construction error; the framework does not infer an empty inventory for a legacy caller. Approval needed before the action still uses the approval requirement and its verifier.

Each `PolicyReviewObligation` has an identifier, complete `PolicyBinding`, mode, verifier identity and version, and exact action identity. `RESTRICT_DECLARED_FUTURE_ACTIONS` also declares a non-empty set of exact future action types. Other modes carry no future action types. The framework defines no action taxonomy and does not infer a relationship between action names.

## Reported facts

An application verifier reports typed facts about one obligation; a reported fact is not authority.

`ReviewVerificationRequest` supplies the declared obligation, action identity and opaque evidence reference to an application verifier. It does not supply a clock, timestamp, deadline, scheduler or full authorization request. `ReviewVerification` carries the obligation identifier, complete binding, action identity and distinct typed facts:

- `ReviewCompletedFact` reports a favorable or unfavorable review outcome.
- `NotificationDeliveredFact` reports a notification-delivery fact.
- `DeadlineStatusFact` reports the application's `NOT_OVERDUE` or `OVERDUE` determination.

The fact types are distinct. A value may be constructed without proving that the obligation belongs to the retained invocation inventory, that the configured verifier produced the fact, or that a required fact set is complete. The values do not implement that check; the [review producer](#the-review-producer) does: it establishes those relations, keeps missing, invalid or unverified required facts unsatisfied, and blocks only the dependent action, before its effect. A review fact never overrides an independent denial.

A notification is post-action and cannot rewrite or retroactively block an observed effect. A favorable review outcome is not authority. A restriction applies only to its declared future action types and is lifted only by a separately declared and verified event. Restart does not resume execution, complete a review, create authority or remove a restriction.

## The review producer

With review verifiers and a governance journal configured, a restriction that an earlier decision declared blocks later actions of its declared types until its verifier reports a favorable completed review.

```python
policy_ruler = PolicyRuler(
    ...,
    governance_journal=journal,             # required with review verifiers
    review_verifiers=[my_review_verifier],  # default None
)
```

- `review_verifiers` is a sequence of at most 256 verifiers, each with a callable `verify`, a `verifier_id` and a `verifier_version`; two verifiers with the same ID and version are invalid. A malformed value, or review verifiers without a governance journal, is `policy.ruler_configuration_invalid`. `None`, the default, installs nothing, and the `review` state stays without a producer.
- The producer reads the obligations from the [decision records](decision_records.md) in the journal. Each record keeps the review obligations its governing decisions declared.

What blocks before an effect:

- Only `RESTRICT_DECLARED_FUTURE_ACTIONS` blocks, and only a later action whose type is one of the declared future action types. An authorization is never blocked by the obligations it declared itself.
- That exemption covers only the decision record the authorization confirmed, which the policy ruler retains for it, at issue and at use. It never comes from the action identifier. A separately submitted action, or a new authorization of the same request, is a later action even with the same identifier, so a retry of an action whose policy restricts its own type is blocked until the review lifts the restriction.
- `NOTIFY_AFTER_ACTION` and `REQUIRE_HUMAN_REVIEW_WITH_DEADLINE` never block before an effect. An overdue review has no consequence by itself.
- For each earlier restriction that names the action's type, the producer calls the restriction's configured verifier with a `ReviewVerificationRequest` carrying the recorded obligation, the current action's identity, and the operation ID of the decision record that declared it as `evidence_ref`.
- The restriction is lifted only when the result reports a `ReviewCompletedFact` with a favorable outcome. An unfavorable outcome, a notification fact or a deadline status does not lift it.
- The verifiers are asked again at every collection: when the receipt is issued and when it is used. A restriction that is not lifted at use blocks the receipt with `authorization.review_restriction_active`. If the review state differs from the one the receipt was bound to, for example because a restriction recorded in between was lifted, the code is `authorization.policy_evidence_changed`.
- A lifted restriction never overrides an independent denial; the policies still decide.
- The restrictions the action cleared are recorded in the `review` field of its own decision record.

Each failure has its own code:

| Policy code | Authorization translation | Authorization class |
|---|---|---|
| `policy.review_restriction_active` | `authorization.review_restriction_active` | blocking authorization |
| `policy.review_verifier_missing` | `authorization.review_verifier_missing` | configuration |
| `policy.review_verifier_failed` | `authorization.review_verifier_failed` | configuration |
| `policy.review_result_invalid` | `authorization.review_result_invalid` | configuration |
| `policy.review_state_unavailable` | `authorization.review_state_unavailable` | configuration |

In the last column, "configuration" means the error is a `SETTAuthorizationConfigurationError`, and "blocking authorization" means a plain `SETTAuthorizationError`. At the policy boundary, all five codes are raised as `SETTPolicyConfigurationError`. See [Policy reason codes](policy_reason_codes.md#review-restriction-diagnostics).

The producer keeps no review lifecycle of its own. Apart from the restrictions an action cleared, recorded in its decision record, review facts are not stored; the restrictions themselves are read from the journal, so they survive a restart as long as the journal does, and nothing is resumed.

## Following up obligations

The application asks SETT, when it chooses, to check every declared obligation with its verifier; each newly verified fact is kept in the governance journal as evidence, never as authority.

```python
report = policy_ruler.follow_up_reviews()
for item in report.obligations:
    print(item.obligation_id, item.notified, item.completed, item.deadline, item.reason_code)
```

- `follow_up_reviews()` needs a governance journal and review verifiers; otherwise it raises `policy.ruler_configuration_invalid`.
- For each review obligation the decision records declare, in any mode, it asks the configured verifier once, with the recorded obligation, the declaring action's identity and the decision record's operation ID as `evidence_ref`. The result is checked as the review producer checks it.
- Each newly verified fact is written to the journal as a review record, schema `sett.governance.review` version 1, and confirmed at the journal's required durability before it is reported. A fact identical to one already recorded is not written again, also after a restart. If a record cannot be confirmed, the follow-up stops with `policy.review_record_unconfirmed`.
- A record can reach the journal while its durability is not confirmed, and is then read back like any other. Before it trusts the review records already in the journal, the follow-up confirms the journal's durability through the last of them; if that is not confirmed, it stops with `policy.review_record_unconfirmed` and writes nothing new.
- An obligation whose verifier is missing, fails or returns an invalid result is reported with `policy.review_verifier_missing`, `policy.review_verifier_failed` or `policy.review_result_invalid`, and the others are still followed up. An obligation whose binding is no longer registered is reported with `policy.review_state_unavailable`. An unreadable journal stops the follow-up with that same code.
- The report is a `ReviewFollowUp`: one `ObligationFollowUp` per obligation, with `notified`, `completed` (the outcome, or `None`), `deadline` (what the verifier reported, or `None`), `evidence_refs`, `reason_code` and `open`, plus the number of records written. `ReviewFollowUp`, `ObligationFollowUp`, `REVIEW_SCHEMA_ID` and `REVIEW_SCHEMA_VERSION` are imported from `sett.policy_ruler.review_follow_up`.

A recorded fact grants nothing; [governance recovery](governance_recovery.md) reports the records with each obligation. A recorded favorable review does not lift a restriction: the review producer asks the verifier again before every effect, so a verifier that stops reporting the review keeps the restriction active. An overdue report has no consequence by itself.

**Why SETT decides no deadline.** When an obligation is due, when it becomes overdue, and how long a request or an authorization lasts depend on the application's own rules, calendars and obligations, which a general framework cannot know. SETT therefore keeps no clock, timer or schedule for reviews: the application decides when to call the follow-up and reports a deadline status through its verifier, and SETT records what was verified. A policy that wants a consequence declares it, for example a restriction on future actions.

## Ownership and limits

The application owns reviewers, notifications, clocks and deadlines; SETT measures no time and keeps no review lifecycle.

The application owns reviewer authority, notification delivery, clock, calendar, event anchor, deadline calculation and overdue determination. The framework adds no timestamp, duration, clock, timer or scheduler to these values. The declared deadline-status fact reports an application determination; it is not a framework measurement.

The protocol and values provide no review lifecycle of their own, and no recovery or execution integration. The [governance journal](governance_journal.md) retains the decision records and the review records without interpreting them; the review producer reads only the obligations kept in decision records, and the review records are evidence that the follow-up writes. Without review verifiers the `review` verified state has no producer, and a governed attempt stops; see [Current limitation](authorization_reason_codes.md#current-limitation).
