# Decision: keep the original failure code when the failure report cannot be written

When the failure report cannot be written, the error keeps the
original failure code next to `authorization.evidence_failed`.

**Status:** decided by the project maintainer and implemented. The contract is section 4 of
[Authorization failure details](authorization_failure_details.md#4-original-failure-code-when-the-failure-report-cannot-be-written).

## Context

SETT writes every authorization failure to the configured evidence sink
before raising it. If that write fails, the framework raises
`SETTAuthorizationConfigurationError` with `authorization.evidence_failed`
instead. A caller must know that evidence is not being recorded.

Without a separate attribute, the reason code of the original failure was
then lost. It was not on the error, and it was not in the evidence, because
the write that would have recorded it is the one that failed. A caller who
received `authorization.evidence_failed` could not tell whether the attempt
was refused because a condition was not met, because a verifier broke, or
because the clock was invalid.

## Options considered

1. Keep the earlier behavior: only `authorization.evidence_failed` reaches
   the caller.
2. Keep `authorization.evidence_failed` as the primary reason code, and also
   keep the original reason code on the error, in a separate attribute.

Putting the original reason code back in first place was not proposed. It
would hide the fact that evidence is not being recorded.

## Decision

Option 2. The error gains `original_reason_code`, which holds the reason code
of the failure that was being reported when the evidence write failed.

## Reasons

- In this situation the original reason code is the only remaining trace of
  why the attempt failed.
- Nothing reported before changes: the primary reason code, the error class
  and the precedence stay the same.
- It exposes no new kind of data. The original code is the one the caller
  would have received if the write had succeeded.

## Consequences

- When `authorization.evidence_failed` comes from a write that was reporting
  a failure, `original_reason_code` holds that failure's code. In every other
  case it is `None`.
- The original failure is still missing from the evidence. The attribute
  lives only on the error object, and if the caller does not log it, it is
  gone. It does not replace a working evidence sink.
- Like the other diagnostic attributes of the error, it is not authority and
  the framework never reads it back.
