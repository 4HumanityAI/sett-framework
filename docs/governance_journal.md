# Governance journal

A journal that keeps opaque, versioned records on a dedicated
evidence stream, with an exact byte format, explicit limits and explicit
confirmation of durability. It never interprets what it stores.

The governance journal preserves opaque versioned bytes on a dedicated,
injected EvidenceStream. Payloads are not interpreted, including payloads
whose schema name looks familiar. Empty bytes do not represent a decision,
review or absence of obligations. The journal itself installs no
real durability producer. Configured on `PolicyRuler` with
`governance_journal=`, it receives the decision records that make up the
durability state; see [Decision records](decision_records.md). With review
verifiers, it also receives the review records of the follow-up; see
[Policy reviews](policy_reviews.md#following-up-obligations).

## Public surface

Seven frozen values and the journal itself, exported only by
`sett.policy_ruler`.

The following objects belong to `sett.policy_ruler.persistence` and are
reexported as the same objects from `sett.policy_ruler` only.

| Name | Meaning |
|---|---|
| GovernanceRecord | Operation ID, domain schema ID/version and immutable payload bytes. |
| GovernanceSubmission | Record, original expected head, derived write ID and exact frame bytes. |
| GovernanceEntry | Position, write ID, record, exact frame, integrity status and non-interpretation status. |
| GovernanceReadResult | Admitted prefix, sampled head, completeness, reason and offending index if attributable. |
| GovernanceWriteResult | Original submission, outcome/stage, admitted entry and separate append/flush receipts when available. |
| GovernanceReconciliation | Original submission, bounded read result, matching/conflicting entry and explicit confirmation outcome. |
| GovernanceJournal | Preparation, bounded reading, at-most-one append attempt and explicit reconciliation. |

Values are frozen keyword-only dataclasses. Payload/frame values and nested
payload-bearing values are omitted from repr for reading convenience. Frozen
values, omitted repr and byte copies are not access control, redaction,
encryption or isolation from hostile Python code. Opaque identifiers are not
inherently safe to disclose. No request or diagnostic tuple is captured
automatically; selecting and protecting stored bytes remains an application
responsibility under its own domain schema.

`GovernanceJournal` requires keyword arguments `namespace_id`, `journal_id`,
`stream`, `origin`, `required_durability`, `integrity_policy`,
`historical_integrity_policies`, `max_frame_bytes`, `max_records` and
`max_total_bytes`. There are no implicit policy, durability or resource defaults.

`prepare(record, *, expected_head)` creates a GovernanceSubmission without
storage calls, using the configured integrity policy. `append(submission)`
returns a GovernanceWriteResult. `read()` returns a GovernanceReadResult
without appending or flushing. `reconcile(submission)` returns a
GovernanceReconciliation and may flush; it is not purely read-only.
`confirm(position)` flushes the stream through one of the journal's entries
and returns `None` when the achieved durability meets `required_durability`,
or the reason code otherwise: the read's own reason,
`policy.journal_identity_mismatch` for a position the journal does not hold,
a storage failure or `policy.journal_durability_unconfirmed`. It never
appends, and a flush through a position also covers every earlier entry.

## Exact format and identities

The exact bytes of each frame, and how a write is identified.

Envelope version 1 uses unsigned big-endian integers and strict UTF-8.
T(text) is a u16 byte length and its bytes; B(bytes) is a u32 length and bytes.
The body contains, in order, T(namespace), T(journal), T(operation), T(schema),
u32(schema_version), B(encoded origin), B(encoded predecessor),
T(integrity policy ID), T(algorithm) and B(payload).
Positions use the stream-position codec of the persistence layer.

The preimage is eight bytes `SETTGOV\0`, u16(1), and B(body).
The frame adds a one-byte digest flag: zero for None, or one followed by B(digest).
A present digest is nonempty and at most 65535 bytes. There is no padding,
compression, normalization, automatic timestamp or trailing data. The configured
integrity policy receives the entire preimage. Unknown envelope versions stop
complete restoration; domain schema IDs/versions remain opaque.

The write ID is `governance.v1.` plus lowercase SHA-256 hex over the bytes
`SETT-GOVERNANCE-WRITE`, zero, `v1`, zero, T(namespace), T(journal), T(operation).
It deliberately excludes schema, payload and predecessor. Reusing an operation
with different full frame bytes is a conflict, not a new identity. Full frames
are compared; the hash-derived ID never replaces that comparison.

Journal identity strings use lowercase ASCII letters/digits followed by those
characters or dot, underscore and hyphen, with 1..256 bytes. Exact strings,
bytes, records, submissions and positions are admitted at the local boundary;
subclasses do not widen that contract. Schema versions are exact integers
1..2**32-1, excluding bool. The explicit origin binds a backend and stream,
without assigning numeric order to cursor tokens or proving historical origin
authenticity. The stream is dedicated; an execution trace is not a journal.

## Bounds and reading

Every scan is bounded, and only a fully consistent scan counts
as complete.

The mandatory positive frame/count bounds are below 2**32 and total bytes below
2**64, with total at least the frame bound. Frame bytes include headers/digest.
A scan retains at most the configured count and total bytes, reading at most
count plus one entries to detect overflow. Injected code can allocate before
yielding or run indefinitely; these limits do not sandbox it. History scans
are linear and retain a bounded in-memory prefix.

A complete scan requires matching initial/final heads, backend order, matching
origin and predecessor continuity, unique positions and operation identities,
valid frame associations and successful configured integrity admission. The last
position must equal the head; an empty scan requires head equal origin.
No head-based retry, repair, truncation or automatic skipping occurs.

A partial result retains only the admitted prefix. Its sampled head is an
observation, not an assured complete snapshot. Original backend bytes remain
unchanged. Two matching heads rely on the append-only backend contract and do
not prove that a hostile backend never rewrote history between observations.

## Integrity and confirmation

Integrity says only that the configured policy checked the
bytes; durability is confirmed only by an adequate flush receipt.

Every entry declares `schema_status="not_interpreted"`. Integrity is separately
`verified` or `disabled`. Explicit NoDigestPolicy is supported and must never
be represented as verified integrity or satisfaction of a required integrity
property. Missing historical policies, changed policy identity/algorithm, false
or nonboolean verification, and malformed digests stop admission.

A positive integrity result reports only the configured policy's check of these
bytes. An unkeyed hash does not authenticate a writer able to rewrite both bytes
and hash, and provides no secrecy. The journal cannot detect undisclosed changes
inside injected policy code. Policy IDs/algorithms are snapshotted and checked
again before invocation; behavior remains a responsibility of that code.

An append receipt alone does not confirm durability. Confirmation requires
matching full bytes and an admitted flush receipt through that exact position,
with adequate achieved durability under the persistence durability ordering.
Passive health
or capability declarations cannot substitute for it. This is a backend-attributed
confirmation, not an independent measurement of survival of a physical failure.
IN_MEMORY cannot satisfy a stronger durability requirement.

## Failure and reconciliation

A failed or uncertain write is reported as such, never rolled
back or retried, and reconciliation only looks for the original bytes.

Local invalid construction/admission raises SETTPersistenceFormatError
with `policy.journal_invalid`, fixed text, no backend ID and no raw cause or
context, including construction inside an active caller exception.

Write outcomes are `not_submitted`, `append_uncertain`,
`appended_unconfirmed` and `confirmed`. A post-call failure cannot claim no
write happened. A valid but insufficient flush receipt remains observable;
malformed responses are not exposed as valid receipts. An identical existing
frame requires no further append call; its durability is confirmed separately.
There is no automatic append retry, identity replacement, rebase or rollback.

Explicit reconciliation reports `inconclusive`, `not_found_in_prefix`,
`conflicting`, `found_unconfirmed` or `found_confirmed`.
Absence from one stable bounded prefix is not eternal absence or permission
to retry an action. Reconciliation never appends, reissues authority, runs
callbacks or retries an effect. Termination signals propagate unchanged and can
leave a write outcome uncertain.

The [journal reasons](policy_reason_codes.md#governance-journal-diagnostics)
distinguish journal failure from policy denial. No transaction joins storage,
journal, trace, authority consumption and external effect. Recovery returns
bytes, not a resumed action or valid decision/review. Complete domain schemas,
real producers, governed recovery, review semantics and integration with
authorization are not part of this component.

