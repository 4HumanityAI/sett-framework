# Controlled checkpoints

A controlled handler can pass declared checkpoints between the
parts of its work. At each one, SETT verifies the conditions declared for it
and checks again those that must be checked every time. A failed checkpoint
stops the next part and undoes nothing that already ran.

## How they are declared

`PolicyRuler` declares the checkpoints of each action type, and
a condition names the one where it is verified.

```python
policy_ruler = PolicyRuler(
    ...,
    condition_verifiers=[funds_verifier],
    controlled_checkpoints={"payment.transfer": ("checkpoint.before_credit",)},
)
```

- `controlled_checkpoints` maps an action type to its ordered checkpoint IDs.
  Each ID is a valid identifier, distinct within its action type and
  different from the entry checkpoint; at most 16 per action type and 256
  action types. The value is fixed for the ruler's life. Anything else is
  `policy.ruler_configuration_invalid`, and `None`, the default, declares
  none.
- A `PolicyCondition` names, in `checkpoint_id`, either the entry checkpoint
  or one of its action type's controlled checkpoints. Any other checkpoint is
  a declaration mismatch: a required policy fails with
  `authorization.policy_result_invalid`.
- At entry, only the conditions of the entry checkpoint are verified. Every
  declared condition must still have its verifier configured, so a missing
  verifier for a later condition blocks before any effect, with
  `authorization.condition_verifier_missing`.
- With authorization configured, `register_handler()` refuses an action type
  that declares checkpoints, with `SETTConfigurationError`: a plain callback
  cannot pass them. Use `register_controlled_handler()`.

## Passing a checkpoint

The handler calls `check_policy_conditions()` on its
`HandlerContext` before each later part of its work.

```python
def transfer(payload, context):
    debit(payload)
    context.check_policy_conditions("checkpoint.before_credit")
    credit(payload)
```

`HandlerContext.check_policy_conditions(checkpoint_id)` returns `None` when
the checkpoint passes and raises otherwise. It works only inside the
controlled handler of a governed submission. It checks, in order:

1. The checkpoint is the next one declared for the action type.
2. The policy generation and the verifier generation are the ones the
   receipt was issued under.
3. The conditions declared for this checkpoint pass their verifiers.
4. Every `RECHECK` condition already verified passes again. An `ATTESTATION`
   condition already verified must still be within its window; outside it,
   the result is `authorization.condition_result_invalid`.

It does not run the policies, the selection, approvals, review or
durability again, and the evaluation lifetime, which bounds the time from
issue to consumption, does not apply. A changed context needs a new
submission.

## When a checkpoint fails

The call raises, what already ran stays done, and the attempt
is reported as partial and never retried.

The call raises `SETTAuthorizationError`, or its subclass
`SETTAuthorizationConfigurationError` for the configuration codes, as at
entry:

- a condition code, with `failed_condition` and `attempted_conditions` set as
  at entry;
- `authorization.policy_generation_changed` when the configuration changed;
- or one of these:

| Reason code | Meaning |
|---|---|
| `authorization.checkpoint_invalid` | The checkpoint is not declared for the action type, is out of order, or was already passed |
| `authorization.checkpoint_blocked` | An earlier checkpoint of the same handler failed; every later call fails too |
| `authorization.checkpoint_unavailable` | No governed controlled handler is running for this context |

`submit_controlled()` reports the attempt as `ExecutionStatus.PARTIAL`, with
the error and its reason code, and never retries it, whatever the retry
policy says. A handler that catches the error and returns keeps its result;
the trace still records the failed checkpoint.

## Checkpoints a handler skips

SETT records them; it cannot stop a callback that ignores them.

A handler that returns without passing every declared checkpoint keeps its
result, and the trace records `handler.checkpoint_missed` with the missing
IDs in `missing_checkpoints`. SETT cannot force a callback to stop or observe
effects that a callback hides.

## Trace records

Each checkpoint leaves a durable trace record; nothing is
written to the governance journal.

- `handler.checkpoint_passed` and `handler.checkpoint_blocked` carry the
  checkpoint ID and, when blocked, the reason code; `handler.checkpoint_missed`
  carries the missing IDs.
- With a persistence backend they are durable before the call returns. If
  the evidence store cannot confirm a record, the checkpoint fails, the next
  part does not run, and the persistence error propagates, as at the effect
  barrier.
- They are not effect boundaries, so the trace's lifecycle rules do not
  change.
- The decision record lists only the conditions verified at entry.

## Limits

- Checkpoints are cooperative. Only a handler that calls them gets their
  protection.
- What a checkpoint compares lives in memory while the handler runs. Nothing
  is restored after a restart, and an interrupted handler is not continued.
- A plain callback can only rely on the entry boundary.
