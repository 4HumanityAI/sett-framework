# Changelog

All notable changes to the SETT framework are documented here.

## [0.15.0] - 2026-11-23

### Adapter configuration

- Provider model identifiers are application configuration. Anthropic,
  OpenAI, Gemini and Ollama require `model`; hosted Whisper, Deepgram and
  Cartesia also require `model`, ElevenLabs requires `model_id`, and local
  faster-whisper requires `model_size`. Set these explicitly when upgrading.
  Missing or blank identifiers fail with `adapter.model_missing`. Voice IDs,
  audio containers and provider connections remain separate settings.
- Gemini uses the `google-genai` SDK through the optional `gemini` extra.
  Reinstall that extra when migrating from `google-generativeai`.

A separate policy layer between application policies and authorization:
declared policy selection, policy identity and provenance, conditions checked
by application verifiers, review obligations, a private handoff to
authorization, and an opaque governance journal. Authorization now requires
verified state from producers the application configures explicitly, and
fails closed rather than act on unverified state.

### Security and audit corrections

- Allow OpenAI callers to select `max_completion_tokens` explicitly and omit
  `temperature` with `None`. Preserve legacy request defaults; reject conflicting
  token limits before sending a request, without model-name inference or retries.
- Collect all visible Anthropic text blocks in order, excluding thinking blocks
  and retaining the empty-string result when no visible text is present.
- Accept `GEMINI_API_KEY` after `GOOGLE_API_KEY` when no explicit key is supplied.
- Correct the documented Ollama import and document safe structured error
  logging, credential ownership and the limits of offline provider checks.

- Close Ollama HTTP error responses and reuse the shared HTTP status
  classification without retrying requests or exposing provider messages.
- Preserve the denial verdict from a real policy outcome in the executor's
  authorization error, so controlled execution reports it as rejected while
  technical blocks and unmet approval requirements remain failures.

- Snapshot action identity before callbacks and payload copying, including
  execution without Authorization Ruler. Fresh root contexts supplied to
  `submit()` retain recoverable trace ancestry.
- Keep provider messages, credentials and credential paths out of public
  Ollama and Google authentication diagnostics. Provider causes remain local
  diagnostic data and must not be exported as sanitized output.
- Refuse Ollama redirects and credential-bearing base URLs; bound response
  reads with `max_response_bytes` (16 MiB by default). Reject invalid AssemblyAI
  transcript identifiers before constructing a polling request.
- Report Azure's configured audio container and reject per-call format
  overrides that change it. Construct a separate adapter for another container.
- Normalize invalid cancellation reason text to a fixed code and preserve
  authorization diagnostics and verdict classification in controlled failures.
- Require actual durability enum members and mark broken trace chains as
  requiring reconciliation. Convert excessive payload-copy recursion to a
  framework validation error without running the handler.
- Fold Unicode compatibility forms and accents in sensitive-key checks;
  document their heuristic scope and the unbounded ephemeral event vocabulary.
- Correct the final authorization-gate description, serialized-state migration
  limits and release archive name without changing prior release records.
- Require an error type in `retry_on` before anything is repeated. An adapter
  error marked `retryable` still classifies as transient for the circuit
  breaker, but it no longer causes a retry by itself, so `max_attempts` alone
  gives one attempt. Applications that relied on the implicit retry must list
  the error type and, for effects, supply an idempotency key.
- Run every submission under its own handler context. `submit()` called from
  inside a controlled handler no longer inherits the outer attempt's identity;
  a controlled handler reached that way fails closed.
- Keep a provider's `Retry-After` inside the policy's `max_backoff_seconds`. A
  longer request ends the call without a retry and without waiting; controlled
  submissions report `retry.provider_delay_exceeded`.
- Stop sending `temperature` from `AnthropicAdapter` unless the application
  sets it; `DEFAULT_TEMPERATURE` is now `None`. A value the installed SDK does
  not accept raises `adapter.configuration_invalid` before any request. The
  adapter was exercised through the real SDK against a local test server with
  `anthropic` 0.40.0, 1.17.0 and 2.8.0, not against the provider's service.
- Build the Anthropic and OpenAI clients with SDK retries disabled, so one
  adapter call sends one request. Opt in to retries with
  `execute_with_resilience()`. The `openai` extra now requires 1.55.3 or newer;
  1.40.0 could not construct a client with current `httpx`.
- Report unusable Ollama responses as `adapter.invalid_response`: bodies that
  are not UTF-8 JSON, JSON that is not an object, and `response` or
  `message.content` fields that are present but are not text. An absent field
  is still an empty answer.
- Read `Retry-After` from the header type `urllib` delivers, so Ollama errors
  carry it.
- Check an ElevenLabs `voice_id`, an Azure `region` and every configurable
  voice `endpoint` or `base_url` before a request is built. Values that could
  change the request path, query or host are refused with a configuration
  error. `http` endpoints remain accepted.
- Keep environment files, coverage data, editor settings and swap files out of
  the release archive. An `.env.example` ships only when every value in it is
  empty.
- Move the internal project-name guard out of the public tree. It remains a
  private control run before publication.

### Added

- Added `sett.policy_ruler` and `PolicyRuler`, which owns policy governance:
  it registers policies, reads the declared selection, evaluates and composes
  the policies, and keeps their obligation state. Final authority and
  receipts stay with `AuthorizationRuler`. `PolicyRuler.evaluate()` runs the
  same engine for inspection only; its `PolicyEvaluation` is never usable as
  authority. See `docs/policy_ruler.md`.
- Added policy selection values. A `PolicySelection` is the application's
  declaration of which policies apply to one exact action, and why, with one
  `PolicyApplicability` per policy; a `PolicySelectionResolver` supplies it.
  `PolicyRuler` reads it before running any policy, and the values decide
  nothing by themselves. See `docs/policy_selection.md`.
- Added `PolicyBinding`, `PolicyOrigin`, and `PolicySourceReference`. A
  binding names a policy and its version and records where its requirements
  come from. It is declared metadata, not proof. See
  `docs/policy_identity.md`.
- Added policy conditions. A `PolicyCondition` declares a check that must
  pass before an action, the verifier that performs it, and how long a
  positive result may be used. A `ConditionVerifier` is the application's
  callback; it reports a `ConditionVerification`, and the policy layer checks
  every result against what it expected. See `docs/policy_conditions.md` and
  `docs/condition_verification.md`.
- Added review obligations. A policy can declare, on its decision, a
  notification after the action, a human review with a deadline, or a
  restriction on declared future actions. SETT carries the declarations and
  the facts an application reports through a `ReviewVerifier`; it runs no
  review. See `docs/policy_reviews.md`.
- Added a final gate before a governed effect. After the effect barrier
  and its trace exporters, which run after the receipt is consumed,
  `SETTExecutor` checks that neither generation changed and that no
  deadline was reached; otherwise the handler does not run, the receipt
  stays consumed, and `handler.blocked` records the existing reason code.
  The trace event `authorization.receipt_verified` gains `decision_ref`,
  which names the issuing attempt and so its decision record. See
  `docs/api_reference.md`.
- Added controlled checkpoints. `PolicyRuler(controlled_checkpoints=...)`
  declares ordered checkpoints per action type, a condition can name one,
  and a controlled handler passes each with
  `HandlerContext.check_policy_conditions()`. There SETT verifies that
  checkpoint's conditions and checks the earlier ones again; a failure
  stops the next part of the handler, undoes nothing, is reported as
  `ExecutionStatus.PARTIAL` and is never retried. A plain handler is
  refused for such an action type. See `docs/controlled_checkpoints.md`.
- Added the review follow-up. `PolicyRuler.follow_up_reviews()`, called when
  the application chooses, asks each declared obligation's verifier once and
  writes each newly verified fact to the governance journal as a review
  record (schema `sett.governance.review`), confirmed before it is
  reported; records already in the journal are confirmed durable before
  they are trusted. A record grants nothing and lifts no restriction. SETT decides
  no deadline: the application reports overdue through its verifier. See
  `docs/policy_reviews.md`.
- Added governance recovery. `read_governance_recovery()` reads the
  governance journal and a restored trace together and reports, for each
  decision, a denial, an approval requirement, a known effect or an uncertain
  one, with each review obligation's records. Missing trace events leave a
  grant's use unknown, even for an intact prefix of the correct trace.
  Positive observations require an intact chain and matching trace and
  authorization-attempt identities. Decision records and review records
  come from one journal read. It
  restores no receipt and resumes nothing; continuing is a new submission.
  SETT gives an approval-required outcome no expiry, which belongs to the
  application. See `docs/governance_recovery.md`.
- Added explanations. `explain()` in `sett.policy_ruler.explanation` builds,
  from one decision record or from the error of a blocked attempt, an
  operational view and a public view, each a closed set of fields with
  stable codes and no sentences. The public view shows SETT's own codes and
  only the application values the application approves; the rest is
  withheld and counted. Next steps appear only as the application declares
  them. See `docs/explanations.md`.
- Added working correspondences with external instruments, outside the
  package. Records in `governance/mappings/` state what SETT's mechanisms do
  for selected provisions and the tests that show it, and name what stays
  with the deployment; `tools/correspondence.py` validates them and
  generates their pages. The format is internal and experimental. A record
  decides no applicability, assesses no institution's requirement and holds
  no score, and an approval covers one exact revision. Records for three
  instruments are included, each covering selected provisions only: the EU
  Artificial Intelligence Act, NIST AI RMF 1.0 and the UNESCO
  Recommendation on the Ethics of Artificial Intelligence. See
  `docs/working_correspondences.md`.
- Added the policy-to-authorization handoff. Authorization never accepts an
  evaluation result from outside: it opens its own private invocation on the
  policy ruler, which binds the exact result to that invocation, and the
  binding is checked again before a receipt is issued and before authority
  is used. See `docs/policy_handoff.md`.
- Added `GovernanceJournal` in `sett.policy_ruler.persistence`, with its
  record, submission, and result values. It keeps opaque, versioned records
  on a dedicated evidence stream, with an exact byte format, explicit limits,
  and explicit confirmation of durability, and it never interprets what it
  stores. `confirm(position)` checks again that an entry is durable as
  required, without appending. See `docs/governance_journal.md`.
- Added `SETTPolicyConfigurationError`, raised when the policy layer cannot
  establish a valid binding or a policy value is invalid. It is a
  `SETTConfigurationError`, not a `SETTAuthorizationError`, and it is exported
  by `sett.policy_ruler` and `sett.exceptions`, not by top-level `sett`.
- Added `SETTAuthorizationError.policy_observations`. On its covered initial
  evaluation paths it is a possibly partial tuple of the admitted policy ID,
  version, and verdict rows; otherwise it is `None`. It is diagnostic data,
  not authority or a durable audit record.
- Added authorization failure details. `SETTAuthorizationError` gains
  `policy_observation_reason_codes`, `failed_condition`,
  `attempted_conditions`, `authorization_attempt_id`, and
  `original_reason_code`, and `AuthorizationOutcome` gains
  `authorization_attempt_id`. Each call to `authorize()` or
  `verify_and_consume()` gets a random identifier that its error or outcome
  and all its evidence events carry. Each field is `None` where it does not
  apply, and nothing that existed changes. See
  `docs/authorization_failure_details.md`.
- Added `templates/consent_model.py` and `docs/consent_model.md`: a
  conceptual consent model to modify or replace. A policy declares a consent
  check as a condition, and the model's verifier looks for a current
  consent record that covers the request. Consent belongs to the
  application; the model does not supply the consent state that
  authorization requires.
- Added `PolicyRuler(implementation_owned_consent=...)`. With `True`, an
  implementation that owns consent declares it, and an internal producer
  records that declaration in the consent state; SETT verifies no consent.
  The default, `False`, changes nothing.
- Added decision records. `PolicyRuler(governance_journal=...)` installs
  the durability producer: every completed bound evaluation writes one
  record (schema `sett.governance.decision`, version 4) to the governance
  journal, and no receipt exists before that record is confirmed at the
  journal's required durability; otherwise the attempt blocks with
  `authorization.decision_record_unconfirmed`. The record carries settled
  facts and a request fingerprint, not the request payload. Each policy row
  keeps the origins and profile its binding declared, and each verified
  condition the requirements it covers; the results of informative
  policies, which decide nothing, are kept with them.
  `read_decision_records()` returns them as values and restores no
  authority. See `docs/decision_records.md`.
- Added the review producer. With `PolicyRuler(review_verifiers=...)` and a
  governance journal, a restriction on declared future actions that an
  earlier decision recorded blocks later actions of those types until the
  application's verifier reports a favorable completed review. An
  authorization is never blocked by the obligations it declared itself; that
  exemption comes from the decision record it confirmed, never from the
  action identifier, so a later action that reuses the identifier is still
  restricted. Notifications
  and reviews with a deadline never block before an effect. Each failure has
  its own `policy.review_*` code and `authorization.review_*` translation,
  and decision records add the declared review obligations and the
  restrictions each action cleared. See `docs/policy_reviews.md`.
- `ElevenLabsTTSAdapter` accepts `output_format`, an ElevenLabs MP3 format
  identifier such as `mp3_44100_128`, sent as the provider's
  `output_format` query parameter. Other values fail at construction with
  `adapter.output_format_invalid`. The default, `None`, leaves the request
  unchanged.
- Added the `policy.*` reason codes and the `authorization.*` codes of the
  bound policy path. See `docs/policy_reason_codes.md` and
  `docs/authorization_reason_codes.md`.
- Added `docs/terminology.md`: what each SETT term means, why it has that
  name, and where its boundary is.
- Added `examples/governed_action.py`: one governed action end to end, with
  every state producer configured. A granted refund runs its handler once;
  a refund whose declared condition is not met, and one the policy denies,
  run nothing. The journal keeps the two decision records, and each
  decision is explained.

### Changed

- Shared governance modules now live in `sett._governance` and Google
  credential helpers in `sett._adapters.google_auth`. Previous import paths
  remain available. Import compatibility does not migrate serialized state.
  Legacy `PolicyDecision` pickles are not a supported migration format.
  Successful deserialization does not establish that the new required fields
  are present; even representing an incomplete value can fail. Reconstruct
  decisions through the current constructors with explicit `conditions` and
  `review_obligations`; SETT does not infer empty declarations for legacy
  serialized objects.

- `AuthorizationRuler` requires an injected `PolicyRuler` through
  `policy_ruler`, built with the same clock and evidence sink objects.
  Omitting it fails with `authorization.policy_ruler_missing`, anything other
  than a `PolicyRuler` fails with `authorization.policy_ruler_invalid`, and a
  different clock or evidence sink fails with
  `authorization.policy_ruler_configuration_mismatch`. An optional
  `selection_resolver` must be the one the policy ruler holds.
- `AuthorizationRuler.register_policy()` requires a `binding`. The 0.14
  two-argument call fails with `authorization.policy_binding_required`.
- `PolicyDecision` and its `grant()`, `deny()`, and `require_approval()`
  factories require keyword-only `conditions` and `review_obligations`, with
  no default. Omitting them raises `TypeError`, and `None` is invalid: SETT
  never infers an empty declaration for code that says nothing.
- `SETTAuthorizationError` and `SETTAuthorizationConfigurationError` are
  never retried by a `RetryPolicy`, even when `retry_on` names them, like a
  filter rejection.
- Consuming a receipt requires the exact receipt object that was issued. An
  equal copy fails with `authorization.policy_binding_unavailable`.
- Moved `PolicyDecision` and `AuthorizationPolicy` to `sett.policy_ruler`
  (`models` and `contracts`). Imports through `sett.authorization_ruler`, its
  `models` and `contracts` modules, and top-level `sett` return the same
  objects. The new selection and identity values are exported by both
  `sett.policy_ruler` and `sett.authorization_ruler`; top-level `sett` gains
  no export.
- Moved the shared authorization values, verdicts, validation, clock, and
  evidence contracts to private modules, so the policy package does not
  import the authorization package. Public imports are unchanged. The
  `__module__` of a moved class names its new location; no pickle or
  cross-process compatibility follows from the move.
- `AuthorizationRuler` and `SystemAuthorizationClock` are implemented in
  `sett.authorization_ruler.authorizer`, and `PolicyRuler` in
  `sett.policy_ruler.evaluator`. `sett.authorization_ruler.ruler` and
  `sett.policy_ruler.ruler` re-export the same classes; private helpers and
  former module globals are not covered.
- `examples/governance_card.py` now wires a `PolicyRuler`, declared bindings,
  and a selection resolver. It configures no verified-state producers, so
  every governed effect in it is refused at the authorization handoff with
  `authorization.policy_evaluation_incomplete`, and the card records that
  gate.
- The project homepage and repository links point to
  `github.com/4HumanityAI/sett-framework`.
- SETT tracks the latest stable Python release. Python 3.15 is the version
  it is developed and verified against, 3.13 and 3.14 remain supported, and
  `requires-python` is `>=3.13`. Python 3.11 and 3.12 are no longer
  supported.
- The CI workflow keeps nothing it builds. Its package job, now named
  "Package verification", still builds the wheel, the source distribution
  and the release archive, installs the wheel into an empty environment,
  runs `pip check` and imports the installed package, but no longer uploads
  them as downloadable artifacts. The matrix runs Python 3.13, 3.14 and
  3.15.

### Fixed

- Restrict the release line-ending guard to text files, preserving declared
  binary formats such as PNG. Regression checks retain CRLF detection in
  source, documentation and files without a known extension.

- Excluded generated governance card HTML and JSON files from the source
  archives and ignore them in every working directory.

- The executor now rechecks the consumed grant after start-event exporters
  and payload copying, immediately before handler entry. A late rejection
  records a blocked effect and leaves the receipt consumed.
- ElevenLabs TTS and the shared HTTP voice transport reject redirects.
  Gladia checks the origin of each polling URL before attaching its API key.
- `TraceRecorder.restore()` reported every governed effect that had
  started, even a completed one, with its `handler.authorized` boundary
  unresolved, so `RecoveryReport.requires_reconciliation` was true for it.
  `handler.started` ends that boundary and starts its own, and the pairing
  treated it only as a start. It now closes the first boundary before
  opening the second. The defect is also present in 0.14.0.

### Documentation

- `docs/SETT_Conventions.md`: each convention opens with a one-sentence
  summary. Convention 10 now asks for an explicit, approved migration when a
  change cannot keep a contract; conventions 14 and 15 now describe
  author-led evolution with community proposals, and explicit scope with
  preserved contracts. A new section describes how changes are approved.
- `docs/api_reference.md` documents the policy layer and opens each top-level
  section with a one-sentence summary. Its `SETTExecutor` section notes when a
  receipt can be issued, and its exceptions table now lists
  `SETTAuthorizationError`, `SETTAuthorizationConfigurationError`, and
  `SETTPolicyConfigurationError`.
- `docs/authorization_reason_codes.md` adds the codes of the bound policy
  path, the condition and review diagnostics, and a "Current limitation"
  section that explains when no receipt is issued. The README states the same
  limitation
  and gains a project status section.
- `docs/authorization_failure_details.md` describes the failure details,
  and two companion documents record the decisions behind them.
- `docs/concepts.md` separates architectural conventions from runtime
  enforcement: private memory is isolated by convention, not by a runtime
  boundary, and SETT provides the pieces to re-evaluate a result, not an
  automatic review workflow.
- `CONTRIBUTING.md` explains how decisions are made. Documents now record
  only the version that introduced them, without a "Last reviewed" line.
- `docs/getting_started.md` and the README state which Python versions SETT
  supports and the ones the suite passed on locally before release.
- Docstrings and comments across the package describe the current behavior
  without internal review labels. No code changed with them.
- Added `tests/test_no_internal_process_labels.py`, which keeps internal
  process labels out of the public tree. Released changelog entries are
  excluded, because published history is not rewritten.

### Compatibility

- Authorization issues a receipt only when every verified state it requires
  has a producer: condition verifiers, the implementation's consent
  declaration, a governance journal and review verifiers, each configured
  explicitly on `PolicyRuler`. Without them, every `authorize()` call that
  reaches state verification fails with
  `authorization.policy_evaluation_incomplete`, even when every policy would
  grant, and a handler that requires authorization cannot run. An
  application that used the 0.14 authorization path has every governed
  effect refused until it configures them. This is deliberate: SETT blocks
  rather than act on unverified state. A policy denial during such an attempt is visible only in
  `SETTAuthorizationError.policy_observations`, with its reason codes in
  `policy_observation_reason_codes`.
- Code written against the 0.14 authorization API must change before it
  runs: `AuthorizationRuler` needs a `PolicyRuler`, each `register_policy()`
  call needs a `binding`, and every `PolicyDecision` needs explicit
  `conditions` and `review_obligations`. Each omission fails closed, with
  the reason code given under Changed or with `TypeError`.
  `examples/governed_action.py` shows a complete configuration.
  For example, replace `PolicyDecision.grant(policy_id)` with
  `PolicyDecision.grant(policy_id, conditions=(), review_obligations=())`
  only when that policy actually declares no conditions or review obligations.
- Applications that do not configure authorization are not required to add a
  policy layer or governance journal. They must still review the Python,
  adapter, retry and other behavior changes in this release; authorization
  being optional does not make the whole upgrade backward-compatible.
- No 0.14 public symbol was removed or renamed, and top-level `sett` exports
  the same names. Classes that moved keep their 0.14 import paths.
- No new mandatory dependency. Supported Python versions are 3.13, 3.14, and
  3.15. Python 3.11 and 3.12 are no longer supported: an application on
  either must move to 3.13 or newer before upgrading.

#### Migrating from v0.14.0

Review these steps before replacing an existing installation. Adapter and retry
changes also apply to applications that do not configure authorization.

1. **Update the environment.** Use Python 3.13 or newer and reinstall SETT from
   the new source tree with the optional extras your application uses. Gemini
   now uses `google-genai`, not `google-generativeai`; the OpenAI extra requires
   `openai>=1.55.3`. SETT is not published on PyPI.
2. **Supply provider model identifiers.** Applications that relied on adapter
   defaults must pass `model`, `model_id` or `model_size`, as listed in
   [Provider model configuration](docs/adapters.md#provider-model-configuration).
   Choose an identifier available to your deployment; SETT supplies no fallback.
   Voice IDs and audio formats are separate configuration. Review the adapter
   changes above if your application overrides formats, endpoints or temperature.
3. **Declare policy selection before wiring authorization.** Build a
   `PolicyBinding` for each policy and a `PolicySelectionResolver` that declares
   applicability for the exact request. Pass the resolver to `PolicyRuler`;
   omitting it raises `authorization.policy_selection_resolver_missing` during
   construction. Registration does not substitute for selection. Required and
   informative policies have different roles, and no applicable required policy
   means `authorization.policy_required_empty`, not permission. Supply the
   declared applicability facts with their provenance; SETT does not infer them.
   See [Declared selection](docs/policy_ruler.md#declared-selection).
4. **Configure the required state producers.** Explicitly supply condition
   verifiers, a governance journal and review verifiers. An empty verifier
   sequence is a declaration that must match the applicable requirements;
   `None` installs no producer and cannot stand in for that declaration. Set
   `implementation_owned_consent=True` only when the application owns consent;
   it records that responsibility and is not evidence of consent verification.
   Choose the journal's storage and durability for the deployment; the in-memory
   example is not durable across process exit. Inject the resulting `PolicyRuler`
   into `AuthorizationRuler`, using the same evidence-sink object and, if a clock
   is supplied explicitly, the same clock object. Omitting the authorization
   clock uses the policy ruler's clock. See
   [Construction](docs/policy_ruler.md#construction) and the complete
   [governed action example](examples/governed_action.py).
5. **Update policy calls and receipt handling.** Add `binding` to policy
   registration and explicit `conditions` and `review_obligations` to the
   `PolicyDecision` constructor and all three factories: `grant()`, `deny()` and
   `require_approval()`. Empty declarations mean no obligations of that kind;
   they must not replace missing application data. Retain the exact issued
   receipt object for consumption; an equal copy is rejected. Reconstructing a
   decision or loading stored data does not restore authority. See
   [Migrating stored policy decisions](docs/getting_started.md#migrating-stored-policy-decisions).
6. **Review retries explicitly.** `max_attempts` alone no longer permits a retry,
   even if an adapter marks an error `retryable`. Name the permitted error types
   in `retry_on`; errors that SETT never retries remain excluded. For adapter
   calls through `execute_with_resilience()`, also declare
   `operation_is_idempotent=True` only when that is true. A retry-enabled effect
   submission requires an `idempotency_key`; that key does not make an external
   service idempotent. Anthropic and OpenAI SDK retries are disabled. A provider
   `Retry-After` above `max_backoff_seconds` ends the call without sleeping or
   retrying: `execute_with_resilience()` raises the original error and
   `submit_controlled()` reports `retry.provider_delay_exceeded`. See
   [Resilience](docs/adapter_contract.md#10-resilience) and the
   [API reference](docs/api_reference.md).

Existing public import paths remain available, but import compatibility does not
migrate serialized objects or preserve pending authority across an upgrade.
Review the changes above against your application's configuration and stored
data before resuming governed work.

### Validation

- The test suite covers policy selection, identity and provenance,
  conditions and their verification, review declarations and facts, the
  policy-to-authorization handoff with its fault cases and controlled
  concurrent interleavings, the governance journal, policy observations and
  the other authorization failure details, import compatibility for the
  moved modules, the label guard, the consent model template, the ElevenLabs
  output format, the consent declaration, the decision records, the review
  producer and the identity of the authorization it exempts, governed
  execution through the executor, controlled checkpoints, the review
  follow-up, governance recovery with its stopping points, trace coverage
  and single journal read, the pairing of trace boundaries on restore,
  explanations with their disclosure checks, policy origins in decision
  records, the working correspondences tooling, alike requirements of
  different policies verified separately, the informative results kept in
  decision records, review records confirmed durable before they are
  trusted, and the governed action example.
- Release measurements are recorded against exact source hashes and
  environments. Earlier measurements do not establish that modified bytes
  pass on another interpreter or in an installed artifact.
- Release verification includes the wheel and source distribution builds,
  clean installation, imports and examples against the installed package.
  These checks were run on the exact source of this release.
- This release was validated on Windows with Python 3.13, 3.14 and 3.15.
  Linux and macOS have not been validated for this release. The CI workflow
  lists them, but those cells were not run on this source.
- Not established by this release: any measurement on Linux or macOS,
  coverage, live provider calls against any external service, and any
  measurement of the durability declarations against real power loss.

## [0.14.0] - 2026-09-03

Opt-in durable evidence, versioned state, pure recovery, explicit audit
closure, copy-on-write migrations, deterministic authorization, and a separate
execution boundary. SETT restores what durable evidence can support and never
resumes interrupted work automatically.

### Added

- Added `sett.authorization_ruler`: detached requests with explicit field
  provenance, exact policy registration, restrictive deterministic composition,
  verified additional authority, and store-backed receipts. Only `GRANT`
  permits an effect; `DENY` and unresolved `REQUIRE_APPROVAL` do not.
- Added policy-owned `ReceiptUse`. Receipts default to `SINGLE_USE`; controlled
  idempotent replay requires unanimous policy consent, a controlled request,
  and an operation key. Expiry, revocation, and complete receipt/request
  matching remain mandatory. The in-memory receipt store makes no restart
  guarantee, and policy consent does not prove external idempotence.
- Added a versioned strict JSON authorization fingerprint with typed nodes,
  NFC normalization before key ordering, normalized numeric equivalents, and
  exact hexadecimal non-integral floats. Normative vectors cover equivalent
  values, invalid inputs, and stable expected digests. This representation is
  separate from historical trace preimages.
- Added `AuthorizationRequestResolver` and optional paired executor
  configuration. Once enabled, authorization is mandatory for every handler
  invocation. Ethics runs first; the resolver preserves SETT-owned identity
  and payload; a receipt is issued and consumed before `handler.authorized`.
  Raw approval inputs reach verifiers through a separate channel, never
  policies or trace attributes. No per-action bypass is provided.
- Added sanitized authorization evidence and structured authorization errors.
  Composition conflicts emit evidence before raising. Execution traces link
  `authorization.receipt_verified` to `handler.authorized` and report rejected
  authority without claiming that a handler ran.
- Added the public `sett.persistence` API. `EvidenceStream` and
  `StateRecordStore` keep append-only evidence separate from random-access
  state while `PersistenceBackend` binds both to one stable, opaque namespace.
  Receipts carry backend identity and declared durability; `UNKNOWN` remains
  outside the durability order and never satisfies an application requirement.
- Added versioned binary formats for exact trace preimages, stream positions,
  stage-state envelopes, private-memory mutations, and migration manifests.
  Recovery verifies the exact bytes that were persisted and never relies on a
  JSON round trip to reproduce a historical hash.
- Added `IntegrityPolicy`, `Sha256IntegrityPolicy`, and `NoDigestPolicy`.
  State digests cover the complete envelope except its backend-owned locator.
  SHA-256 is documented as change detection, not secrecy or authentication;
  keyed policies remain application-owned because SETT does not manage keys.
- Added thread-safe in-memory reference implementations and a SQLite reference
  backend with transactional compare-and-append and idempotent state writes.
  SQLite durability declarations come from effective pragma readback rather
  than requested settings. Each SQLite file is bound to both its backend and
  namespace identities. WAL with FULL or EXTRA can declare `POWER_LOSS`;
  rollback journals require EXTRA for that declaration, while FULL remains a
  conservative `PROCESS_CRASH`. Both backends pass the reusable,
  dependency-free contract probes exported by `sett.testing`.
- Added pure durable recovery through `TraceRecorder.restore()` and explicit
  continuation through `TraceRecorder.resume()`. `RecoveryReport` separates
  chain integrity, trace completeness, reconstructed outcome, unresolved
  boundaries, stage descriptors, diagnostics, and the last durable position.
  One interrupted trace no longer makes a complete trace look incomplete.
  The stream remains authoritative when an accepted anchor cannot confirm its
  required durability, but the unconfirmed anchor is not published in the
  local view. A failed confirmation or an append with no receipt blocks
  further recording until explicit recovery, preventing continuation from an
  uncertain durable prefix or stream head.
- Added durable stage-output persistence for `run_pipeline_controlled()`.
  Exact state bytes reach the required durability before
  `pipeline.stage_completed` can claim completion. Materialization is a
  separate explicit operation that reports both integrity and interpretation
  status before returning a value.
- Added opt-in `PrivateMemory` persistence through a stable `memory_id`.
  Writes and clears require a real execution context, record durable intent,
  persist strict JSON state before changing the live view, and record a
  content-free confirmation. Restoration replays only confirmed mutations;
  ordinary ephemeral memory retains its existing Python-value behavior.
- Added `AuditClosure` as an axis independent from `ExecutionStatus`. A known
  effect outcome can be returned with an explicit audit gap or escalated as
  `SETTAuditClosureError`, according to the required `AuditGapPolicy`. Neither
  response marks the effect as failed or makes retrying it safe.
- Added append-only boundary dispositions with typed evidentiary bases.
  A disposition explains an unresolved boundary without fabricating its
  missing terminal or changing the reconstructed lifecycle result.
- Added deterministic, copy-on-write migrations for stage outputs and private
  memory. Mandatory dry-runs derive canonical manifests from durable evidence,
  preserve both diagnostic axes, ignore unreferenced orphans, reject stale or
  branched lineages, preserve transitive trust, and replay destination writes
  idempotently after an interrupted attempt.
- Added reusable adapter and phrasing-expert templates. The adapter template
  fails closed until its provider call and result mapping are implemented and
  names the provider SDK rather than an unavailable SETT package extra.
- Added `.github/workflows/ci.yml`. Every declared Python version runs the
  suite, the warnings-as-errors suite, the pinned Ruff configuration,
  `compileall`, the import sweep, and `pip check` on Linux and Windows, with
  macOS covered once on the newest interpreter. A separate job builds the
  wheel and the source distribution, installs the wheel into an empty
  environment, imports `SETTOrchestrator` from outside the source tree, and
  builds the release archive.
- Added `tools/import_sweep.py`. It imports every module in the package with
  no optional provider SDK installed and reports the roster rather than
  stopping at the first failure. The audit ran this by hand; it now runs in
  every matrix cell.
- Added `MANIFEST.in`. setuptools' default source distribution carried the
  package and the tests and nothing else: no `docs/`, no `templates/`, no
  `tools/`, no `examples/`, no changelog. That default contradicted both the
  documented install path and the L-02 disposition below, so the sdist now
  carries the source of the project rather than only its importable half.
  The wheel is unchanged: only `sett*` is packaged there.
- Added `tests/test_supported_python_versions.py`. The classifiers, the
  `requires-python` floor, the Ruff target version, the CI matrix, the README
  badge, and the getting-started requirements must agree. Declaring a version
  and testing it are now one act, because doing only the first fails the
  suite.
- Added `docs/foreign_callers.md`, the stable contract for code SETT does not
  own. An external tool loop reaches governance through
  `submit(Action(action_type, payload))`, whose only types are a string and a
  JSON-safe dictionary, so no SETT type crosses into the caller and no caller
  type crosses into SETT. A refusal is an exception rather than a return
  value, so a caller that discards the result still cannot proceed past it.
- Added `examples/foreign_tool_loop.py` and `examples/governance_card.py`.
  The first governs a dependency-free stand-in for any tool-calling framework;
  the second exports one real run as a standalone JSON and HTML record of what
  was permitted, what was refused, and by which layer.

### Changed

- Moved `SETTExecutor`, `HandlerContext`, and `current_handler_context` to
  `sett.execution_ruler`. Their top-level and historical
  `sett.core_ruler.executor` imports remain identical compatibility reexports.
  Action, lifecycle, execution context/results, idempotency, and resilience
  remain kernel contracts. Authorization establishes the new authority pillar;
  Execution follows so the effect boundary can depend on it without a Core
  runtime dependency on Authorization.
- `SETTOrchestrator` accepts an optional backend and explicit persistence
  policy. The v0.14.0 durable execution integration is deliberately limited to
  `run_pipeline_controlled()`; other orchestrator entry points reject a
  configured backend before application work because they do not expose audit
  closure.
- `SETTAgent` accepts an optional `private_memory_id`, and `PrivateMemory`
  accepts the corresponding optional `memory_id`. Both changes are additive;
  omitting them preserves ephemeral behavior.
- `ExecutionResult` now carries `audit_closure`, defaulting to
  `NOT_REQUESTED`. Existing construction and non-persistent execution retain
  their previous status, value, and exception semantics.
- When durable evidence is configured, trace event vocabulary and frame size
  are bounded before a lifecycle boundary can open. Limits are 128 bytes for
  kind and component type, 1,024 bytes for component name, 64 bytes for
  status, 32 reason codes, 256 bytes per reason code, 4 KiB for their aggregate,
  16 KiB for attributes, and 32 KiB for the complete frame. Omitting
  persistence preserves the previous ephemeral event-vocabulary behavior.
- The documented concurrency roster now contains ten narrow guarantees.
  `InMemoryEvidenceStream`, `InMemoryStateRecordStore`, and
  `SQLitePersistenceBackend` join the five existing components after
  concurrent stress coverage. `InMemoryAuthorizationReceiptStore` and
  `InMemoryAuthorizationEvidence` additionally protect atomic receipt
  consumption and their own evidence sequence, not application policies.

### Fixed

- Distribution metadata now uses the PEP 639 SPDX form: `license = "MIT"`
  and `license-files = ["LICENSE"]`, with setuptools 77 as the minimum build
  backend. The deprecated license table and license classifier were removed
  before their announced 2027 enforcement date; the framework's MIT license
  and bundled license text are unchanged.
- Release ZIP entries now declare a fixed Unix `create_system` alongside
  their fixed timestamp and POSIX mode. A pre-release rebuild on another
  operating system exposed identical content with different host metadata.
  Regression coverage compares builds under both Windows and Unix defaults;
  byte reproducibility still requires identical source bytes and a matching
  Python/zlib compression toolchain.
- The release builder now parses command-line arguments: `--help` prints
  usage without writing an archive, and unknown or excess arguments fail
  before writing. Output inside the source tree and option-like root artifacts
  are rejected before opening the destination, preventing self-inclusion and
  accidental packaging of an earlier `--help` artifact.
- Corrected Convention #21's historical wording: v0.13.2 was unpublished when
  the Pocket TTS exception was approved, but was subsequently published. The
  exception's original rationale and scope are unchanged.
- During joint integration, an adversarial exporter test exposed a mutable
  action-identity gap: a callback could change the action type after handler
  selection and cause authorization to evaluate a different route. The
  configured authorization path now snapshots action identity before
  callbacks, while payload copying still waits for successful route preflight.
- Resolver payload matching now checks both structural equality and the
  existing strict JSON reference representation. Python equality alone let
  boolean/integer, integral-float, and signed-zero substitutions change what
  policies inspected while the handler retained the original snapshot.
  Local snapshot preservation remains distinct from the deliberately
  normalized receipt fingerprint used across process boundaries.
- Release-tooling comments no longer describe SETT as an unpublished project.
- Generalized inherited adapter provenance notes that still named a private
  prototype and its archive path.
- A durable terminal whose requested metadata exceeds the frame contract now
  records an in-contract terminal with the original outcome and
  `audit.terminal_metadata_rejected`. It carries only framework-owned fields and
  discarded-item counts, so invalid terminal metadata cannot leave an already
  opened lifecycle boundary permanently unverifiable. Recovery reports expose
  the affected terminal event IDs separately from trace completeness.
- Completed the source-installation correction begun in v0.13.2. Optional
  adapter module documentation, runtime dependency errors, API examples, and
  test documentation now point to editable extras from a source checkout
  instead of naming an unavailable PyPI package. The external adapter template
  names the provider SDK directly because copied adapters do not belong to
  SETT's package and cannot define SETT extras.
- Corrected `result_digest()` documentation that described an unkeyed digest
  as unable to reveal its input. Low-entropy outcomes can be recovered through
  candidate guessing, and equal outcomes remain correlatable. The helper is
  limited to idempotency comparison and is not state-integrity evidence.
- Corrected the public ethics description: default numeric harm scoring comes
  from `ContextAnalyzer` heuristics or a registered domain analyzer.
  `HarmCategory` weights and the rules list are inspectable configuration
  metadata; they do not feed the numeric score or emitted audit entries.
- `SQLitePersistenceBackend.open()` now returns an object accepted directly by
  `SETTOrchestrator`. The component contracts had passed independently, but an
  inheritance mismatch made the documented integrated construction fail.
- `import sett` failed on Python 3.10 while the project declared support for
  it. `sett/persistence/sqlite.py` imported `typing.Self`, which exists from
  3.11, so the entire package, not only the SQLite backend, was unimportable
  on a declared interpreter. Nothing caught it because every test ran on 3.13.
  See the audit-remediation section below for the support window that replaces
  the claim and the matrix that now proves it.
- `sett/core_ruler/lifecycle.py` carried a `# noqa: PYI034 - Python 3.10
  support` suppression whose stated reason no longer exists. `__enter__()`
  returns `Self` and the suppression is gone.
- The release archive copies file bytes verbatim, so `.gitattributes` never
  governed it: a maintainer editing on Windows could ship CRLF into an archive
  whose reproducibility the release tooling claims. This was found by causing
  it while preparing this release. `tests/test_build_release_zip.py` now fails
  on any release file carrying CRLF.

### Defensive and infrastructure audit remediation

A defensive and infrastructure audit was run against the v0.14.0 candidate
before release, covering defensive behavior, packaging, clean installation,
and the new surfaces. It reported no critical or high finding, three medium
findings, and one low finding. Every one is dispositioned here, and the
findings keep their original identifiers so the report and this entry can be
read against each other.

- **M-01, SQLite infrastructure errors were not normalized.** Some native
  SQLite failures escaped `SETTPersistenceError`, so an application catching
  the framework's own structured persistence failure missed failures produced
  by the framework's own reference backend. Opening an unwritable path raised
  a bare `sqlite3.OperationalError`. Every backend operation now translates
  `sqlite3.Error` into `SETTPersistenceError` with a stable
  `persistence.sqlite.<operation>_failed` reason code, the attributable
  `backend_id`, and the driver's exception preserved as `__cause__`. The
  translation catches `sqlite3.Error` only, so `SETTIdempotencyConflictError`,
  `SETTPersistenceContinuityError`, `SETTPersistencePositionError`,
  `SETTConfigurationError`, `SETTValidationError`, and the
  `state.record_missing` reason code keep both their types and their meaning.
  The public message is one stable sentence and carries neither the driver's
  text nor the database path.
- **M-02, SQLite files were created world-readable.** Documented rather than
  papered over. `docs/security_model.md` gains a section stating that SETT
  sets no filesystem permissions and that protecting the bytes is the
  deploying application's responsibility, with the modes observed on POSIX
  and on Windows and a four-step deployment procedure. The technical point
  that makes the guidance actionable is that the directory, not the file, is
  the control point: the `-wal` and `-shm` sidecars belong to SQLite's
  open/close cycle, a clean `close()` removes them, and the next `open()`
  recreates them, so permissions applied to a sidecar do not survive a
  restart while permissions applied to the containing directory are inherited
  by every file SQLite later creates. No best-effort `chmod` was added,
  because one applied at open time would miss the sidecars, would not survive
  their recreation, and would not be an ACL policy on Windows: it would read
  as protection while providing close to none. `PrivateMemory` now says in
  its own docstring that its name describes an architectural position and not
  a storage property.
- **M-03, no automated matrix for the declared Python versions.** Closed, and
  it immediately earned its cost. The first run on a second interpreter found
  that `import sett` had never worked on the declared 3.10 (see Fixed). The
  declared window is now Python 3.11 through 3.14: 3.11 is the floor the code
  has actually required since durable persistence landed, and 3.14 is added
  because it is current and was missing. This narrows a claim that was never
  true rather than dropping working support, and 0.14.0 is unpublished, so no
  installed release loses an interpreter it had. The window is now enforced
  from both sides: CI runs it, and a test fails if the classifiers, the
  floor, the lint target, the matrix, and the public documentation ever stop
  agreeing.
- **L-01, adapter messages could carry provider text.** Fixed at the single
  point every adapter funnels through. `normalized_adapter_error()` builds
  the public message from controlled identifiers only, so it reads
  `azure_speech synthesize failed (authentication)` and never embeds the
  provider's own exception text. The previous behavior truncated that text to
  200 characters, which bounded its length and nothing else: the audit's
  synthetic bearer token survived truncation intact and was recoverable from
  `logger.error("%s", error)`. The provider's message is not lost; it stays
  on `__cause__`, where a local traceback still shows it in full.
  `bound_provider_error_message()` remains public and unchanged for an
  application that deliberately chooses to embed bounded provider text, and
  its docstring now records why the framework no longer does.
- **L-02, template distribution.** The templates themselves were already in
  this release; what was open was where they are reachable from. They are
  repository and source-release resources: present in the checkout, the
  release archive, and the source distribution, and deliberately absent from
  the wheel. A template is a file you copy and edit until it is your code,
  not a module SETT imports. Making that statement true required
  `MANIFEST.in`, because the default sdist carried neither the templates nor
  the documentation. `templates/README.md` states the access path, and a test
  asserts the release archive still carries all four.

Two claims in the audit's own baseline needed correcting rather than
accepting. Ruff had not been run there, because the audit environment had no
network to install the pinned version; it has now been run, and Ruff 0.16.0
passes on the whole tree. And the reported 1,230 passing tests required
`requests` to be importable, which nothing declared: on a genuinely empty
environment 48 tests failed for a missing package rather than reporting the
credential rules they exist to check. `requests` is now part of the `dev`
extra, with a comment saying it is a test-environment requirement and not a
framework dependency. The core still installs with none.

### Compatibility

- Persistence and authorization are opt-in and add no mandatory dependency.
  SQLite uses the Python standard library. Existing applications that omit
  both retain the v0.13.2 execution paths and do not need new arguments.
- No v0.13.2 public symbol was removed or renamed. New persistence contracts
  are stable in `sett.persistence`; recovery and integrity contracts are
  stable in `sett.audit_ruler`. Structured persistence and audit-closure
  exceptions are also re-exported from `sett`.
- Supported Python versions are 3.11, 3.12, 3.13, and 3.14. `requires-python`
  is `>=3.11`. v0.13.2 declared `>=3.10`, but `import sett` never worked
  there once durable persistence landed, so this corrects an untrue claim
  rather than withdrawing support that existed. An application already
  running on 3.11 or newer needs no change.
- `SETTAdapterError.__str__()` no longer includes provider-supplied text
  (L-01). Structured attributes, `safe_view()`, reason codes, and `__cause__`
  are unchanged, and no code that follows convention #13 and reads attributes
  instead of parsing the message is affected. Code that asserted on provider
  wording inside an adapter message must read `__cause__` instead.
- Automatic pipeline resumption, retention, compaction, cross-namespace
  migration, import, production secret storage, `UniversalMemory` restoration,
  `ExecutionControlRegistry` restoration, and persistence of the existing
  `IdempotencyStore` through this backend remain outside v0.14.0.

### Validation

- Full suite: 1,255 tests passing, comprising the 1,038-test v0.13.2 baseline
  plus 193 tests for durable formats, restart recovery, audit gaps, boundary
  dispositions, stage and private-memory state, copy-on-write migrations,
  contract probes, SQLite reopen behavior, authorization contracts and
  fingerprints, joint effect barriers, import compatibility, concurrency, templates,
  release packaging, documentation, language, and confidentiality guards, plus
  17 tests added while closing the audit findings above: SQLite infrastructure
  error normalization and the domain failures it must not flatten, the
  adapter message contract under the audit's own bearer-token probe, the
  declared-versus-tested Python window, and release-archive content, plus
  7 tests for the foreign-caller contract: the plain-type boundary, refusal
  raised rather than returned, fail-closed unregistered and unattached paths,
  and a guard that no agent framework is imported anywhere in `sett/`.
- The suite passes on Python 3.11.16, 3.12.14, 3.13.15, and 3.14.7, with
  identical results under `-W error`. Each interpreter also passes
  `python -m ruff check .` with the pinned Ruff 0.16.0, `compileall`, and the
  import sweep at 92/92 modules with no optional provider SDK installed.
  These are executed results on the candidate, not a claim derived from one
  interpreter.
- Total coverage: 88% over roughly 8,400 executable lines. Areas most
  affected by this release: idempotency 97%, UniversalMemory 96%, ethical
  filter 95%, authorization store 94%, SQLite persistence 94% (up from 92%
  after M-01), authorization fingerprint 93%, executor 92%, trace 90%,
  migrations 78%. Migrations remain the recommended target for the next
  focused review, because they affect durable state and carry the most
  complex failure paths. A coverage percentage guides attention; it does not
  by itself demonstrate defensive correctness.
- Release checks: the wheel and the source distribution build, the wheel
  installs into an empty virtual environment, `pip check` reports no broken
  requirement, and `SETTOrchestrator` imports from the installed package with
  no source tree on the path. The release archive builds and carries the
  templates and the CI workflow.
- Not established by this release: live provider calls against any external
  service, an operating-system matrix beyond Linux, Windows, and one macOS
  cell, and any measurement of the durability declarations against real power
  loss. Those remain declarations verified from effective SQLite pragmas, not
  physical proof.

## [0.13.2] - 2026-08-21

Provider-neutral voice language consistency, audited error boundaries, and
optional Kyutai Pocket TTS support.

### Fixed

- `LocalSTTAdapter.transcribe()` now accepts `language_code`, matching
  `GoogleSTTAdapter` and `AssemblyAISTTAdapter`, and reduces a BCP-47 tag to
  the bare language identifier faster-whisper expects (`es-AR` becomes
  `es`). The historical `language` name remains a compatibility alias. When
  both names are present, equivalent primary subtags are accepted and a real
  conflict raises a structured configuration error instead of silently
  allowing one spelling to overwrite the other.
- `AzureTTSAdapter` and `AzureSTTAdapter` now accept `language_code` at
  construction and per call. A call such as `transcribe(...,
  language_code="es-AR")` reaches Azure as `language=es-AR` instead of being
  silently discarded in favor of the configured default.
- The canonical spelling covers every official STT adapter that offers
  language selection. Deepgram accepts `language_code` at construction and
  per call; Gladia and hosted Whisper accept it per call. Whisper reduces
  BCP-47 tags to the primary language identifier its provider expects.
- The historical `language` spelling remains a compatibility alias where it
  already existed. Equivalent effective tags are accepted; conflicts raise a
  structured `SETTServiceAdapterError` with
  `adapter.language_arguments_conflict` before provider work.
- `CartesiaTTSAdapter` joins Azure, Google, and LocalTTS in accepting per-call
  `language_code`, with the same compatibility and conflict behavior.
- `LocalTTSAdapter` now normalizes failures raised while creating the engine,
  reading voice metadata, or applying voice properties. These paths no longer
  leak provider exceptions outside SETT's error hierarchy.
- pyttsx3 language matching now understands the control-byte prefix emitted
  by older eSpeak metadata and underscore-separated locale tags. The
  `local-tts` extra now requires pyttsx3 2.99 because pyttsx3 2.90 does not
  expose SAPI5 language metadata on Windows, making honest language selection
  impossible there.
- Local TTS and STT paths preserve cancellation and deadline exceptions after
  provider work instead of translating them into service-adapter failures.
- `ContextAnalyzer.EMOTIONAL_RISK_MODIFIERS` is now an immutable mapping.
  One analyzer instance can no longer change the risk score produced by every
  other instance through shared class state.

### Added

- `LocalTTSAdapter.synthesize()` now accepts an optional `language_code`
  keyword argument, matching `GoogleTTSAdapter`. Without an explicit
  `voice_id`, it looks for an installed host voice whose own metadata names
  the requested language and leaves the current voice untouched when none
  does. It deliberately does not guess from free-form voice identifiers.
- `PocketTTSAdapter` adds optional Kyutai Pocket TTS 2.1 support through the
  existing `TTSBase` contract. It lazily loads a caller-selected language and
  voice, accepts the conditioning forms supported by Pocket TTS, and returns
  mono PCM16 WAV bytes. The adapter declares six language families and voice
  cloning.
- Pocket TTS generation is exposed honestly as non-streaming because
  `TTSBase.synthesize()` returns one complete byte payload. The underlying
  model's streaming API does not change SETT's current contract.
- One adapter instance serializes model loading, voice conditioning, and
  generation because Pocket TTS documents its generation methods as not
  thread-safe. The package and model remain optional; first use can download
  model weights or catalog voices, while cached assets can synthesize without
  a remote service.

### Maintenance

- SETT now owns an explicit Ruff 0.16.0 policy in `pyproject.toml`. Repository
  validation no longer depends on whichever default rule set an installed
  Ruff version happens to provide. The full tree passes with zero findings.
- Imports, annotations, unused test values, broad exception assertions,
  intentional Unicode fixtures, and context-manager structure were cleaned
  across the repository. Deliberate audit-boundary catches remain in place
  with narrow inline explanations.
- Thirteen test modules now use stable, version-independent filenames. Their
  names describe the contract under test, so future releases update the same
  files instead of creating parallel version-labelled suites.
- The adapter deadline test now exercises the adapter and verifies the actual
  `0.0` effective timeout contract. The environmental-threshold test now pins
  the real `WARN` to `REJECT` transition instead of leaving its baseline
  verdict unchecked.
- LocalSTT now delegates language aliases, conflicts, primary-subtag handling,
  and lowercase normalization to the shared `_language.py` resolver instead
  of maintaining a second copy of the same rule.
- Dedicated post-provider tests now prove that LocalTTS and LocalSTT preserve
  `SETTCancelledError` and `SETTTimeoutError` rather than translating them into
  service-adapter failures. Separate fixtures identify legacy eSpeak control
  bytes and macOS nsss underscore locales independently.

### Documentation

- `README.md` and `docs/getting_started.md` now document source installation
  instead of presenting SETT as available from PyPI, where it is not published.
- Removed the unpublished draft Convention #24, which proposed a repository
  `checksums.txt` practice that was never present in the released v0.13.1
  archive. This entry records the governance edit without presenting the draft
  as a released guarantee.

### Compatibility

- No public symbol was removed or renamed. Existing callers using `language`
  continue to work where that spelling already existed; `language_code` is
  the canonical spelling for provider-neutral call sites.

### Versioning decision

- `PocketTTSAdapter` is additive public API in the
  `sett.services_tts_stt` submodule, so Convention #21 would ordinarily place
  it in a minor release. The maintainer explicitly approved a one-time
  exception for this still-unpublished v0.13.2 patch: Pocket TTS became known
  only days before the release was closed and addressed an immediate need for
  a local CPU TTS option. Keeping the completed work in the same unpublished
  patch avoids manufacturing two public releases for one review cycle. This
  decision does not redefine the stable API boundary and does not relax the
  default rule for future additive public APIs.

### Validation

- 1,038 tests pass, 0 skipped. The 1,003-test v0.13.1 suite remains intact;
  the 35 release tests cover provider-neutral language selection, aliases and
  conflicts, BCP-47 normalization, host-voice metadata, normalized failures,
  post-provider cancellation and timeout preservation, Pocket TTS contracts,
  lazy loading, WAV encoding, voice-state reuse, serialized concurrent calls,
  and immutable emotional risk modifiers.
- SETT keeps zero mandatory dependencies. `pocket-tts>=2.1.0,<3` is available
  through the `pocket-tts` extra and is included only in the opt-in `all`
  extra.

## [0.13.1] - 2026-08-19

Fail-closed routing and embedding-overhead patch, plus release-audit cleanup.

### Origin

A frozen comparative benchmark exposed avoidable steady-state action latency
in SETT's governed execution path. Follow-up scenario-level profiling and
repeated ablation isolated two confirmed contributors: synchronous
`logging.lastResort` writes when the host application had not configured
logging, and governance work performed before handler availability was known.
The full protocol, comparisons, and measurements belong in the separate
benchmark report; this entry records the release-relevant causal findings.

### Changed

- Importing `sett` now installs a package-level `logging.NullHandler` without
  configuring the root logger. Applications that do not configure logging no
  longer fall through to Python's synchronous stderr `lastResort` handler;
  application-installed handlers still receive propagated records normally.
- `SETTExecutor.submit()` resolves the exact handler before copying payloads,
  reading environmental context, or invoking the `EthicalFilter`. An
  unroutable action now emits `action.blocked` with the normalized verdict
  `block` and reason `handler.not_registered`, then raises a structured
  `SETTConfigurationError`. It does not create a false ethical verdict or a
  false handler failure.
- `SETTExecutor.submit_controlled()` now performs the same readiness and route
  preflight before payload snapshots, fingerprints, idempotency reservations,
  or handler-attempt events. An impossible action therefore cannot consume an
  idempotency key or manufacture an attempt that never reached a handler.

### Fixed

- The concurrency reference now counts and names its five narrow thread-safe
  exceptions consistently, including `UniversalMemory` in the practical
  deployment guidance.
- `CartesiaTTSAdapter` now reports a blank `voice_id` as a structured
  `SETTServiceAdapterError` with configuration metadata instead of leaking a
  native `ValueError`.

### Documentation

- Corrected the v0.13.0 changelog entry to include release-audit changes
  that were already present in the published v0.13.0 source and release
  notes but were mistakenly carried into the draft v0.13.1 entry. This
  is a historical-record correction; no v0.13.1 behavior is attributed
  to those earlier changes.
- README.md's Install section documented `pip install sett-framework`
  as though the package were available on PyPI. It is not, and there is
  no near-term plan to publish it. The section now documents installing
  from source instead.

### Performance interpretation

- The logging change directly affects emitted WARNING and ERROR records. The
  early routing changes directly affect unroutable and unready actions.
  Neither removes work from a registered ALLOW path, so this release does not
  attribute an aggregate ALLOW-path improvement to these changes without a
  separate, isolated measurement.
- The ablation also observed an approximately 7% improvement on registered
  ALLOW-shaped paths, even though those paths should not invoke either
  corrected behavior. That result remains unexplained and is excluded from
  the patch's claimed effects pending isolated investigation.

### Validation

- 1,003 tests pass, including the complete inherited suite and regressions
  proving that unroutable or unready controlled submissions do not copy
  payloads, evaluate policy, reserve idempotency keys, or open handler
  attempts.

## [0.13.0] - 2026-08-16

Provider-neutral adapter contracts, honest capability selection, passive
health, structured failures, opt-in resilience, and an expanded voice roster.

### Added

- Immutable `AdapterCapabilities`, `AdapterRequirements`, `AdapterHealth`,
  `AdapterErrorView`, `AdapterHandle`, and `AdapterSelection` contracts, with
  tri-state capability support and detached JSON-safe views.
- Pure `select_adapter()` filtering with stable rejection reasons, passive
  health and circuit-state checks, and caller-owned tie-breaking through
  `SelectionPolicy` / `PrioritySelectionPolicy`. SETT ships no provider
  default or recommendation.
- `SETTAdapterError` as the structured common parent of the existing LLM and
  service adapter errors. Official adapters normalize authentication, quota,
  timeout, unavailable, invalid-response, unsupported-capability, and
  configuration failures while preserving provider exceptions as causes.
- `CircuitBreaker`, `CircuitBreakerPolicy`, `CircuitState`, and
  `execute_with_resilience()` in `sett.core_ruler.resilience`. Retry and
  circuit breaking remain off by default; retry additionally requires an
  explicit idempotency statement.
- Sanitized adapter call, health, retry, circuit, and fallback-selection trace
  events. Prompts, audio, responses, credentials, and raw provider messages
  are excluded.
- Remote voice adapters for Deepgram STT, Azure Speech TTS/STT, AssemblyAI
  STT, Gladia STT, OpenAI-hosted Whisper STT, and Cartesia TTS.
- Local voice adapters for pyttsx3 TTS, faster-whisper STT, Kokoro TTS, and
  Piper TTS. Model loading is deferred until first use where applicable.
- `EdgeTTSAdapter`, explicitly marked community/unofficial because it does
  not use the supported Azure Speech API.
- Dependency-free `sett.testing.adapter_contract` checks for third-party
  adapter capability, health, cancellation, error, and secret-safety
  contracts.
- `docs/adapters.md` and `examples/adapter_selection.py`.

### Changed

- `LLMBase`, `TTSBase`, `STTBase`, and `SentimentBase` now provide concrete
  conservative `capabilities`, `health`, and non-billable `check_health()`
  members. Existing v0.12 third-party subclasses require no changes.
- All eight existing official adapters declare only the behavior implemented
  by their current synchronous path and feed passive health/tracing.
- Retry implementation moved to `sett.core_ruler.resilience`; the old
  `sett.core_ruler.retry` module remains as a compatibility re-export.
- Deadline duration exports retain their configured upper bound despite
  floating-point cancellation at large monotonic-clock values.
- `CircuitBreaker` is now included explicitly in the documented concurrency
  guarantees; its failure accounting and one-time open transition are covered
  by an 8-thread, 2,000-failure stress test.
- The living adapter and execution-context design documents now use stable
  canonical paths (`docs/adapter_contract.md` and
  `docs/execution_context.md`). Introduction and review versions live in each
  document's metadata instead of its filename, preventing parallel contracts
  in future releases.
- Prose now favors commas, semicolons, colons, parentheses, or sentence breaks
  over dash-shaped pauses. Arithmetic operators, Markdown list markers,
  changelog dates, ranges, glossary separators, and tool directives retain
  their functional symbols.
- OpenAI, Anthropic, and Gemini client construction now preserves SETT-owned
  adapter, cancellation, and timeout exceptions unchanged, matching their
  real call paths, while provider construction failures remain normalized.

### Fixed

- Late failures from calls already in flight no longer move a circuit's OPEN
  timestamp or postpone recovery. A failed HALF_OPEN probe still starts a new
  recovery timeout.
- The internal execution-context conformance helper `_is_uuid()` now imports
  `UUID` instead of raising `NameError` when called directly.

### Validation

- 995 tests pass, including the complete inherited suite and new v0.13
  contract, resilience, credential, tracing, and voice-adapter coverage.

## [0.12.0] - 2026-08-10

Lifecycle management: cancellation, deadlines, structured outcomes and
idempotency for executions already in flight, together with the
hardening that release auditing of this version turned up, several
pieces of which fix behaviour that had been wrong since well before it.

### Added
- `ExecutionControl`, `ExecutionControlRegistry`, `CancellationToken`,
  `CancellationSource`, `CancellationSnapshot` and `Deadline` in
  `sett/core_ruler/lifecycle.py`. A control carries its own
  `ExecutionContext`; subtree cancellation is resolved by walking the
  `parent_id` chain that context tree already maintains, so no second
  hierarchy is introduced and the `run_id` visible in an exported trace
  event is the identifier that can be cancelled.
- Cooperative cancellation with LIFO cleanup hooks, monotonic deadlines
  that a nested unit can shorten but never extend, `lifecycle_scope()`
  propagation via `contextvars`, and `LifecyclePolicy` for per-level
  timeout defaults: run, stage, agent, adapter and handler.
- `ExecutionStatus`, `ExecutionState` and `ExecutionResult` in
  `sett/core_ruler/execution_result.py`. Six terminal statuses that keep
  an ethical rejection, a cancellation, a deadline and a technical
  failure apart. `ExecutionResult.unwrap()` re-raises the original
  exception instance, preserving its concrete type and attributes.
- `SETTOrchestrator.process_controlled()` and
  `run_pipeline_controlled()`, added alongside the existing entry points
  in the same way `process_traced()` was in v0.11.0. Partial pipeline
  work is reported as `PARTIAL` with the completed stages' outputs still
  reachable, never discarded.
- `SETTOrchestrator.cancel(run_id)`, `control_registry` and
  `lifecycle_policy`; `SETTOrchestrator(lifecycle_policy=...)`.
- Automatic checkpoints at the already-instrumented boundaries (routing,
  agents, registered experts, pipeline stages), so no existing agent or
  expert subclass needs to change to become cancellable.
- Idempotency in `sett/core_ruler/idempotency.py`: `action_fingerprint()`
  with numeric canonicalization, `IdempotencyStore` protocol,
  `InMemoryIdempotencyStore` reference implementation, `IdempotencyStatus`,
  `IdempotencyRecord`, `IdempotencyReservation`, `ActionAttempt` and
  `key_digest()`.
- `RetryPolicy`, `ErrorClass` and `classify_error()` in
  `sett/core_ruler/retry.py`. Retries are opt-in per error type; a set of
  framework errors is never retried regardless of policy.
- `SETTExecutor.submit_controlled()`, `register_controlled_handler()` and
  `HandlerContext`, carrying the idempotency key and the run's real
  remaining time into a handler. `submit()` is wrapped, not
  reimplemented.
- Reserved adapter keyword arguments `timeout` and `cancellation_token`,
  with `pop_lifecycle_kwargs()`, `lifecycle_kwargs()` and
  `AdapterLifecycle`. Honoured by every official adapter.
- Per-instance Google Cloud credentials (`sett/services_google_auth.py`):
  the TTS, STT and sentiment adapters no longer set
  `GOOGLE_APPLICATION_CREDENTIALS` process-wide, where two differently
  configured instances used to interfere with each other and with every
  other Google client in the process.
- Exceptions `SETTCancelledError`, `SETTTimeoutError`,
  `SETTDependencyError` and `SETTIdempotencyConflictError`, all under
  `SETTError`.
- `SkipCause` on `StageOutcome`: a stage that never ran now names the
  stage that made it unreachable, instead of only reporting that it did
  not run.
- Trace terminals for cancellation, deadlines and partial results at
  every level, plus `handler.attempt_started`/`_completed`/`_failed` and
  the idempotency events. `TraceRecorder.knows_event()`.
- Eleven new test modules.

### Fixed
These were wrong in v0.11.0 and earlier.
- A non-finite `risk_score` from a `ContextAnalyzer` reached `ALLOW`.
  `nan >= threshold` is False in Python, so a single NaN cleared the
  reject threshold and the warn threshold and arrived at an allow verdict
  with no rule having allowed anything, a fail-open in the layer this
  framework exists to make fail-closed. `ContextAnalysis` now refuses a
  score that cannot be compared, at the one type every analyzer must
  return.
- Policy could approve one payload while the handler performed another.
  `SETTExecutor.submit()` read the caller's `Action` twice: once to
  build the filter's context and once to call the handler; and
  `UniversalMemory.update()` read the published result twice. A thread
  still holding the object could change it in between. Both now read once
  at the door and derive everything from that single copy.
- A domain beginning with `__env_ctx__` collided with the reserved
  environmental-context namespace: it was stored as an agent result,
  hidden from `read_all()`, and read back as an environmental context.
  The prefix is now reserved, and `ENV_CONTEXT_PREFIX` is public.
- `PrivateMemory.get_history()` returned the internal entries, so a
  reader could edit the record of a write that already happened. It is
  deep-copied now, like `read()` and `get_all()` already were.
- `ElevenLabsTTSAdapter` accepted a whitespace-only API key, because the
  `.strip()` covered only the environment lookup, and failed later as a
  provider authentication error instead of at construction.
- `RiskProfile` values could be published to `UniversalMemory` by an
  uncooperative agent, despite the documented boundary. The check is now
  structural, at the one place every publication passes through.
- Sensitive metadata and trace keys were matched by plain substring, so
  `apiKey`, `x-api-key`, `api.key` and `private/key` passed. Keys are now
  normalized to letters and digits before comparison, and one shared
  `is_sensitive_key()` serves both the context and the trace.
- Provider SDK exceptions escaped adapter constructors untranslated.
  `OpenAIAdapter`, `AnthropicAdapter` and `GeminiAdapter` built their
  client outside any `try`, and `build_google_client()` re-raised
  anything that was not exactly a `DefaultCredentialsError`, so
  `except SETTError` around "build my adapters" did not hold and the
  caller had to know each provider library's exception hierarchy to catch
  a failure. Every construction failure now leaves as
  `SETTLLMAdapterError` or `SETTServiceAdapterError` with the original
  kept as `__cause__`; SETT's own cancellations and deadlines still pass
  through with their type intact.
- Adapter timeouts skipped the validator this release promised covered
  them. `validate_duration_seconds()` was introduced so one rule would
  serve every duration, and the five adapters holding their own
  `timeout_seconds`, namely the three cloud LLM ones, `OllamaAdapter` and
  `ElevenLabsTTSAdapter`, were the ones it did not reach. An adapter
  could hold a `NaN` timeout, which compares False against every bound
  and so is never reached. They validate now, on the same terms as
  everywhere else.
- An invisible character was enough to lower a `ContextAnalyzer` score.
  Layer 1 matched keywords against the raw text, so `harm` written with a
  zero-width space inside it, or `delete` written in fullwidth
  characters, scored as ordinary prose. Text is now canonicalized first:
  format characters are dropped, then `NFKC` and `casefold` are applied
  through the public
  `canonicalize_for_keyword_match()`. What that cannot reach is stated
  under Known limitations rather than implied to be covered.
- `OllamaAdapter` let a malformed `base_url` escape as a bare
  `ValueError` from urllib, walking through the `SETTLLMAdapterError`
  protection every caller relies on, including `PhrasingExpert`, which
  answers with `fallback_text()` on exactly that type and so lost its
  fallback. A `base_url` that is not an absolute http(s) URL naming a
  host is now refused at construction, where a configuration mistake
  belongs, and the request path translates every failure rather than the
  three it happened to enumerate. SETT's own `SETTCancelledError` and
  `SETTTimeoutError` still pass through with their type intact.
- `HandlerContext.attempt_number` and the attempt log reset to 1 every
  time a caller resubmitted an action under the same `idempotency_key`
  after a retryable failure, instead of climbing the way
  `IdempotencyRecord.attempt_count` already did for the same key. A
  handler or an operator reading the log saw every resubmission reported
  as a first attempt. Both now read the store's own count for a keyed
  action; without a key, unaffected, since there is no cross-submission
  identity to count against.

### Changed
- `EnvironmentalContext.auto_notify_emergency` is now
  `should_notify_emergency`, attribute and `to_dict()` key. The old name
  implied the object notified somebody; it is a derived boolean saying
  whether the risk level warrants notification. **Breaking.**
- `register_expert()`, `register_handler()`,
  `register_controlled_handler()` and `register_analyzer()` refuse a
  duplicate instead of replacing it silently, which was almost always two
  different things unintentionally sharing one name. **Breaking.**
  `register_agent()` still replaces; that is how a `StubDomainAgent`
  gives way to the real implementation, but it now logs a warning when what
  it replaced was not a stub.
- Empty or whitespace-only agent domains, expert names and action types
  are refused at registration.
- `PrivateMemory.write()` stores a deep copy, so mutating the object you
  passed in no longer changes what was stored without an audit entry;
  `clear()` now records an audit entry too.
- `UniversalMemory` hands the `EthicalFilter` its own copy, so an
  analyzer that mutates what it is given cannot reach stored memory.
- Timeouts and durations reject `NaN` and infinity everywhere, through a
  single `validate_duration_seconds()`; `RetryPolicy.max_attempts`
  rejects fractions and booleans.
- An `Action` carrying an `idempotency_key` must have a
  JSON-serializable payload: a `repr()` is neither stable nor unique
  enough to detect a reused key.
- `_verify_terminals()` reads a single module-level `_TERMINAL_BY_START`
  table; a cancelled broadcast and a cancelled handler close their trace
  boundaries as cancellations rather than failures.
- `run_pipeline()` populates the new additive `StageOutcome` fields. Its
  control flow, its trace events and every pre-existing field are
  unchanged.
- Documented contracts that were true but unwritten: transitioning an
  unknown key in an `IdempotencyStore`, or releasing an unknown `run_id`
  from `ExecutionControlRegistry`, is a silent no-op rather than an
  error. Both are now stated on the public `Protocol` and on the concrete
  methods, and fixed by tests, so a later change to either has to be a
  decision instead of a drift. No behaviour changed.
- `docs/api_reference.md` covers every name in `sett.__all__`. Twelve had
  no mention at all: the cancellation views, the idempotency types,
  `action_fingerprint()` and the retry classification; the four
  lifecycle exceptions had no attribute tables. The concurrency section
  now also names `ExecutionControlRegistry` and
  `InMemoryIdempotencyStore`, which hold their own locks, and its
  description of the registry says what the implementation does: the
  parent relation is owned by `ExecutionContext`, and the registry keeps
  a derived children index over it as a lookup accelerator. A guard test
  keeps the document and `__all__` from drifting apart again.
- Every public class, method and function in the package carries a
  docstring, and every public `__init__` declares `-> None`. Dozens of
  getters, factories and constructors had neither. A guard test enforces
  both, for the reason every other guard here exists: a list fixed by
  hand once refills the same way it filled the first time.
- `docs/SETT_Conventions_v2.md` is now `docs/SETT_Conventions.md`. No
  version of SETT has been published yet, so nobody outside this
  project has ever linked to the `_v2` name; there is no external
  reader for whom a `v1` ever existed. Keeping the suffix would only
  invite the question of where that `v1` is. Every in-repo reference
  (code comments, docstrings, tests) was updated to the new name; the
  older `[0.11.0]` and `[0.7.0]` entries below still say
  `SETT_Conventions_v2.md` because that was its real name when those
  versions were written, and honest history doesn't get edited after
  the fact (Convention #16).

### Removed
- The per-version `MIGRATION_v*.md` guide, and the packaging logic that
  shipped the current one while excluding superseded ones. No version of
  SETT has been published yet, so there is no adopter for a guide to
  serve and no obligatory convention requiring one. The release archive
  now excludes `MIGRATION_*.md` unconditionally, whatever version it
  claims to document. Revisit this only if the project gains real
  external adopters who need one; until then, this CHANGELOG is the
  record of what changed and why.
- The per-release `AUDIT_vX.Y.Z.md` file. Only `AUDIT_v0.11.0.md` was
  ever produced under that name; no version before it had one, and this
  release does not restart the pattern. A single instance was never a
  convention to begin with, the same one-instance-becomes-a-rule
  mistake Convention #14 exists to catch elsewhere, applied here to
  documentation instead of code. This CHANGELOG entry is the record of
  what was verified for a given release and why; a second in-repo
  document restating the same information would duplicate it without
  telling a reader anything they could not already find here.

### Compatibility
- Two breaking changes, both listed above: the `should_notify_emergency`
  rename and the duplicate-registration guards.
- Everything else is additive, or a stricter refusal of input that was
  already invalid. Every new dataclass field is appended and defaults to
  `None`, so positional construction and equality of values written the
  previous way are unaffected.
- `process()`, `run_pipeline()` and `submit()` were deliberately NOT
  rewired to delegate to their controlled counterparts. That conversion
  belongs to a version where the new path already has a release behind
  it.
- Without a control, every checkpoint added at a boundary is unreachable:
  `current_execution_control()` returns `None` outside `lifecycle_scope()`,
  which only the controlled entry points enter.
- The 340 tests of v0.11.0 pass unmodified.

### Known limitations
- SETT cannot cancel an arbitrary non-cooperative Python handler. Three
  levels of guarantee are documented: cooperative code, a provider given
  the remaining time, and untrusted code that belongs in an isolated
  process.
- Exactly-once is not offered and not achievable against an external
  system. An in-memory idempotency store says nothing about anything that
  happened before the current process started, and says so in its own
  docstring.
- `bound_provider_error_message()` caps the length of a provider's error
  text; it does not redact it. Writing a redactor for arbitrary
  third-party text produces confidence rather than safety. Where a
  provider message must not appear publicly at all, raise a SETT error
  and keep the original as `__cause__`.
- The `timeout` forwarding of the SDK-backed adapters is exercised
  against stubs, which accept any keyword argument. `OllamaAdapter` and
  `ElevenLabsTTSAdapter` speak plain HTTP and are verified end to end.
- `ExecutionResult.value` is a reference, not a snapshot. The dataclass
  is frozen, which in Python means its fields cannot be reassigned and
  says nothing about the object a field points at: whoever else holds a
  mutable value can still change it. Same trade-off `Action` makes, and
  stated for the same reason; what SETT guarantees is that it will not
  change what it handed you, not that nobody can. Code needing a true
  snapshot takes its own `deepcopy()`.
- The public namespace of the submodules has no formal policy yet. Thirty
  five names without a leading underscore live in `sett.*` submodules
  without being in `sett.__all__`, and the question of which of those are
  supported API and which are internal detail belongs to the v1.0 freeze,
  not here. Deciding it now would freeze a boundary nobody has designed.
  Until then, treat `sett.__all__` as the supported surface and a
  submodule name as reachable but unfrozen.

  `ENV_CONTEXT_PREFIX` and `canonicalize_for_keyword_match()` are the
  concrete case, and are worth naming because this release calls both
  "public". What that means here is what they are: named, importable and
  documented in `docs/api_reference.md` with their real import paths,
  rather than literals buried in a module. It does not mean they were
  added to `sett.__all__`; promoting them to the root namespace would be
  settling one corner of the policy above ahead of the rest of it, which
  is exactly the sort of piecemeal decision the freeze exists to avoid.
  Whether they belong at the root is one of the questions v1.0 answers,
  with these two as the worked example.
- The naming convention for the runtime and configuration primitives this
  release introduces (`ExecutionControl`, `ExecutionControlRegistry`,
  `LifecyclePolicy`, `RetryPolicy`, `ExecutionState`/`Result`/`Status`)
  is not yet ratified in `docs/SETT_Conventions.md`. They violate nothing
  in Conventions #22 and #23; whether the `-Policy`, `-Registry` and
  `-Control` suffixes should become a named category with rules of its
  own is an open question for a later conventions update.
- Canonicalization does not defeat homoglyphs. Cyrillic `а` is a
  different letter from Latin `a`, not a different spelling of it, so
  `harm` written with one still misses the keyword. A hand-maintained
  confusables table is not offered, because an incomplete one produces
  confidence rather than safety. The generic keyword matcher is
  fail-honest scaffolding for an application that has registered no
  analyzer of its own; what guards a critical action is a domain
  analyzer registered with `register_analyzer()`, together with
  `SETTExecutor` resolving handlers by exact `action_type` and failing
  closed without one.

## [0.11.0] - 2026-07-29

### Added
- Immutable `ExecutionContext` with generated or caller-supplied
  `trace_id`/`run_id`, causal `parent_id`, UTC creation time, opaque
  application/instance/subject/session/turn identifiers, recursively frozen
  size-bounded metadata, and `derive()` for child operations.
- Run-local propagation using `contextvars`. Context is bound only while a
  component executes and is always reset, including after rejection and
  exceptions.
- `TraceEvent` and per-orchestrator `TraceRecorder`: thread-safe structured
  events, monotonic sequence, SHA-256 hash chain, immediate `cause_id`
  references, parent-run verification, sanitized export, and exporter hooks.
- End-to-end instrumentation for routing, broadcast, agents, registered
  experts, native pipeline stages, universal-memory publication, ethical
  decisions, action proposals, handlers, results, rejections, and errors.
- `SETTOrchestrator.get_trace()`, `export_trace()`,
  `register_trace_exporter()`, `verify_traces()`, and `last_trace_id`.
- `SETTOrchestrator.process_traced()` and immutable `TracedResult` for callers
  that need the result and trace identity together.
- Stable generated `Action.action_id` for causal effect correlation.
- `tests/test_execution_context.py`, covering context isolation,
  immutability, causal paths, privacy, compatibility, pipelines, broadcasts,
  actions, failures, and fail-closed exporter behavior.
- `docs/execution_context.md` and `MIGRATION_v0.11.md`.

### Security
- Traces record operational metadata only. Input payloads, action payloads,
  handler return values, exception messages/locals, biometric values, risk
  profiles, prompts, model responses, and private-memory values are excluded
  by default.
- Sensitive metadata keys and non-JSON objects are rejected. Metadata is
  bounded to 32 top-level keys, depth 8, and 16 KiB serialized.
- `handler.authorized` is the committed pre-effect boundary. A registered
  exporter failure there records `handler.blocked`, raises
  `SETTConfigurationError`, and never emits `handler.started` or invokes the
  real-world handler.
- Trace exports are defensive copies. Hash and causal verification detects
  mutation, removal, reordering, duplicate IDs, cross-trace/forward causes,
  invalid parent runs, and incomplete instrumented operations.
- Public recorder attributes are deeply immutable and enforce the same limits
  as execution metadata, including rejection of non-finite numbers, sensitive
  keys, unsupported values, excessive depth, key count, and serialized size.
- Pipeline validation, transformation errors, routing failures, expert/agent
  errors, and exporter failures now close every opened boundary with an exact
  terminal event and immediate causal link.

### Fixed
- `tests/test_elevenlabs_adapter.py` now guards with
  `pytest.importorskip("requests")`. Previously, a bare install (`pip
  install -e .`, without the `[elevenlabs]` extra) made these 12 tests
  FAIL instead of skip, contradicting the project's own documented
  install instructions. Retroactive fix, applied 2026-07-29, inherited
  unchanged from 0.10.1 and earlier, now fixed here and back-patched
  into every unpublished prior version (0.9.0, 0.10.0, 0.10.1).
- `docs/api_reference.md`'s Concurrency section now lists `TraceRecorder`
  as a second documented thread-safety exception alongside
  `UniversalMemory`. The guarantee itself was already real (`TraceRecorder`
  has held an internal `threading.RLock()` around `record()` since it was
  introduced above); the docs simply hadn't been updated to say so.
  Retroactive fix, applied 2026-08-01, found by an independent public-API
  audit.

### Compatibility
- `SETTAgent.process(input_data)`, `SETTExpert.resolve(context)`, existing
  executor handler signatures, `SETTOrchestrator.process()`, broadcasts, and
  pipelines remain source-compatible.
- Context parameters are additive and keyword-only at orchestrator entry
  points.
- The complete 0.10.1 suite passes unchanged.
- The private companion-assistant reference application's complete baseline
  suite passes against this source release without application code changes.
- SETT suite: 340 passed, including 50 execution-context/trace contract
  tests. Reference compatibility suite: 841 passed.

## [0.10.1] - 2026-07-28

Four rounds of documentation/consistency fixes, all found and fixed the
same day, before any of them were published, and squashed into this single
release rather than four separate ones (same reasoning as v0.7.0's own
three-pieces-in-one-release precedent: nothing here was ever shipped
individually, so there is no history to preserve by keeping them apart).

### Docs
- `docs/security_model.md` claimed `PrivateMemory` "intentionally retains
  normal Python object identity": true before v0.9.0, false since. That
  release changed `read()`/`get_all()` to return `deepcopy()`s (same
  hardening already applied to `UniversalMemory` in v0.8.0), but this
  paragraph was never updated to match. Found by an independent
  installation/upgrade validation exercise (not the usual API audit),
  cross-checking docs against actual behavior rather than symbol
  inventory. Corrected, with the "why" (what broke before the fix, same
  failure mode `UniversalMemory` already closed) spelled out instead of
  just flipping the claim.
- `docs/api_reference.md`: the `PrivateMemory` method table only said
  `get_all()` returns a "Copy" (ambiguous: shallow or deep?) and said
  nothing about `read()`. Both rows now say explicitly "deep copy (since
  v0.9.0)".
- `sett/services_tts_stt/base.py`'s module docstring had a paragraph in
  Spanish (citing a nonexistent "SETT_Convenciones_v2.md" naming entry)
  that does not exist in the actual published v0.7.0 source, a local
  corruption introduced at some point after that release, unrelated to
  any real decision. Confirmed against the real published file and
  removed; the docstring now ends where it originally did.
- Translated five Spanish example strings used as demo/test payloads
  for the sentiment and TTS/STT adapters (`sett/services_sentiment/google.py`,
  `sett/services_tts_stt/{google,elevenlabs}.py`,
  `tests/test_google_sentiment_adapter.py`,
  `tests/test_elevenlabs_adapter.py`, `tests/test_google_tts_stt_adapters.py`)
  to English, for consistency: SETT is a general-purpose framework and its
  own source should read in English throughout, regardless of the fact
  that these particular adapters are language-agnostic and would work
  identically with text in any language. No behavior change: these were
  arbitrary example values (several going through fully mocked adapters).

### Added
- `docs/SETT_Conventions_v2.md`: the framework's own conventions/invariants
  document, published for the first time. Every source citation of
  "Convención #N" / `SETT_Convenciones_v2.md` across this codebase
  (`CHANGELOG.md`'s v0.7.0 section, `sett/biometric_ruler/__init__.py`,
  `sett/ethics_ruler/ethic_kernel/context_analyzer.py`,
  `sett/services_sentiment/google.py`,
  `tests/test_google_tts_stt_adapters.py`) had pointed at this file since
  v0.7.0, but it only ever existed as a private, Spanish-language working
  note used to draft the arXiv paper, which never shipped, so every one of
  those citations was pointing at nothing for anyone outside this
  project. Translated in full and published as `SETT_Conventions_v2.md`;
  every citing site updated from "Convención #N" to "Convention #N" and
  from the old filename to the new one. Two internal-project-name
  mentions found while translating (referring to the companion-assistant
  application by name) were genericized before publishing, consistent
  with Convention #20, and the guard test below caught both on the first
  run.
- `tests/test_no_spanish_in_public_tree.py`: a new guard test, same
  spirit and structure as `test_no_internal_project_names.py`. Scans the
  public tree for text that looks like Spanish (accented vowels, the
  letter n-with-tilde, and inverted question/exclamation marks -
  characters Spanish uses constantly and English never uses natively)
  and fails if it finds any that isn't on an explicit, documented
  allowlist (the framework author's real name, the project's own
  "English, Español, 日本語" paper-language list, a couple of necessary
  illustrative example words/names, and one historical CHANGELOG entry
  quoting an already-fixed past mistake for the record). Motivated
  directly by the two Spanish-leak incidents above, both only caught by
  manual review this time; this test would have caught the first one
  automatically the moment the suite ran. Deliberately not a
  grammar-aware classifier: a careless Spanish phrase with zero accented
  characters is rare but theoretically possible and would not be
  caught; manual review before a release remains the backstop for that
  specific gap. Verified against a real regression: temporarily
  reintroducing the original Spanish-docstring mistake makes this test
  fail with the exact offending line reported.

### Compatibility
- Documentation and test-suite only. No code logic changed, no public
  API changed. 290 tests (up from 280; 10 new, all in
  `test_no_spanish_in_public_tree.py`).

## [0.10.0] - 2026-07-28

### Added
- `PhrasingExpert.verify_facts(phrased, facts, context) -> str`: an
  optional fourth hook, called after `phrased` already exists (from
  the LLM if configured and successful, from `fallback_text()`
  otherwise), letting a subclass validate the ACTUAL text against
  facts already known and swap it out if it contradicts them. Default
  implementation is a pass-through (`return phrased`), so every
  existing subclass keeps behaving exactly as before with zero changes
  required. `resolve()` now calls it as part of its template:
  `determine_facts -> _phrase -> verify_facts`.
  Motivated by a second-hand finding during an external audit: two
  independent subclasses in a downstream project each needed this
  same post-hoc check (a greeting that must never contradict the real
  time of day; a reply that must never assert the wrong name for the
  user) and, with no sanctioned hook for it, each had fully overridden
  `resolve()` just to insert one verification step, triggering the
  v0.9.0 override warning in the process. Same origin story as
  `PhrasingExpert` itself (see its class docstring): an unplanned
  repetition across independent call sites is the signal that
  something belongs in the framework, not copy-pasted per project.
- `SETTOrchestrator.register_executor(self, executor: SETTExecutor)`:
  added the missing type hint on `executor` (residual item from the
  v0.8.0 audit, never part of the 8 accepted fixes). Zero behavior
  change.

### Compatibility
- Fully backward compatible: `verify_facts()` is optional with a
  no-op default, and does not change `PhrasingExpert`'s existing
  abstract contract (`determine_facts`/`build_prompt`/`fallback_text`
  are still the only required overrides).
- 280 tests (up from 275).

## [0.9.0] - 2026-07-27

Eight decisions from a full public-API audit performed after v0.8.0's
first release (a symbol-by-symbol inventory, then a phased pass over
naming, signatures, exceptions, mutability, subclassing contracts,
documentation, concurrency posture, and import surface). Same
methodology as always: nothing accepted from a summary, every finding
reproduced against the actual source before deciding on a fix. All
eight were accepted; none were rejected.

### Added
- `SETTOrchestrator.verify_ethical_audit_log()`: delegates to
  `EthicalFilter.verify_audit_log()`, reachable from the orchestrator
  itself instead of requiring access to the private `_ethical_filter`
  attribute. Closes a real gap the audit found: v0.8.0's tamper-evidence
  guarantee was invisible to anyone using only the documented entry
  point.
- `SETTValidationError(SETTError, ValueError)`: deliberate multiple
  inheritance, the same pattern the standard library's own
  `json.JSONDecodeError` uses. `RiskProfile.__post_init__` now raises
  this instead of a plain `ValueError`; both `except SETTError` and
  `except ValueError` catch it. Not extended to `Action` /
  `BiometricReading` / `EnvironmentalContext`: no real case yet
  justifies validating them, noted as a candidate rather than applied
  speculatively.
- A non-blocking warning (logged, not raised) when a `PhrasingExpert`
  subclass overrides `resolve()` against its own documented "template
  method, do not override" contract. Fires at instantiation, and does
  not prevent the override from working: consistent with this
  project's "convention, not magic" stance elsewhere in the framework,
  while still surfacing a silent contract violation that previously
  left no trace at all.

### Fixed
- `PrivateMemory.read()` and `get_all()` returned mutable references
  into internal state: a caller mutating a nested value (a list or
  dict inside what was returned) could corrupt stored data without
  ever calling `write()`, leaving no trace in `get_history()`. Both
  now return `deepcopy()`s, the same treatment `UniversalMemory`
  already received in v0.8.0.

### Removed
- `sett/services_gen_ai/`: an empty scaffold module (a one-line
  docstring, no classes, no functions, not referenced by
  `sett/__init__.py`, any doc, or any `pyproject.toml` extra) since
  v0.1.0. Unlike `StubDomainAgent`, which is explicit, honest
  scaffolding, an unreferenced empty directory reads as an unfinished
  promise rather than an active placeholder. Will be recreated with
  real content if a concrete use case appears.

### Docs
- `api_reference.md`: documented `SafetyAssessment`, `ContextAnalysis`,
  `TTSBase`, `STTBase`, `SentimentBase`, `SentimentResult`, and
  `SentenceSentiment`: all public, exported, used in real tests, and
  previously undocumented. Added a field table for `EthicalRule`
  (previously a single usage-example line). Documented
  `EthicalFilter.verify_audit_log()` /
  `SETTOrchestrator.verify_ethical_audit_log()`, and added `sequence`,
  `previous_hash`, and `entry_hash` to both audit log field lists.
  Added an explicit Concurrency section: no thread-safety guarantee for
  the framework as a whole beyond `UniversalMemory`'s internal lock,
  which already existed in code but had never been written down
  anywhere a real adopter would read it.

### Validation
- 11 new tests covering all four code-level changes above
  (`tests/test_hardening_public_api_audit.py`), including the same
  adversarial mutation pattern already used for `UniversalMemory` in
  v0.8.0, applied now to `PrivateMemory`.
- Test suite: 275 passing tests (264 before this round).

### Compatibility
- Low risk, but not zero, so noted explicitly rather than folded
  silently into "Fixed": `import sett.services_gen_ai` now raises
  `ModuleNotFoundError` instead of importing an empty module. `RiskProfile`
  now raises `SETTValidationError` instead of a plain `ValueError`; existing
  `except ValueError` handlers keep working unchanged, since
  `SETTValidationError` is one. No other public signature changed.

## [0.8.0] - 2026-07-25

### Security and contract hardening
- `SETTExecutor.submit()` now fails closed unless the Executor is attached to
  a `SETTOrchestrator` whose `UniversalMemory` has an `EthicalFilter`.
- `SETTAgent._publish_to_universal()` and `propose_action()` now raise
  `SETTConfigurationError` when the agent is not wired, replacing silent
  no-ops that could hide invalid deployments.
- `UniversalMemory`, ethical audit logs, execution logs, and history expose
  defensive copies so callers cannot mutate internal state without a governed
  write.
- Memory history, ethical decisions, and executed-action logs use a sequenced
  SHA-256 hash chain and expose verification helpers. This is tamper-evident
  within the running process; it is not a cryptographic signature or durable
  external attestation.

### Safety semantics
- Added `SafetyAssessment`, separating `situation_urgency`,
  `action_harm_risk`, `omission_risk`, and `protective_action`.
- `human_at_risk` is now an urgency/audit signal rather than an implicit
  near-rejection score. Domain analyzers decide whether a proposed action is
  harmful; a severe human situation no longer automatically makes a
  protective action dangerous.
- During the same unreleased 0.8.0 cycle, a non-protective action that would
  otherwise be silently allowed while `human_at_risk=True` is promoted from
  `ALLOW` to `WARN`. Its harm score is not modified, protective actions remain
  unaffected, and the audit log records
  `human_at_risk_without_protective_classification` in
  `decision_reason_codes`.
- Exported `ContextAnalysis` and `SafetyAssessment` through the public API.

### Validation
- Added hardening regressions for unattached execution, missing governance,
  defensive snapshots, and hash-chain verification.
- Test suite: 254 passing tests before downstream integration validation.
- Declared Python 3.13 in package classifiers after successful validation.

### Compatibility
- This release intentionally changes previously silent behavior. Applications
  that instantiated agents or executors outside an orchestrator must wire them
  explicitly or expect `SETTConfigurationError`.

## [0.7.0] - 2026-07-23

Three additive, backward-compatible pieces, bumped together per
Convention #21 ("don't fragment two already-finished pieces of work
into separate releases if they're going to be published at the same
time") rather than as three separate releases. Not gated on the
remaining plan points of the
consumer application that motivated this work (multi-user
permissions, module-proposal self-improvement): per Convention #19,
finished work doesn't wait on decisions that have no date attached.

### Added
- `sett/services_tts_stt/base.py`: `TTSBase` and `STTBase`, the first
  concrete interfaces for the `services_tts_stt` module (previously an
  empty scaffold since v0.1.0). Same interchangeability contract as
  `LLMBase`: an Expert depends on the interface, never on a specific
  voice provider. Deliberately two separate interfaces, not one merged
  "voice" interface: a provider can implement only one (ElevenLabs has
  no STT product and gets no forced STT adapter).
- `sett/services_tts_stt/google.py`: `GoogleTTSAdapter` and
  `GoogleSTTAdapter`, backed by Google Cloud Text-to-Speech and
  Speech-to-Text. Ported from a recovered pre-SETT prototype
  (`listening.py`/`speaking.py`, Nov. 2024), reshaped to the stateless
  adapter contract: the original's UI coupling, audio playback, and
  concurrent-listen lock were left at the application layer on purpose
  (see STTBase's docstring for why).
- `sett/services_tts_stt/elevenlabs.py`: `ElevenLabsTTSAdapter`, backed
  by ElevenLabs' REST API directly via `requests` (no SDK dependency,
  same reasoning as `OllamaAdapter` using only stdlib for Ollama). Ported
  from an archived multi-engine TTS patch: only the ElevenLabs call was
  carried over, not the engine-selection/playback logic around it.
- New optional-dependency extras in `pyproject.toml`: `google-tts-stt`
  (`google-cloud-texttospeech`, `google-cloud-speech`) and `elevenlabs`
  (`requests`). Core framework still has zero mandatory dependencies.
- Convention #22 in `SETT_Conventions_v2.md`: service adapters are
  explicitly outside the Agent/Expert growth tree (#1): no
  `PrivateMemory`, no orchestrator registration, not a filter subject,
  and the naming taxonomy (`...Base`, `...Adapter`, `...Agent`/`...Expert`,
  `...Analyzer`, `SETT...Error`) is documented as a first-class
  convention rather than left as unwritten practice.
- 25 new tests: full mocked coverage for `ElevenLabsTTSAdapter` (HTTP
  mocked, same style as `test_ollama_adapter.py`) and for
  `GoogleTTSAdapter`/`GoogleSTTAdapter` (missing-dependency path made
  deterministic via `sys.modules`, plus full synthesize()/transcribe()
  behavior against an injected fake `google.cloud` module: the Google
  Cloud SDK itself is not installed in this environment, so this is the
  first test coverage a Google-Cloud-backed adapter has had in this
  project; Gemini/OpenAI/Anthropic still have none, unchanged from
  before). 213 tests passing (188 previous + 25 new).

- `sett/services_sentiment/base.py`: `SentimentBase`, `SentimentResult`,
  `SentenceSentiment`. Fills a slot that has existed since v0.1.1:
  `ContextAnalyzer.analyze()`'s `emotional_state` parameter was
  documented as "Detected emotional state (from Sentiment Analyzer)"
  before any adapter supplied it. Returns a raw signal (polarity score,
  magnitude, optional per-sentence breakdown): mapping it to a
  categorical `emotional_state` string is left to the application,
  same layering choice as TTSBase/STTBase staying free of playback/UI.
- `sett/services_sentiment/google.py`: `GoogleSentimentAdapter`, backed
  by Google Cloud Natural Language. Ported from the recovered
  `sentiment_analyzer.py` prototype's `analyze_text_sentiment()` only:
  its GCS/BigQuery storage and its `enhance_response()` Gemini call
  (text generation, not sentiment analysis) were both left out on
  purpose, not merely trimmed for size.
- `google-sentiment` extra (`google-cloud-language`) in `pyproject.toml`.
- Convention #23 in `SETT_Conventions_v2.md`: "ruler" is reserved for
  the framework's own foundational pillars (`core_ruler`, `ethics_ruler`,
  `memory_ruler`, `risk_ruler`), not for important domain Agents; this
  clarifies where it sits relative to #1 (Agent/Expert) and #22 (Adapter).
- 10 more new tests for `GoogleSentimentAdapter` (same sys.modules
  injection strategy as the TTS/STT Google tests). 223 tests passing
  (213 previous + 10 new).

- `sett/biometric_ruler/biometric_reading.py`: `BiometricReading`, a
  new structural pillar mirroring how `risk_ruler` holds
  `RiskProfile`/`EnvironmentalContext`. Extracted from
  `ContextAnalyzer._detect_human_at_risk`, which read
  `context["health"]`/`context["heart_rate_bpm"]` directly via ad hoc
  dict access: including the nested-vs-flat fallback added in v0.1.1
  to fix a real bug where an agent publishing flat biometric keys was
  invisible to risk detection. That parsing now lives in
  `BiometricReading.from_context()`, in one place instead of inline in
  the ethics layer, closing off the specific way a second call site
  could have reintroduced the same class of bug. Thresholds unchanged
  (150/40 bpm, 39.5/35.0°C): this is a relocation, not a
  recalibration. `_detect_human_at_risk`'s behavior is unchanged byte
  for byte; `test_ethics.py` was not modified and still passes.
- Convention #23 amended with the concrete reasoning for why
  `biometric_ruler` was built now while an `Emotion_Ruler` for
  sentiment was deliberately not: biometrics already caused a real,
  documented bug (v0.1.1), actual "extracted from use" (#15) evidence,
  while the sentiment adapter added in this same release has zero
  downstream consumers yet.
- 21 new tests for `BiometricReading` (parsing, thresholds, boundaries,
  serialization), independent of `ethics_ruler`. 244 tests passing (223
  previous + 21 new).

### Fixed
- Eight literal mentions of a consumer application's internal codename
  (in CHANGELOG.md, two adapter example docstrings, and three test
  fixtures) that had accumulated across this same release, in
  violation of Convention #20: found by a manual sweep ahead of
  preparing this release for public visibility. Docs/tests only, no
  public API touched: stays under this same 0.7.0, per Convention #21
  ("docs only → commit or patch, no new release if the package itself
  doesn't change").
- Second incident, found AFTER the fix above had already been pushed:
  a ninth mention (`services_tts_stt/elevenlabs.py`'s docstring,
  a file path containing the codename) had slipped past the new guard
  test because its `\b` (word-boundary) pattern treats underscore as a
  "word" character, so it never matched a codename directly adjacent
  to `_`. Fixed the docstring, and closed the gap in the pattern itself
  (letters-only boundary instead of `\b`) so this class of miss can't
  recur. 7 new regression tests fix both the catch (underscore/digit-
  adjacent variants) and the non-regression (no new false positives on
  unrelated words).
- `tests/test_no_internal_project_names.py`: new guard test, same
  spirit as `test_version_consistency.py`: scans the whole public
  tree for any casing of a consumer application's codename and fails
  the suite if one leaks in again. Deliberately excluded from the
  published tree via `.gitignore` (it necessarily names, in its own
  source, the codename it audits for): run it locally before every
  release, it is not part of the 244 tests below.

### Notes
- Motivated directly by the STT/TTS/sentiment gap identified while
  reviewing a recovered pre-SETT prototype and by the decision to close
  it before a v1.0.0 release: not by the "two independent instances"
  rule (#14), which this deliberately does not wait for; foundational
  I/O and perception infrastructure was judged not safely deferrable to
  after a public release, same reasoning already written into #12's
  origin (Ollama as zero-dependency default).
- What `GoogleSentimentAdapter` does NOT do yet, on purpose: decide what
  counts as "distressed" vs "anxious" for `EMOTIONAL_RISK_MODIFIERS`,
  or connect to `PhrasingExpert` so generation reflects the detected
  tone. Both are the next step (an application-level
  `SentimentAnalyzerAgent`, per Convention #23): this release is the
  adapter the slot has been waiting for, not the agent that will use
  it.

## [0.6.0] - 2026-07-22

### Added
- **Native pipelines**: `SETTOrchestrator.run_pipeline(steps, input_data, ...)`
  runs an ordered sequence of stages, each handled by a different
  registered agent, with explicit hand-to-hand data flow between stages.
  New types `PipelineStep`, `StageOutcome`, `RejectionOutcome` and
  `PipelineResult` (frozen dataclasses, exported from `sett`).
  Three guarantees define the mechanism:
  1. **Memory isolation between stages**: stage inputs are passed
     explicitly (previous stage's output, optionally reshaped by a
     per-step `transform`), never read from universal memory; each
     agent keeps its own `PrivateMemory`.
  2. **Fail-closed configuration**: every stage domain is validated
     before the first stage runs; empty pipelines and transforms that
     return non-dicts raise instead of silently no-oping. A
     misconfigured pipeline never produces partial side effects.
  3. **Rejection handling as part of the mechanism**: when the
     `EthicalFilter` rejects a stage, that agent publishes nothing,
     the remaining stages are skipped, and the rejection is returned
     explicitly in `PipelineResult.rejection` with the structured
     fields introduced in v0.5.0 (`action`, `score`, `threshold`,
     `principle`, `reasoning`). Applications no longer need to wrap
     each chain in hand-written try/except and pass the outcome to
     their synthesizer manually: the mechanism does it.

  Motivated by real downstream usage: an application built on SETT
  chains seven agents by hand in a routing function, preserving memory
  isolation between stages: the property that made flattening the
  chain into a single agent unacceptable. That hand-written chain was
  studied as the living specification for this API. Introducing native
  pipelines before v1.0 (rather than after) avoids conflicting with
  sequencing logic developers would otherwise write by hand against a
  stable API.

### Fixed
- Version consistency: `sett.__version__` had fallen one version behind
  `pyproject.toml` in the v0.6.0 preparation (it still reported the
  previous version). Both now read 0.6.0, and a new guard test
  (`tests/test_version_consistency.py`) asserts they always match, so
  this class of mismatch can never ship silently again.

### Notes
- 188 tests passing (19 for native pipelines, covering the three
  guarantees independently plus explicit verification that `process()`
  routing and broadcast behavior is untouched; 1 version-consistency
  guard).
- Purely additive: `process()` is unchanged; no existing code needs
  to change a line. Only a "halt" rejection policy is provided:
  a "continue" policy waits for a real downstream case to need it.

## [0.5.0] - 2026-07-21

### Added
- `SETTEthicalFilterRejectedError` now carries structured attributes:
  `.action`, `.score`, `.threshold`, `.principle`, `.reasoning`, in
  addition to its human-readable message. `str(e)` returns exactly the
  same message as before, byte for byte; the attributes expose the same
  data without string parsing. `.score` and `.threshold` are full-precision
  floats (the message renders the score rounded to 2 decimals).
  Motivated by real downstream usage, not theory: an application built
  on SETT needed the score and principle separately and had to parse
  `str(e)` by hand, which caused an actual bug: splitting the message
  on `"."` truncated a decimal score. All attributes default to `None`,
  so any code constructing the exception with only a message keeps
  working unchanged. 10 new tests covering both guarantees separately:
  message compatibility and structured access.

### Notes
- 168 tests passing. No breaking changes. The only `raise` site in the
  framework (`EthicalFilter.evaluate()` on REJECT) now passes the
  structured data alongside the unchanged message.

## [0.4.0] - 2026-07-17

### Docs (post-release correction, 2026-07-20)
- **Corrected note**: the originally published v0.4.0 entry of this
  changelog named specific private downstream projects built on SETT.
  Those references have been replaced with generic descriptions
  ("two independent projects", "an early prototype application",
  "a companion-assistant application"), in line with the repository's
  policy of not naming private projects in public documentation. The
  technical content of the entry is unchanged; noted here to keep the
  version history accurate rather than silently rewriting it.

### Added
- `EthicalFilter.register_analyzer(action_type, analyzer)` /
  `unregister_analyzer(action_type)`: register a domain-specific
  `ContextAnalyzer` for one exact action type, with the generic
  analyzer as fallback for everything else. Resolves a real conflict
  found building two independent projects on SETT: `EthicalFilter`
  only accepted a single analyzer for the whole system, so a project
  needing domain-specific scoring (e.g. economic harm for
  `"confirm_purchase"`) had to fully replace the generic analyzer for
  every action, not just the one that needed it.
  Additive and safe: existing code that never calls this is
  unaffected. 9 new tests. Verified in one of those projects: switching
  from a full analyzer replacement to `register_analyzer("confirm_purchase", ...)`
  produced byte-for-byte identical verdicts across all four demo
  scenarios, with the added correctness that every other action now
  uses the real generic analyzer instead of borrowing the economic one.
- `docs/api_reference.md` now documents `PhrasingExpert`: the base
  class formalizing the "LLM only phrases deterministic facts, never
  invents them, always has a fallback" pattern discovered independently
  twice while building an early prototype application, before either
  instance was planned as reusable. (The class itself already existed
  in the codebase; this release adds its documentation and its first
  real-world validation.) Verified by refactoring that prototype's
  greeting expert to use it: splitting one expert that mixed two
  phrasing responsibilities (greeting + habit acknowledgment) into two
  focused `PhrasingExpert` subclasses, with identical observable
  behavior before and after.
- `StubDomainAgent`: a generic, ready-to-use placeholder agent for a
  domain that isn't built yet. No subclassing needed. Register one per
  domain your router/synthesizer needs to call, so a multi-agent
  system's full flow is testable end to end before every real agent
  exists; swap in the real agent later under the same domain with no
  other change required. Extracted from a companion-assistant application built on top
  of SETT (first used to build and test a router and a multi-domain
  synthesizer before any of six domains had a real implementation).
  10 new tests, including one verifying a stub can be transparently
  replaced by a real agent under the same domain.

### Fixed
- `default_ruleset()` was missing a rule for `HarmCategory.AMBIGUITY`,
  even though the category and its weight (2.0) were already defined
  in `DEFAULT_HARM_WEIGHTS`: an inconsistency between what the
  framework declares and what its own default ruleset actually uses.
  Confirmed safe to add: `EthicalRuleset.rules` is not read anywhere
  in the actual score computation (only `reject_threshold`,
  `warn_threshold`, and `principle` are), so this changes no existing
  numeric behavior for anyone using the default ruleset: it only
  completes it. Found because a real downstream project had
  already worked around the gap by adding the rule to its own custom
  ruleset.

### Notes
- 158 tests passing. No breaking changes.
- This is Phase 0 and Phase 0.5 of a larger companion-assistant reconstruction
  schema: both were blockers for later phases (Shopping integration
  needed register_analyzer; any user-facing expert benefits from
  PhrasingExpert).

## [0.3.1] - 2026-07-17

### Fixed
- `templates/agent_template.py`, `templates/expert_template.py`, and
  `templates/README.md` were mistakenly published with all comments,
  docstrings, and TODOs in Spanish (the maintainer's own working
  language) while
  the rest of the repository is in English. This was an oversight:
  the working notes weren't translated back before publishing. All
  three files are now in English; the code itself never changed (it
  was already English throughout: class names, imports, logic).
  Verified the templates still instantiate and run correctly after
  translation.
- Fixed a leftover Spanish word ("almacén scenario") inside otherwise
  English comments in `sett/memory_ruler/universal.py`,
  `sett/risk_ruler/environmental_context.py`, and `tests/test_ethics.py`,
  now reading "warehouse scenario" throughout.
- Translated Spanish test data strings in `tests/test_ollama_adapter.py`
  (e.g. sample prompts like `"hola"`) to English for consistency. These
  were arbitrary example values with no effect on test behavior.

### Notes
- No functional changes in this release: code logic, the public API,
  and test coverage are identical to v0.3.0. 126 tests passing.

## [0.3.0] - 2026-07-16

### Added
- `CONTRIBUTING.md`: welcoming guide for issues and pull requests.
  Explicitly invites contributions in English, Español, or 日本語.
  Honest about what doesn't exist yet (no Discord/chat server, no
  translated docs) rather than implying infrastructure that isn't there.
- `sett/services_llm/ollama.py`: `OllamaAdapter`, for free, local,
  offline LLM inference via [Ollama](https://ollama.com). Unlike the
  cloud adapters, it requires **no extra pip dependency**: talks to
  Ollama's local REST API using only the Python standard library
  (`urllib`). Recommended low-resource models: `qwen3:1.7b` (lightest)
  or `phi4-mini` (MIT license, built for CPU-only machines). 17 new
  tests (HTTP mocked: Ollama itself is an external local service, not
  something a test environment can assume is installed).
- `templates/`: blank, copy-and-fill `agent_template.py` and
  `expert_template.py`. Both are real, runnable code as-is (verified:
  they instantiate, register, and process an empty result without
  errors before any customization): not pseudocode. `agent_template.py`
  documents the three valid ways to close `process()`
  (`_publish_to_universal`, `propose_action`, `submit_action` +
  Executor) as commented, mutually-exclusive options.
- `docs/getting_started.md` and `docs/api_reference.md` updated for
  both of the above; README notes `OllamaAdapter` needs no extra
  install.

### Fixed
- README intro and Key Features wording overclaimed what the
  EthicalFilter intercepts: it said "every action," which read as
  "every line inside an expert's `resolve()`." Corrected to say what
  is actually true: every action submitted as an `Action`
  (`propose_action`/`submit_action`) and every write to universal
  memory, before either takes effect.
- README Quick Start had dropped the `get_ethical_audit_log()`
  demonstration at some point: the part that makes the governance
  story tangible instead of just claimed. Re-added; verified the
  exact code block in the README produces the two audit log lines
  shown (`memory_write` from `_publish_to_universal`, and
  `send_notification` from `submit_action`).

### Status
126 tests passing (109 previous + 17 for OllamaAdapter). No breaking
changes to any existing public API.

## [0.2.1] - 2026-07-14

### Fixed
- `README.md` Quick Start example was still showing the pre-Executor
  code from v0.1.x: it never used `SETTExecutor`/`Action`, so it did
  not reflect the "Actions as Data" pattern shipped in v0.2.0. Updated
  to the verified, working example using `submit_action()`.
- Minor README improvements: badges, a short "Why SETT?" section, a
  "Key features" checklist, corrected license formatting, and
  `ContextAnalyzer`'s description now says "biometric" instead of the
  broader (and less accurate) "biological".

### Notes
- No code changes in this release: `sett/` is identical to v0.2.0.
  Only `README.md` and `pyproject.toml` (keywords) were updated.

## [0.2.0] - 2026-07-14

### Added
- `SETTExecutor` (`sett/core_ruler/executor.py`) and `Action`
  (`sett/core_ruler/action.py`): the "actions as data" pattern. Agents
  describe real-world side effects (send a message, call an emergency
  API, charge a payment) as data instead of performing them directly.
  Only a handler registered on the `SETTExecutor`: the sole component
  allowed to touch the real client: can execute the effect, and only
  after the `EthicalFilter` approves it.
- `SETTOrchestrator.register_executor()`: wires a `SETTExecutor` to
  universal memory and to every registered agent, order-independent
  (works whether agents are registered before or after the Executor).
- `SETTAgent.submit_action()`: submits an `Action` through the
  registered Executor. Raises `SETTConfigurationError` if no Executor
  or no matching handler is registered: fails closed, not open.
- `examples/with_executor.py`: full demonstration of the Executor
  pattern across four scenarios (approved notification, approved
  emergency dispatch, blocked-before-execution, missing handler).
- `tests/test_executor.py`: 18 new tests covering the Executor,
  `Action`, and `submit_action()` end to end, including the privacy
  contract (RiskProfile values never leak into the audit log) and
  order-independent registration.

### Fixed (0.1.1, folded into this release)
- **Corrected note**: an earlier draft of this changelog claimed
  `sett/__init__.py` was corrupted in the published v0.1.0 package. That
  was inaccurate: the file on GitHub was correct. The corruption was
  present only in a local verification zip generated during this
  changelog's audit process, not in the actual published release. No
  fix was needed for this; noted here only to correct the record.
- `examples/with_ethics.py` crashed with `TypeError` in Scenario 1:
  `AwareContextAnalyzer.analyze()` had a signature that didn't accept
  `risk_profile`/`environmental_context`, which `EthicalFilter` always
  passes. Fixed.
- `emotional_state`, `RiskProfile`, and `EnvironmentalContext` never
  actually reached the `EthicalFilter` in the real orchestrated flow:
  `orchestrator.process()` accepted `emotional_state` but never
  forwarded it to `agent.process()`, and `_publish_to_universal()` /
  `UniversalMemory.update()` never forwarded any of the three risk
  layers either. Every real evaluation silently ran with
  `emotional_state="unknown"` and no `RiskProfile`/`EnvironmentalContext`,
  regardless of what was passed in: only direct calls to
  `filter.evaluate()` (as in the test suite) exercised the full system.
  Fixed via automatic propagation: the orchestrator sets
  `agent._current_emotional_state` / `agent._current_location_id`
  before calling `process()`, and `_publish_to_universal()` reads them
  automatically. No existing agent subclass needs to change.
- Biometric risk detection (`ContextAnalyzer._detect_human_at_risk`)
  only checked `context["health"]["heart_rate_bpm"]` (nested), but
  agents publish flat results (`context["heart_rate_bpm"]`, as in
  `examples/multi_agent.py`): so dangerous vitals (HR=155,
  temp=39.8°C) were silently evaluated as `ALLOW`. Now checks both
  nested and flat structures.

### Notes
- `propose_action()` (added in 0.1.1) remains available as the
  lightweight, no-setup alternative to `submit_action()`: use it for
  low-stakes side effects; use the Executor pattern for the ones where
  "the developer forgot to gate it" is not an acceptable failure mode.
- `SETTEthicalFilterWarningError` and `SETTMemoryAccessDeniedError`
  remain defined but unused: kept intentionally for future use rather
  than removed; `SETTConfigurationError` gained its first real use in
  this release (missing Executor/handler).
- `PrivateMemory` access restriction remains a Python convention
  (leading underscore), not a runtime-enforced boundary. This is a
  deliberate scope decision, not an oversight: revisit only if a
  concrete exploit path is identified.

## [0.1.0] - 2026-07-09

Initial public release. Core hierarchy (`SETTOrchestrator` /
`SETTAgent` / `SETTExpert`), dual memory (`PrivateMemory` /
`UniversalMemory`), `EthicalFilter` with three-layer risk evaluation
(`HarmCategory` / `RiskProfile` / `EnvironmentalContext`), LLM adapters
for Anthropic, OpenAI, and Gemini. 91 tests. Published alongside the
SETT preprint on Zenodo (English, Español, 日本語).
