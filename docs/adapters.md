# Adapter contracts

SETT adapters are explicit application dependencies. The framework describes,
filters, and observes adapters; it never chooses a provider, creates a default,
or silently moves work to another service.

## The contract

Every `LLMBase`, `TTSBase`, `STTBase`, and `SentimentBase` instance exposes:

- `capabilities`: an immutable `AdapterCapabilities` declaration;
- `health`: the most recent passive observation, initially `UNKNOWN`;
- `check_health()`: a non-billable read of that observation.

Capability support is tri-state. `UNKNOWN` means the adapter has not made a
claim; it is not interchangeable with `NO`. A requirement rejects both values,
but reports a different reason code for each.

```python
from sett import AdapterKind, AdapterRequirements

requirements = AdapterRequirements(
    AdapterKind.STT,
    language="es",
    input_format="audio/wav",
)
```

Capabilities describe the implementation in SETT, not every feature the
underlying provider may offer. For example, a synchronous adapter declares
`streaming=NO` even when the provider has a separate streaming API.

## Explicit selection

`select_adapter()` is a pure filter. One eligible candidate may be returned
directly. More than one requires a caller-owned `SelectionPolicy`; otherwise
the result explains `adapter.selection_policy_required` and chooses nothing.

```python
from sett import (
    AdapterHandle,
    AdapterKind,
    AdapterRequirements,
    PrioritySelectionPolicy,
    select_adapter,
)

candidates = [
    AdapterHandle("primary", application_primary_adapter),
    AdapterHandle("secondary", application_secondary_adapter),
]
selection = select_adapter(
    candidates,
    AdapterRequirements(AdapterKind.TTS, output_format="audio/mpeg"),
    policy=PrioritySelectionPolicy(("primary", "secondary")),
)
```

The names and order above are application configuration, not SETT defaults.
Selection performs no network I/O and does not invoke an adapter.

## Failures, resilience, and health

All official adapters raise `SETTAdapterError` through the existing
`SETTLLMAdapterError` or `SETTServiceAdapterError` subclasses. Structured
fields include provider, operation, category, status, retryability, and a
stable reason code. `safe_view()` deliberately excludes raw messages and
causes.

Since v0.14.0, the public message is built only from values SETT controls:
the provider label, the operation, the neutral error category, and the
protocol status. It reads `azure_speech synthesize failed (authentication)`
and never carries the provider's own exception text. Earlier releases
embedded that text truncated to 200 characters, which bounded its length
and nothing else, so `logger.error("%s", error)` exported whatever the
provider had put in `str(e)`.

The provider's message is not discarded. It remains on `__cause__`, so a
local traceback still shows it in full. That is the intended asymmetry:
formatting an adapter error is safe by default, and reading the provider's
text is a deliberate diagnostic act. `logger.exception(...)` and any other
traceback-printing call still export the chain, so a deployment that must
not log provider text at all should log `error.safe_view()` and catch
adapter failures before an uncaught traceback is written.

Retries and circuit breaking are opt-in through
`execute_with_resilience()`. No retry occurs without a `RetryPolicy` and an
explicit `operation_is_idempotent=True`. A `CircuitBreaker` is per instance;
state is not shared or persisted by SETT.

Successful and failed calls update passive health. Health checks never make a
hidden provider request. Adapter call, health, retry, circuit, and selection
events join the active trace with metadata only; never prompts, audio,
responses, credentials, or raw provider messages.

## Official voice roster

All implementations are peers. Import concrete classes from
`sett.services_tts_stt`.

| Kind | Adapters |
|---|---|
| TTS | `GoogleTTSAdapter`, `ElevenLabsTTSAdapter`, `AzureTTSAdapter`, `CartesiaTTSAdapter`, `LocalTTSAdapter`, `KokoroTTSAdapter`, `PiperTTSAdapter`, `PocketTTSAdapter`, `EdgeTTSAdapter` |
| STT | `GoogleSTTAdapter`, `DeepgramSTTAdapter`, `AzureSTTAdapter`, `AssemblyAISTTAdapter`, `GladiaSTTAdapter`, `WhisperSTTAdapter`, `LocalSTTAdapter` |

`LocalTTSAdapter` uses pyttsx3 host voices. `LocalSTTAdapter` uses a lazily
loaded faster-whisper model. `PyttsxTTSAdapter` and
`FasterWhisperSTTAdapter` are descriptive aliases for those two classes.
`LocalTTSAdapter.synthesize()` accepts `language_code` for host-voice
matching. `LocalSTTAdapter.transcribe()` accepts the same name and retains
`language` as a compatibility alias; both are normalized to the primary
subtag faster-whisper expects.

Every official STT adapter that exposes language selection accepts
`language_code`. Azure and Deepgram accept it at construction and per call;
Gladia and hosted Whisper accept it per call. Hosted Whisper and LocalSTT
reduce a BCP-47 tag to the primary subtag their providers expect. Historical
`language` spellings remain compatibility aliases.

Official TTS adapters with per-call BCP-47 selection use the same canonical
name, including Azure, Cartesia, Google, and LocalTTS. Supplying both names
with different effective tags fails before provider work instead of silently
choosing one or discarding `language_code`.

`PocketTTSAdapter` uses Kyutai Pocket TTS 2.1. Install it from the source tree
with `pip install -e ".[pocket-tts]"`. The adapter loads its model and voice
state on first use and returns one complete mono PCM16 WAV payload. It reports
`streaming=NO` because SETT does not expose the provider's chunk iterator
through `TTSBase`. It reports voice cloning because Pocket TTS accepts caller
audio or exported voice states. A first model or catalog-voice load may
download assets; cached assets do not require a remote synthesis service.
Pocket TTS documents one model instance as unsafe for concurrent generation,
so the adapter serializes calls. SETT does not bundle model weights or voices;
applications remain responsible for voice consent and applicable licenses.

`EdgeTTSAdapter` is explicitly community/unofficial: `edge-tts` calls an
internal Microsoft Edge reader-mode service, not the supported Azure Speech
API. Applications must evaluate availability, terms, and commercial-use risk.

Provider dependencies are optional extras. Constructing a remote adapter does
not contact its provider; local model-backed adapters load models on first use.

## Third-party verification

The dependency-free kit under `sett.testing` can run from pytest, unittest, or
a plain build script:

```python
from sett import AdapterKind
from sett.testing import assert_adapter_contract

assert_adapter_contract(my_adapter, AdapterKind.STT)
```

Behavioral helpers `assert_cancellation_preflight()` and
`assert_normalized_adapter_error()` accept caller-supplied probes, so the
third-party test remains in control of fake clients and never needs a live
provider.
