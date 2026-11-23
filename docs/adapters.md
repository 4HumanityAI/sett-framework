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

## Provider model configuration

SETT keeps provider integrations, but does not select a model for the application.
Supply a nonblank identifier when constructing these adapters:

| Adapters | Required argument |
| --- | --- |
| Anthropic, OpenAI, Gemini, Ollama | `model` |
| OpenAI Whisper, Deepgram, Cartesia | `model` |
| ElevenLabs | `model_id` |
| Local faster-whisper | `model_size` |

Missing or blank values report `adapter.model_missing` after dependency and
credential checks. SETT validates the identifier's presence, not its availability
in a provider account. Provider calls can still reject an unavailable model.
Voice identifiers, languages and audio containers remain separate settings.
Adapters for fixed engines retain their existing interfaces; no artificial model
argument is added to them.

For example, the application can read its choice from an environment variable:

```python
import os
from sett.services_llm.ollama import OllamaAdapter

llm = OllamaAdapter(model=os.environ["SETT_LLM_MODEL"])
```

Set that variable to a model installed on your Ollama server before running the
example. SETT itself does not read it or supply a fallback. Existing applications
that relied on a default must now pass their chosen identifier explicitly.
Gemini additionally requires the `google-genai` package from the `gemini` extra;
install the updated extra from the source checkout when upgrading.

## LLM request parameters and credentials

OpenAI uses Chat Completions. Its existing defaults remain a legacy
`max_tokens=1024` budget and `temperature=0.75`. Models that require a completion
budget or reject sampling parameters must be configured explicitly:

```python
import os
from sett.services_llm.openai import OpenAIAdapter

llm = OpenAIAdapter(
    model=os.environ["SETT_LLM_MODEL"],
    max_completion_tokens=4096,
    temperature=None,
)
answer = llm.complete("Describe the inputs required by this task.")
```

Set `OPENAI_API_KEY` and choose an available model that supports these settings.
The two token limits are mutually exclusive positive integers. At construction,
`None` means unset; leaving both unset preserves the legacy 1024-token default.
The constructor's `max_tokens` default is now `None` to distinguish it from an
explicit limit. A per-call token argument replaces the configured token choice
for that call; leaving both per-call values unset selects the legacy default.
When neither token keyword is passed, the constructor's choice is retained.
`max_completion_tokens` includes reasoning tokens where applicable, so it is
not a promise of that many visible output tokens. `temperature=None` omits the
field, at construction or per call. Existing calls with neither new setting
retain their previous request parameters. SETT does not infer capabilities from
model names or retry a rejected request with different parameters.

Anthropic returns all `text` blocks in order, concatenated without an added
separator. Thinking, redacted thinking and other non-text blocks are excluded.
No visible text produces an empty string, not evidence that a task succeeded.
Malformed text blocks produce `adapter.invalid_response`. This text-only
interface does not implement tool calls or preserve thinking signatures for
provider-native conversation replay. Sampling parameters remain optional and
subject to the installed SDK and the selected model.

Commercial LLM adapters accept an explicit `api_key`. Otherwise OpenAI reads
`OPENAI_API_KEY`, Anthropic reads `ANTHROPIC_API_KEY`, and Gemini tries
`GOOGLE_API_KEY` before `GEMINI_API_KEY`. `None` and empty strings fall through
to the next credential source; a selected whitespace-only value is rejected
after stripping, without trying another identity. SETT does not load `.env`
files. Install the matching optional extra from the source checkout, for
example `pip install -e ".[openai]"`, `.[anthropic]` or `.[gemini]`.

These request and response rules have offline regression coverage with SDK
doubles. That coverage does not establish authenticated compatibility with
every provider, model or SDK version. Real-provider checks require separately
supplied credentials and must be opt-in. Ollama transport regressions use a
local HTTP test server, not a running Ollama model server. Remote HTTP carries
prompts and replies without transport encryption; secure the connection before
sending sensitive content.

## Azure audio containers

Azure TTS supports MP3, WAV, OGG, WebM and raw PCM through its configured
`output_format`. For OGG, construct a separate `AzureTTSAdapter` with
`output_format="ogg-24khz-16bit-mono-opus"`, your credential, region and any
provider-specific voice settings. Its `audio_format` is `"ogg"` and its declared
output MIME type is `"audio/ogg"`.

A per-call override may not change the configured container. An MP3 adapter
rejects an OGG override with `adapter.output_format_invalid` before making the
request. Construct an OGG adapter instead; this keeps the returned bytes and
the instance's declared format consistent. These declarations identify the
requested format; they do not validate arbitrary audio returned by a provider.

## Request construction and response limits

Identifiers and endpoints are checked before any request is built.

- An ElevenLabs `voice_id`, at construction or per call, must be a single
  URL path segment made of letters, digits, `-`, `.`, `_` or `~`.
- An Azure `region` must be a single host label such as `westeurope`. To
  use another host, pass `endpoint`.
- `endpoint` and `base_url` on the HTTP voice adapters must be absolute
  `http` or `https` URLs that name a host, without user information or a
  fragment. Any host is accepted, including a proxy or a local test server.
  With `http` the credential travels without transport encryption.

A refused value raises a configuration error and no request is sent.

Timeouts bound each socket operation, not the whole call. A provider that
keeps sending data slowly can hold a call open longer than
`timeout_seconds`. Cancellation tokens and deadlines are checked before a
request and after its response, and between polls of an asynchronous job.
They do not interrupt a read that is already in progress.

Only `OllamaAdapter` bounds the size of a response, through
`max_response_bytes`. The HTTP voice adapters read the provider's whole
response into memory. A declared audio format identifies what was
requested; the bytes returned are not inspected.

`AnthropicAdapter` and `OpenAIAdapter` send the system prompt
`You are a helpful assistant.` when the caller passes none. `GeminiAdapter`
and `OllamaAdapter` send no system prompt in that case. Pass `system`
explicitly when the difference matters.

Model identifiers have no default. Voice identifiers are a separate
setting, and several voice adapters, such as Azure, Edge, ElevenLabs and
Kokoro, do ship one. Set the voice explicitly when the application depends
on a particular one.

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

`ElevenLabsTTSAdapter` returns MP3. Its optional `output_format` selects an
ElevenLabs MP3 format, such as `mp3_44100_128`, sent as the provider's
`output_format` query parameter. Only MP3-family identifiers are accepted,
so the adapter's `mp3` container and `audio/mpeg` capability stay true; any
other value fails at construction with `adapter.output_format_invalid`. The
default, `None`, sends no parameter, and the provider uses its own default.
The format is fixed per instance. Which formats an account may use is the
provider's rule, not SETT's.

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

The HTTP voice adapters using the shared transport, and ElevenLabs TTS,
reject redirects. A Gladia transcription job can be polled only at the
configured origin, with the same scheme, host and effective port.
