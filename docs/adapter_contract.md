# Adapter contract and resilience design

Status: current framework contract  
Introduced: SETT Framework v0.13.0  
Last reviewed: SETT Framework v0.13.2

This is the canonical, living design document for SETT adapters. Future
releases update this file in place; Git history and the changelog preserve the
version in which each decision was introduced.

## 1. Purpose

The contract introduced in v0.13.0 standardizes how applications inspect,
select, observe, and recover from external adapters. It adds capability
declarations, passive health,
provider-neutral errors, opt-in resilience, pure selection, sanitized trace
events, and a reusable third-party contract kit.

The contract remains faithful to SETT's existing boundaries:

- adapters are dependencies called by experts, not agents;
- the orchestrator does not own or register provider clients;
- applications choose and configure providers explicitly;
- the core framework keeps zero mandatory dependencies;
- no hidden health, selection, or fallback operation may contact a provider.

## 2. Public contracts

`sett.core_ruler.adapter` contains immutable provider-neutral types:

- `AdapterKind`: LLM, TTS, STT, or sentiment;
- `CapabilitySupport`: YES, NO, or UNKNOWN;
- `ConnectivityRequirement`: none, local service, remote service, or unknown;
- `AdapterCapabilities`: declared implementation behavior;
- `AdapterRequirements`: caller-owned requirements;
- `AdapterHealthStatus` and `AdapterHealth`: the latest passive observation;
- `AdapterErrorKind` and `AdapterErrorView`: safe error classification;
- `AdapterHandle`, `SelectionPolicy`, and `AdapterSelection`: pure selection.

UNKNOWN is intentionally distinct from NO. Requiring a capability rejects
both, but the selection result uses different reason codes. Capability
collections are immutable and `safe_view()` returns detached JSON-compatible
data.

Capabilities describe the adapter implementation that ships, not every
feature its provider might expose through some other API. A synchronous
implementation therefore declares streaming as NO.

## 3. Backward-compatible bases

`LLMBase`, `TTSBase`, `STTBase`, and `SentimentBase` retain their existing
abstract methods and gain concrete properties:

- `capabilities`, conservatively UNKNOWN except for the interface kind;
- `health`, initially UNKNOWN;
- `check_health()`, a read-only observation with no provider I/O.

A third-party v0.12 subclass still instantiates without implementing anything
new. Official adapters override `capabilities` with truthful declarations.

## 4. Error hierarchy

`SETTAdapterError` is the common parent of the existing
`SETTLLMAdapterError` and `SETTServiceAdapterError`. Their old one-message
constructors remain valid.

Structured fields include category, provider, adapter, operation, HTTP
status, retryability, retry delay, capability, and reason code. `safe_view()`
excludes raw messages and exception causes. Normalization preserves the
original provider exception as `__cause__`.

The normalized public message is composed only of controlled identifiers
(provider, operation, category, protocol status). Provider-supplied text is
never part of `str(error)`; it is reachable only through `__cause__`. See
`docs/adapters.md` for the logging consequence.

Lifecycle exceptions keep their own types and are never translated into a
provider error.

## 5. Voice capability model

Voice cloning is a TTS capability, not a new adapter kind. An adapter that can
operate with caller-supplied voice identity or conditioning declares
`voice_cloning=YES`; this is not an authorization or policy statement.
Consent, licensing, disclosure, and persona selection are application
concerns and are outside this release.

ElevenLabs and Cartesia declare custom voice-ID support. Pocket TTS accepts
audio prompts and exported voice states. Fixed-voice local and community
implementations do not declare voice cloning.

## 6. Official adapter roster

Existing official adapters receive capability declarations:

- LLM: `OpenAIAdapter`, `AnthropicAdapter`, `GeminiAdapter`, `OllamaAdapter`;
- TTS/STT: `GoogleTTSAdapter`, `GoogleSTTAdapter`,
  `ElevenLabsTTSAdapter`;
- sentiment: `GoogleSentimentAdapter`.

New voice adapters:

- STT: `DeepgramSTTAdapter`, `AzureSTTAdapter`,
  `AssemblyAISTTAdapter`, `GladiaSTTAdapter`, `WhisperSTTAdapter`,
  `LocalSTTAdapter`;
- TTS: `AzureTTSAdapter`, `CartesiaTTSAdapter`, `LocalTTSAdapter`,
  `KokoroTTSAdapter`, `PiperTTSAdapter`, `PocketTTSAdapter`,
  `EdgeTTSAdapter`.

`LocalTTSAdapter` uses pyttsx3 host voices. `LocalSTTAdapter` uses a lazily
loaded faster-whisper model. Descriptive aliases are also available as
`PyttsxTTSAdapter` and `FasterWhisperSTTAdapter`.

`PocketTTSAdapter` uses Kyutai Pocket TTS through a lazily loaded local model.
Its SETT path returns complete PCM16 WAV bytes and therefore declares
streaming as NO, even though the provider also exposes a separate chunked
API. One adapter instance serializes generation because the upstream model
does not support concurrent calls. Initial model or voice acquisition may
need network access; synthesis uses no remote service once those assets are
cached.

The AssemblyAI, Gladia, hosted Whisper, Cartesia, Kokoro, Piper, and Pocket TTS routes may
ship without live-account release tests. They must still be implemented,
instantiable, dependency-isolated, capability-declared, and covered with fake
provider tests.

`EdgeTTSAdapter` is included with an explicit community/unofficial label. It
uses an internal reader-mode service rather than the supported Azure Speech
API. Its metadata and documentation make that limitation visible.

## 7. No framework defaults

SETT ships adapters and capability facts, never a provider choice.

- There is no `get_default_*_adapter()` API.
- `select_adapter()` filters and explains; it does not recommend.
- One eligible candidate may be selected directly.
- Multiple eligible candidates require a caller-supplied `SelectionPolicy`.
- Documentation examples must say that their ordering is application-owned.

Commercial plans, quotas, entitlements, credential stores, and a higher-level
capability broker remain outside SETT.

## 8. Selection

`select_adapter()` is pure and deterministic. It checks kind, declared
capabilities, language, formats, connectivity, passive health, and an optional
circuit breaker. It performs no provider call.

Every exclusion receives a stable reason code. UNKNOWN and unsupported
capabilities are explained separately. An unavailable health state and an
open circuit are explicit exclusions. When a caller policy selects a route
after other candidates were rejected, a sanitized fallback-selection event
may join the active trace.

## 9. Health and tracing

Health starts UNKNOWN and changes only after a real call or an explicit,
honest check. No billable probe is hidden behind property access.

Adapter events use the existing trace chain:

- `adapter.call_started`, `adapter.call_completed`, `adapter.call_failed`;
- `adapter.health_checked`;
- `adapter.retry_scheduled`;
- `adapter.circuit_opened`, `adapter.circuit_closed`;
- `adapter.fallback_selected`.

Event attributes may include adapter name, modality, operation, duration,
error category, and reason code. They never include prompts, audio, provider
responses, credentials, or raw provider messages.

## 10. Resilience

Retry and circuit breaking live in `sett.core_ruler.resilience`. The previous
`sett.core_ruler.retry` path remains as a compatibility re-export.

`CircuitBreaker` is thread-safe, per instance, and uses an injectable
monotonic clock. Its states are CLOSED, OPEN, and HALF_OPEN; only one
half-open probe is admitted. The recovery timeout starts when the circuit
transitions to OPEN. Failures from calls that were already in flight continue
to be counted but do not postpone that recovery window; a failed HALF_OPEN
probe is a new OPEN transition and starts a fresh timeout.

Resilience is opt-in:

- no retry occurs without a `RetryPolicy`;
- retry also requires `operation_is_idempotent=True`;
- cancellation, validation, configuration, authentication, ethical rejection,
  and idempotency conflict never become retry permission;
- provider timeouts remain ambiguous and are not automatically retried;
- `Retry-After` never extends the caller's remaining deadline;
- circuit state is neither global nor persisted across restarts.

## 11. Contract kit

`sett.testing.adapter_contract` has no pytest dependency. It provides static
inspection and caller-controlled behavioral probes for:

- immutable, JSON-safe capability exports;
- correct interface kind;
- honest initial UNKNOWN health;
- cancellation before provider contact;
- normalized errors with preserved causes;
- secret-free safe error views.

The kit never contacts a provider by itself.

## 12. Original v0.13 release gate

The original v0.13.0 release gate required:

- capability inspection works without concrete-class knowledge;
- all official adapters declare only their implemented behavior;
- provider failures share the normalized error hierarchy;
- selection and fallback need no provider-specific exception clauses;
- third-party v0.12 adapters still instantiate;
- retries and circuit breaking remain off by default;
- traces explain attempts and degradation without content leakage;
- the public contract kit validates a minimal external adapter;
- existing tests and the new v0.13 suite pass;
- compile, lint, build, clean install, `pip check`, examples, and release
  audits pass.

## 13. Original v0.13 scope boundary

The original v0.13.0 scope did not add credential storage, billing, quota or entitlement
policy, persona selection, a commercial capability broker, generic streaming
interfaces without two real implementations, general concurrent execution,
or cross-restart persistence of health and circuit state.
