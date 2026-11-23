# SETT security and safety model

## Execution trace privacy

SETT 0.11 adds causal execution traces without turning the trace into a copy
of application memory. The default trace contains component names, operation
types, statuses, reason codes, correlation identifiers, safe counts, risk
decision fields, and error types. It does not contain input payloads, action
payloads, handler results, exception messages or locals, prompts, model
responses, biometric values, risk profiles, or private-memory values.

`ExecutionContext.metadata` accepts only recursively JSON-safe values. It
rejects sensitive key fragments, framework-reserved keys, non-finite numbers,
non-string mapping keys, and unsupported objects. It is bounded by key count,
depth, and serialized size. Optional subject/session identifiers are opaque;
applications should use pseudonymous values.

Sensitive-key matching folds Unicode compatibility forms, case and accents
before checking known fragments. It is a heuristic over key names, not a
content scanner or a general cross-script homoglyph detector. A plausible
key name can still carry private data in its value; applications remain
responsible for admitting only appropriate metadata.

`export_trace()` returns defensive sanitized dictionaries. Exporter callbacks
receive the same defensive representation. Exporters are best-effort for
diagnostic events, but a failure while exporting the pre-effect
`handler.authorized` event records `handler.blocked` and blocks the handler.
`handler.started` therefore means `handler.authorized` was committed and
exported successfully. When Authorization Ruler is configured, the additional
`authorization.receipt_verified` event causally precedes
`handler.authorized`, and a final gate after that event's exporters checks
that the consumed grant did not go stale meanwhile; if it did,
`handler.blocked` is recorded instead of `handler.started`. SETT does not permit an effect it cannot trace at that
configured boundary. A start event opens the dispatch span. Once its exporters
return and the payload is copied, SETT checks authority again immediately
before calling the handler. A late rejection closes that start with
`handler.entry_blocked`. The receipt stays consumed and the handler never runs.

Direct `TraceRecorder.record()` calls apply the same recursive constraints:
attributes are deeply frozen, limited to 32 keys, depth 8, and 16 KiB,
non-finite numbers and sensitive key fragments are rejected, and unsupported
objects are never converted with `repr()`.

Without an `EvidenceStream`, event names and reason codes retain their
existing unbounded vocabulary. Large values can consume memory; the recorder
does not enforce a total memory quota. With a stream configured, vocabulary
and frame limits apply even when that stream stores evidence in RAM.

The recorder's hash chain is tamper-evident, not signed. External append-only
storage, signatures, access control, retention, and deletion policy remain
deployment responsibilities.

## Fail-closed execution

A `SETTExecutor` executes a registered handler only when all of these conditions
are true:

1. it is attached through `SETTOrchestrator.register_executor()`;
2. the attached `UniversalMemory` has an `EthicalFilter`;
3. a handler is registered for the exact action type;
4. the filter approves the proposed `Action`;
5. when Authorization Ruler is configured, the application resolver preserves
   SETT-owned identity and payload, every applicable policy grants, and the
   issued receipt verifies and is consumed immediately before the effect.

Any missing condition raises and no handler runs. Agents must also be registered
before publishing results or proposing actions; detached agents no longer
silently discard governed operations.

Handler routing is resolved before policy work. If no exact route exists, SETT
does not inspect or copy the payload and does not manufacture an ethical
verdict. It emits the normalized trace decision `action.blocked` with
`verdict="block"` and `handler.not_registered`, then raises a structured
`SETTConfigurationError`. No handler boundary opens and no effect can run.
The controlled path performs the same check before payload fingerprinting,
idempotency reservation, or attempt tracing. Consequently an impossible
action cannot consume a business key or appear as a handler attempt.

Authorization is optional for compatibility, but mandatory for every handler
once enabled on an executor. Configuration requires both an
`AuthorizationRuler` and an `AuthorizationRequestResolver`; there is no
per-handler or per-action bypass. The resolver supplies application-owned
authority facts, while SETT supplies and then verifies action identity, trace
identity, action type, execution mode, operation key, and the detached payload.
Any discrepancy blocks the handler. The configured path captures action
identity before callbacks so mutation of the caller's `Action` cannot
substitute another action type's authority for the already selected handler.

Authorization events contain only opaque identifiers, counts, verdicts,
reason codes, and error types. They exclude payloads, principals, resources,
scope values, raw approval evidence, and request fingerprints. SETT verifies
declared structure and provenance, not whether a consuming application told
the truth about its external identity or resource system.

## Situation urgency is not action harm

`SafetyAssessment` keeps four dimensions separate:

- `situation_urgency`: seriousness of the human context;
- `action_harm_risk`: expected harm caused by the proposed action;
- `omission_risk`: expected harm caused by doing nothing;
- `protective_action`: whether the action is intended to reduce omission risk.

The EthicalFilter derives its base verdict from `ContextAnalysis.risk_score`,
which a domain `ContextAnalyzer` must derive from the proposed action. High
urgency never inflates that score and does not automatically reject or warn an
action explicitly classified as protective.

As a conservative fallback, when `human_at_risk=True`, the action is **not**
classified as protective, and the score would otherwise produce `ALLOW`, the
filter promotes only that decision to `WARN`. The score remains unchanged and
the audit entry records the reason code
`human_at_risk_without_protective_classification`. This means “review-worthy
urgency without a protective classification”, not “the action is necessarily
harmful” and not necessarily “no domain analyzer exists”.

## Defensive snapshots

Universal-memory reads, history, ethical audit entries, and Executor logs return
deep copies. Mutating a returned object cannot rewrite internal state.
`PrivateMemory.read()` and `PrivateMemory.get_all()` return deep copies as well.
Mutating a returned nested value does not modify stored state or create a
history entry. Applications should prefer immutable values where practical
and use `write()` to change stored state.

## Tamper-evident logs

Each history or audit entry contains a sequence number, the previous entry hash,
and its own SHA-256 hash. `verify_history()` and `verify_audit_log()` verify the
chain. Unified execution-trace verification additionally rejects duplicate
event IDs, cross-trace or forward causes, missing/cross-trace parent runs, and
instrumented starts without exactly one corresponding terminal outcome.

An open boundary after an interrupted process also makes unified `verify()`
return `False`; this does not by itself establish corruption. Durable recovery
separates `RecoveryReport.chain_integrity` from per-trace completeness so an
operator can distinguish intact but interrupted evidence from altered evidence.
Termination signals such as `KeyboardInterrupt` and `SystemExit` propagate.
If a pre-effect exporter raises one, the handler does not run and a trace
boundary can remain open. SETT does not fabricate a terminal event or treat
that interruption as evidence of a completed effect.

Durable terminal events also fail closed without sacrificing lifecycle closure.
If terminal metadata exceeds the durable contract, SETT records the same
terminal outcome with framework-owned fallback metadata and discarded-item
counts. Recovery reports distinguish that metadata loss from both an open
boundary and chain corruption.

Without configured persistence, this detects accidental or in-process
modification when verification runs. SETT can persist the unified
`TraceRecorder` chain to append-only storage through a configured persistence
backend, so its verification can survive process restarts. The separate
component history and audit-log views remain in-memory surfaces.

Persistence does **not** provide authenticity against an attacker who can
replace the complete chain and its storage, and it is not a digital signature.
Deployments that need that protection must additionally sign the chain or
anchor it in an external trusted system. SETT provides neither the
signature nor the external anchoring.

## Filesystem protection of durable persistence

SETT does not set, enforce, or verify filesystem permissions on the files a
persistence backend writes. Protecting them is the deploying application's
responsibility, and this section states the boundary explicitly rather than
leaving it implied.

The v0.14.0 defensive audit opened `SQLitePersistenceBackend`, wrote evidence,
and observed the resulting modes on a POSIX system:

```text
x.sqlite3      0644
x.sqlite3-wal  0644
x.sqlite3-shm  0644
```

Those are SQLite's `0666` creation modes reduced by a `022` umask. They are a
property of the process environment, not a SETT decision, and they grant read
access to group and other. On Windows the files carry no POSIX mode at all:
`stat` reports `0666` and effective access comes from the NTFS ACL the
containing directory hands down. The specific numbers therefore describe two
observed environments, not a guarantee about every platform or deployment.

**The directory is the control point, not the files.** A backend writes up to
three files: the database, and the `-wal` and `-shm` sidecars. The sidecars
belong to SQLite's lifecycle, not to SETT's: they appear when the database is
opened in WAL mode, and a clean `close()` removes them again. The next
`open()` recreates them from the environment in effect at that moment.
Tightening permissions on the sidecars after opening a backend therefore
protects nothing durable, because the files that were tightened no longer
exist by the time the process reopens the database. Restricting the directory
that contains them does, because both the database and every sidecar SQLite
later recreates inherit from it.

The recommended deployment procedure is:

1. Create a dedicated directory for the namespace's persistence files, owned
   by the account that runs the application, before opening any backend.
2. Restrict that directory to that account (POSIX `0700`, or an equivalent
   Windows ACL with inheritance) using the platform's own tools.
3. Open the backend on a path inside it, and place nothing else there.
4. Apply the same restriction to backups, snapshots, and log shipping. A
   correctly protected database copied into a world-readable archive is
   exactly as exposed as it would have been in place.

What is at stake is content, not configuration. Persisted evidence, stage
outputs, `PrivateMemory` state, and authorization evidence can carry the
application's own data. SETT bounds and sanitizes what reaches a *trace
event*; it does not encrypt or otherwise obscure what reaches *disk*.

`PrivateMemory` is named for its position in the architecture, not for a
filesystem property. It is private with respect to other agents and to
`UniversalMemory` (and, as convention #6 records, private by convention
rather than runtime-enforced even there). Persisting it writes strict JSON
into the same database as everything else, with no isolation, no separate
permissions, and no encryption. Nothing in the name should be read as a
storage guarantee.

SETT deliberately implements no best-effort hardening of its own. A
`chmod` applied to the database at open time would leave the sidecars
untouched, would not survive their recreation, and would not be an ACL policy
on Windows: it would read as protection while providing close to none. The
honest position is the one above - the application owns this, and owns it at
the directory level.

## Remaining boundaries

- Harm-category weights remain policy metadata; they are not silently injected
  into numerical scoring.
- SETT supplies mechanisms, not validated medical, legal, or emergency policy.
- Domain applications must provide analyzers, consent, regional procedures,
  verified contacts, and provider-specific reliability controls.

## Adapter credentials and diagnostic output

The application owns credential storage, access permissions, rotation and
revocation. SETT does not provide a secret vault or automatically inspect prompts,
conversation history or memory for secrets. Keep authentication credentials out
of content sent to models. Environment variables are a configuration mechanism,
not encryption or isolation from other code in the process.

Adapter errors retain the provider exception as `__cause__` for diagnostics.
That exception can contain secrets or request data. Full tracebacks, including
those produced by `logger.exception()` or `exc_info=True`, must be treated as
potentially sensitive. The normalized message and `safe_view()` omit raw
provider exception text; this is not a universal redaction guarantee for
application-supplied data or independently configured SDK logging.

For exported adapter-failure records, catch the error and log its structured
view without attaching exception information:

```python
import logging
from sett.exceptions import SETTAdapterError

logger = logging.getLogger(__name__)

def complete_with_safe_failure_logging(llm, prompt):
    try:
        return llm.complete(prompt)
    except SETTAdapterError as error:
        logger.warning("Adapter operation failed: %s", error.safe_view(), exc_info=False)
        return None
```

Here `None` signals failure to the caller; it is not a successful answer or
authorization. Configure other exception handlers and log exporters separately.
See [adapter failures](adapters.md#failures-resilience-and-health) for the
unchanged diagnostic contract and [LLM configuration](adapters.md#llm-request-parameters-and-credentials)
for credential precedence and request parameters.
